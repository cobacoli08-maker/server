# ══════════════════════════════════════════════════════════════════════════════════
#  decal_db_addon.py  —  Public database + gallery for every uploaded decal
#  ------------------------------------------------------------------------
#  Used together with metadata_server.py (Railway) OR bot_server.py (local).
#
#  Install — add this to metadata_server.py, PLACE it before `if __name__`
#  (exactly like the lyric_sync_addon block):
#
#      try:
#          import decal_db_addon
#          decal_db_addon.register(app)   # cookie is read from the ROBLOX_COOKIE ENV
#          print("✅ decal_db_addon registered (/decals, /decals/sync, /gallery)")
#      except Exception as _e:
#          print("⚠️ decal_db_addon not loaded:", _e)
#
#  Needs Pillow to detect blank images  ->  add `pillow` to
#  metadata_requirements.txt.
#
#  ENV used (set these on Railway):
#   • ROBLOX_COOKIE   (required) — a Roblox account cookie used to upload decals.
#   • MULTI-ACCOUNT (optional) — decals from several accounts merge into ONE DB:
#       - ROBLOX_COOKIE_2, ROBLOX_COOKIE_3, ... ROBLOX_COOKIE_20  (one cookie each), OR
#       - ROBLOX_COOKIES  (many cookies in a single var, separated by a NEW LINE).
#     /decals/sync pulls every account and stores them together, tagged by owner.
#     A dead/banned cookie is skipped automatically; the other accounts still sync,
#     and decals already in the DB are kept (the database is never wiped on sync).
#   • DECAL_ADMIN_KEY (recommended for public) — if set, endpoints that
#     modify data (sync/rescan/add/delete) MUST send the X-Admin-Key header.
#     Reading /decals stays PUBLIC. If empty, all endpoints are open (local mode).
#   • DECAL_DB_DIR    (recommended on Railway) — folder to store the database. Point it to
#     a Railway Volume (e.g. /data) so data isn't lost on every redeploy.
#
#  Endpoint:
#   • GET  /decals            -> list of valid decals (public). ?all=1 = include blank/pending.
#   • POST /decals/sync       -> pull ALL account decals from Roblox + detect blanks. (admin)
#   • POST /decals/rescan     -> recheck pending/blank/blocked entries. (admin)
#   • POST /decals/add        -> add one asset id manually/automatically. (admin)
#   • DELETE /decals/<id>     -> remove one entry from the database. (admin)
#   • GET  /decals/export     -> download the FULL database as a JSON backup. (admin)
#   • POST /decals/import     -> restore a backup (merge by default; ?mode=replace to wipe first). (admin)
#   • GET  /gallery           -> serve decal_gallery.html if present (for local use).
#
#  Valid data only: titles/asset ids exactly as on the Roblox account, nothing fabricated.
# ══════════════════════════════════════════════════════════════════════════════════
import os
import io
import json
import time
import requests as http_req
from flask import request, jsonify, send_file, Response

_HERE = os.path.dirname(os.path.abspath(__file__))
_DIR = (os.environ.get("DECAL_DB_DIR", "").strip() or _HERE)
DB_PATH = os.path.join(_DIR, "decals_db.json")          # full database (internal)
PUBLIC_SNAPSHOT = os.path.join(_DIR, "decals.json")      # snapshot for static hosting
GALLERY_HTML = os.path.join(_HERE, "decal_gallery.html")

# Blank-detection thresholds. Conservative so real images are NOT discarded.
# A failed decal is truly flat (#FFFFFF / #000000), pixel spread ~0.
_UNIFORM_SPREAD_MAX = 8
_WHITE_MIN = 249
_BLACK_MAX = 6


# ── storage ─────────────────────────────────────────────────
def _load_db():
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and "items" in data:
                return data
    except Exception:
        pass
    return {"items": {}, "updatedAt": 0}


def _save_db(db):
    db["updatedAt"] = int(time.time())
    try:
        os.makedirs(_DIR, exist_ok=True)
    except Exception:
        pass
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    ok = [v for v in db["items"].values() if v.get("status") in ("ok", "pending", "blank", "error")]
    ok.sort(key=lambda x: x.get("created") or "", reverse=True)
    try:
        with open(PUBLIC_SNAPSHOT, "w", encoding="utf-8") as f:
            json.dump({"items": ok, "updatedAt": db["updatedAt"]}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[decal_db] failed to write snapshot:", e)


# ── blank detection (Pillow) ────────────────────────────────────
def _analyze_blank(img_bytes):
    try:
        from PIL import Image
    except Exception:
        return "ok", {"note": "pillow-missing"}
    try:
        im = Image.open(io.BytesIO(img_bytes)).convert("RGB").resize((32, 32))
        px = list(im.getdata())
        rs = [p[0] for p in px]; gs = [p[1] for p in px]; bs = [p[2] for p in px]
        spread = max(max(rs) - min(rs), max(gs) - min(gs), max(bs) - min(bs))
        avg = (sum(rs) + sum(gs) + sum(bs)) / (3 * len(px))
        uniform = spread <= _UNIFORM_SPREAD_MAX
        is_white = uniform and avg >= _WHITE_MIN
        is_black = uniform and avg <= _BLACK_MAX
        blank = bool(is_white or is_black)
        return ("blank" if blank else "ok"), {
            "spread": spread, "avg": round(avg, 1), "white": is_white, "black": is_black,
        }
    except Exception as e:
        return "ok", {"note": "analyze-failed:" + str(e)}


# ── Roblox API ──────────────────────────────────────────────
def _session(cookie):
    s = http_req.Session()
    s.cookies['.ROBLOSECURITY'] = cookie
    s.headers.update({"User-Agent": "Mozilla/5.0 DecalDB"})
    return s


def _whoami(s):
    """Return (userId, username) for the cookie's account, or (None, None)."""
    try:
        r = s.get("https://users.roblox.com/v1/users/authenticated", timeout=15)
        if r.status_code == 200:
            d = r.json()
            return str(d.get("id") or ""), (d.get("name") or "").strip()
    except Exception:
        pass
    return None, None


def _fetch_all_created_decals(s):
    """Pull all Decals created by the account (paginated) via the creations API."""
    items = []
    cursor = ""
    for _ in range(200):  # safety cap
        params = {"assetType": "Decal", "isArchived": "false", "limit": 50, "sortOrder": "Desc"}
        if cursor:
            params["cursor"] = cursor
        r = s.get("https://itemconfiguration.roblox.com/v1/creations/get-assets",
                  params=params, timeout=30)
        if r.status_code == 401:
            raise RuntimeError("Cookie expired / invalid (401).")
        if r.status_code != 200:
            raise RuntimeError("creations get-assets HTTP %s: %s" % (r.status_code, r.text[:200]))
        data = r.json()
        for it in (data.get("data") or []):
            aid = it.get("assetId") or it.get("id")
            if not aid:
                continue
            items.append({
                "assetId": str(aid),
                "name": (it.get("name") or "").strip() or ("Decal " + str(aid)),
                "created": it.get("created") or it.get("createdTime") or "",
            })
        cursor = data.get("nextPageCursor") or ""
        if not cursor:
            break
        time.sleep(0.2)
    return items


def _fetch_thumbnails(s, asset_ids):
    out = {}
    for i in range(0, len(asset_ids), 50):
        batch = asset_ids[i:i + 50]
        r = s.get("https://thumbnails.roblox.com/v1/assets", params={
            "assetIds": ",".join(batch), "size": "420x420",
            "format": "Png", "returnPolicy": "PlaceHolder",
        }, timeout=30)
        if r.status_code != 200:
            continue
        for d in (r.json().get("data") or []):
            out[str(d.get("targetId"))] = {
                "state": d.get("state") or "", "imageUrl": d.get("imageUrl") or "",
            }
        time.sleep(0.15)
    return out


def _fetch_created_dates(s, asset_ids):
    """Return {assetId: created_iso}. The creations API does NOT include the real
    upload date, so we read it from the develop assets API (the same "Created"
    date Roblox shows on the asset page, e.g. "Created Jul 14, 2026")."""
    out = {}
    for i in range(0, len(asset_ids), 50):
        batch = asset_ids[i:i + 50]
        try:
            r = s.get("https://develop.roblox.com/v1/assets",
                      params={"assetIds": ",".join(batch)}, timeout=30)
            if r.status_code != 200:
                continue
            for d in (r.json().get("data") or []):
                aid = str(d.get("id") or d.get("assetId") or "")
                c = d.get("created") or d.get("createdTime") or ""
                if aid and c:
                    out[aid] = c
        except Exception as e:
            print("[decal_db] created-date fetch error:", e)
        time.sleep(0.15)
    return out


def _classify(entry, thumb, deep_blank_check=True):
    state = (thumb or {}).get("state", "")
    img = (thumb or {}).get("imageUrl", "")
    entry["state"] = state
    entry["imageUrl"] = img
    if state == "Blocked":
        entry["status"] = "blocked"; return
    if state in ("Pending", "InReview", "") or not img:
        entry["status"] = "pending"; return
    if state == "Error":
        entry["status"] = "error"; return
    if deep_blank_check:
        try:
            ib = http_req.get(img, timeout=20).content
            status, detail = _analyze_blank(ib)
            entry["blankDetail"] = detail
            entry["status"] = status
            return
        except Exception as e:
            entry["blankDetail"] = {"note": "dl-failed:" + str(e)}
    entry["status"] = "ok"


# ── register ───────────────────────────────────────────────
def register_uploaded_decal(asset_id, name="", cookie=None):
    """Immediately add a freshly-uploaded decal to the shared DB so it appears on
    /decal right away, WITHOUT waiting for a full "Sync from Roblox". Safe to call
    from the upload route; returns the stored entry (or None for an invalid id)."""
    aid = str(asset_id or "").strip()
    if not aid.isdigit():
        return None
    cookie = cookie or os.environ.get("ROBLOX_COOKIE", "").strip()
    db = _load_db()
    prev = db["items"].get(aid, {})
    entry = {"assetId": aid,
             "name": (name or "").strip() or prev.get("name") or ("Decal " + aid),
             "created": prev.get("created", ""),
             "addedAt": prev.get("addedAt") or int(time.time()),
             "owner": prev.get("owner", ""), "ownerId": prev.get("ownerId", "")}
    try:
        if cookie:
            sess = _session(cookie)
            uid, uname = _whoami(sess)
            if uname:
                entry["owner"] = uname; entry["ownerId"] = uid or entry.get("ownerId", "")
            cd = _fetch_created_dates(sess, [aid])
            if cd.get(aid):
                entry["created"] = cd[aid]
            thumbs = _fetch_thumbnails(sess, [aid])
            _classify(entry, thumbs.get(aid))
        else:
            entry.setdefault("status", "ok")
    except Exception as e:
        print("[decal_db] register_uploaded_decal error:", e)
        entry.setdefault("status", "pending")
    db["items"][aid] = entry
    _save_db(db)
    print("[decal_db] auto-registered uploaded decal", aid, "->", entry.get("status"))
    return entry


def register(app, roblox_cookie=None):
    """register(app) -> cookie from ENV; or register(app, COOKIE) for local use."""

    def _cookies():
        """Collect every configured account cookie (deduped, in order).
        Sources: register(app, cookie) arg, ROBLOX_COOKIE, ROBLOX_COOKIE_2..20,
        and ROBLOX_COOKIES (newline- or ';;'-separated)."""
        out = []
        if roblox_cookie:
            out.append(roblox_cookie.strip())
        single = os.environ.get("ROBLOX_COOKIE", "").strip()
        if single:
            out.append(single)
        for n in range(2, 21):
            v = os.environ.get("ROBLOX_COOKIE_%d" % n, "").strip()
            if v:
                out.append(v)
        blob = os.environ.get("ROBLOX_COOKIES", "")
        if blob:
            for line in blob.replace(";;", "\n").splitlines():
                v = line.strip()
                if v:
                    out.append(v)
        seen = set(); uniq = []
        for c in out:
            if c and c not in seen:
                seen.add(c); uniq.append(c)
        return uniq

    def _cookie():
        cs = _cookies()
        return cs[0] if cs else ""

    def _admin_key():
        return os.environ.get("DECAL_ADMIN_KEY", "").strip()

    def _admin_ok():
        key = _admin_key()
        if not key:
            return True  # not set = open mode (local/dev)
        given = request.headers.get("X-Admin-Key") or request.args.get("key") or ""
        return given == key

    def _deny():
        return jsonify({"error": "admin only — a valid X-Admin-Key is required"}), 403

    @app.post("/decals/sync")
    def decals_sync():
        if not _admin_ok():
            return _deny()
        cookies = _cookies()
        if not cookies:
            return jsonify({"error": "ROBLOX_COOKIE is not set."}), 500
        db = _load_db(); items = db["items"]
        added = 0
        accounts = []          # per-account report
        errors = []            # dead/banned/expired cookies etc.
        for idx, cookie in enumerate(cookies, 1):
            try:
                s = _session(cookie)
                uid, uname = _whoami(s)
                owner = uname or ("account #%d" % idx)
                created = _fetch_all_created_decals(s)
                print(f"[decal_db] {owner}: found {len(created)} decals, checking thumbnails...")
                thumbs = _fetch_thumbnails(s, [c["assetId"] for c in created])
                cdates = _fetch_created_dates(s, [c["assetId"] for c in created])
                acc_added = 0
                for c in created:
                    aid = c["assetId"]; prev = items.get(aid, {})
                    entry = {"assetId": aid, "name": c["name"],
                             "created": cdates.get(aid) or c["created"] or prev.get("created", ""),
                             "addedAt": prev.get("addedAt") or int(time.time()),
                             "owner": owner, "ownerId": uid or prev.get("ownerId", "")}
                    _classify(entry, thumbs.get(aid))
                    if aid not in items:
                        added += 1; acc_added += 1
                    items[aid] = entry
                accounts.append({"owner": owner, "ownerId": uid,
                                 "found": len(created), "newlyAdded": acc_added})
            except Exception as e:
                print("[decal_db] sync error (account #%d):" % idx, e)
                errors.append({"account": idx, "error": str(e)})
        _save_db(db)
        counts = {}
        for v in items.values():
            counts[v.get("status", "?")] = counts.get(v.get("status", "?"), 0) + 1
        return jsonify({"ok": True, "total": len(items), "newlyAdded": added,
                        "accountsSynced": len(accounts), "accounts": accounts,
                        "errors": errors, "counts": counts})

    @app.post("/decals/rescan")
    def decals_rescan():
        if not _admin_ok():
            return _deny()
        cookie = _cookie()
        if not cookie:
            return jsonify({"error": "ROBLOX_COOKIE is not set."}), 500
        try:
            db = _load_db()
            targets = [a for a, v in db["items"].items()
                       if v.get("status") in ("pending", "blank", "blocked", "error")]
            if not targets:
                return jsonify({"ok": True, "rescanned": 0, "total": len(db["items"])})
            thumbs = _fetch_thumbnails(_session(cookie), targets)
            for aid in targets:
                _classify(db["items"][aid], thumbs.get(aid))
            _save_db(db)
            return jsonify({"ok": True, "rescanned": len(targets), "total": len(db["items"])})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.post("/decals/recheck")
    def decals_recheck():
        """PUBLIC (no admin): re-check thumbnail status for freshly-uploaded /
        still-processing decals so the gallery updates live on refresh. The
        browser cannot call Roblox directly (CORS blocks thumbnails.roblox.com),
        so the check runs here on the server using the account cookie. Only
        touches unsettled items (pending/error) or explicit ids from the body,
        and is capped so a refresh stays fast."""
        cookie = _cookie()
        if not cookie:
            return jsonify({"error": "ROBLOX_COOKIE is not set."}), 500

        def _counts(items):
            c = {}
            for v in items.values():
                c[v.get("status", "?")] = c.get(v.get("status", "?"), 0) + 1
            return c

        body = request.get_json(silent=True) or {}
        want = [str(x).strip() for x in (body.get("ids") or []) if str(x).strip().isdigit()]
        try:
            db = _load_db()
            if want:
                targets = [aid for aid in want if aid in db["items"]]
            else:
                targets = [aid for aid, v in db["items"].items()
                           if v.get("status") in ("pending", "error", "")]
            targets = targets[:80]  # keep a page refresh fast
            if not targets:
                return jsonify({"ok": True, "rechecked": 0, "resolved": 0,
                                "total": len(db["items"]), "counts": _counts(db["items"])})
            thumbs = _fetch_thumbnails(_session(cookie), targets)
            resolved = 0
            for aid in targets:
                _classify(db["items"][aid], thumbs.get(aid))
                if db["items"][aid].get("status") != "pending":
                    resolved += 1
            _save_db(db)
            return jsonify({"ok": True, "rechecked": len(targets), "resolved": resolved,
                            "total": len(db["items"]), "counts": _counts(db["items"])})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.post("/decals/add")
    def decals_add():
        """Add one asset id (e.g. called automatically after a decal upload)."""
        if not _admin_ok():
            return _deny()
        cookie = _cookie()
        body = request.get_json(silent=True) or {}
        aid = str(body.get("assetId") or body.get("id") or "").strip()
        name = (body.get("name") or "").strip()
        if not aid.isdigit():
            return jsonify({"error": "invalid assetId"}), 400
        try:
            db = _load_db()
            entry = {"assetId": aid, "name": name or ("Decal " + aid),
                     "created": "", "addedAt": int(time.time())}
            if cookie:
                s = _session(cookie)
                uid, uname = _whoami(s)
                if uname:
                    entry["owner"] = uname; entry["ownerId"] = uid or ""
                thumbs = _fetch_thumbnails(s, [aid])
                _classify(entry, thumbs.get(aid))
            else:
                entry["status"] = "ok"
            db["items"][aid] = entry
            _save_db(db)
            return jsonify({"ok": True, "item": entry})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.get("/decals/export")
    def decals_export():
        # Full database backup (admin only) — download this BEFORE wiping/recreating Railway.
        if not _admin_ok():
            return _deny()
        db = _load_db()
        payload = json.dumps(db, ensure_ascii=False, indent=2)
        resp = Response(payload, mimetype="application/json")
        resp.headers["Content-Disposition"] = 'attachment; filename="decals_db_backup.json"'
        return resp

    @app.post("/decals/import")
    def decals_import():
        # Restore a backup produced by /decals/export (admin only).
        #   mode=merge   (default) -> keep existing entries and add/overwrite from the file.
        #   mode=replace           -> wipe the DB first, then load the file.
        if not _admin_ok():
            return _deny()
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "invalid JSON body"}), 400
        incoming = body.get("items")
        mode = (request.args.get("mode") or body.get("mode") or "merge").lower()
        norm = {}
        if isinstance(incoming, dict):
            for k, v in incoming.items():
                if not isinstance(v, dict):
                    continue
                aid = str(v.get("assetId") or k).strip()
                if aid:
                    norm[aid] = {**v, "assetId": aid}
        elif isinstance(incoming, list):
            for v in incoming:
                if isinstance(v, dict) and v.get("assetId"):
                    aid = str(v["assetId"]).strip()
                    norm[aid] = {**v, "assetId": aid}
        else:
            return jsonify({"error": "no 'items' found in the file"}), 400
        db = _load_db()
        if mode == "replace":
            db["items"] = {}
        added = 0
        for aid, entry in norm.items():
            if aid not in db["items"]:
                added += 1
            db["items"][aid] = entry
        _save_db(db)
        return jsonify({"ok": True, "mode": mode, "imported": len(norm),
                        "newlyAdded": added, "total": len(db["items"])})

    @app.get("/decals")
    def decals_list():
        db = _load_db()
        show_all = request.args.get("all") in ("1", "true", "yes")
        items = list(db["items"].values())
        if not show_all:
            items = [v for v in items if v.get("status") in ("ok", "pending", "blank", "error")]
        items.sort(key=lambda x: (x.get("created") or "", x.get("addedAt") or 0), reverse=True)
        return jsonify({"items": items, "updatedAt": db.get("updatedAt", 0),
                        "totalStored": len(db["items"]),
                        "adminRequired": bool(_admin_key())})

    @app.delete("/decals/<asset_id>")
    def decals_delete(asset_id):
        if not _admin_ok():
            return _deny()
        db = _load_db()
        if str(asset_id) in db["items"]:
            db["items"].pop(str(asset_id)); _save_db(db)
            return jsonify({"ok": True})
        return jsonify({"error": "not found"}), 404

    @app.get("/gallery")
    def decals_gallery():
        if os.path.exists(GALLERY_HTML):
            return send_file(GALLERY_HTML)
        return Response("decal_gallery.html not found (in production the gallery is served by Vercel at /decal).", status=404)

    print("[decal_db] addon active → /decals, /decals/sync, /decals/rescan, /decals/recheck, /decals/add, /decals/export, /decals/import, /gallery"
          + (" (admin-gated)" if _admin_key() else " (OPEN - set DECAL_ADMIN_KEY for public!)"))
