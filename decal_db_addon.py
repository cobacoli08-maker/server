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
import collections
import threading
import tempfile
try:
    import fcntl  # POSIX inter-process lock (Linux / Railway)
except Exception:
    fcntl = None

# -- debug event log (in-memory ring buffer, exposed via GET /decals/log) --
_EVENTS = collections.deque(maxlen=400)
def _log(event, **data):
    rec = {"ts": int(time.time() * 1000), "event": event}
    rec.update(data)
    _EVENTS.append(rec)
    try:
        print("[decal_db] " + json.dumps(rec, ensure_ascii=False)[:1500])
    except Exception:
        print("[decal_db]", event, data)

_HERE = os.path.dirname(os.path.abspath(__file__))
_DIR = (os.environ.get("DECAL_DB_DIR", "").strip() or _HERE)
DB_PATH = os.path.join(_DIR, "decals_db.json")          # full database (internal)
PUBLIC_SNAPSHOT = os.path.join(_DIR, "decals.json")      # snapshot for static hosting
GALLERY_HTML = os.path.join(_HERE, "decal_gallery.html")

# -- concurrency safety (many mods hitting the shared backend at once) --------
# The whole DB is one JSON file. A naive read-modify-write loses data or reads a
# half-written file when an upload, a sync and a gallery refresh overlap. Every
# write goes through _mutate_db: it grabs an in-process lock AND a cross-process
# file lock, then writes atomically (temp file + os.replace). Slow Roblox calls
# are always done BEFORE the lock so the critical section stays tiny.
_DB_LOCK = threading.RLock()
_LOCK_PATH = DB_PATH + ".lock"
_RECHECK_LOCK = threading.Lock()
_RECHECK_STATE = {"ts": 0.0}
_DISCOVERY_COOLDOWN = 20  # seconds; refreshes inside this window skip Roblox calls


class _FileLock:
    def __init__(self, path):
        self.path = path
        self.fh = None

    def __enter__(self):
        if fcntl is None:
            return self
        try:
            self.fh = open(self.path, "w")
            fcntl.flock(self.fh, fcntl.LOCK_EX)
        except Exception:
            self.fh = None
        return self

    def __exit__(self, *a):
        if self.fh is not None:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_UN)
                self.fh.close()
            except Exception:
                pass
            self.fh = None


def _atomic_write_json(path, obj):
    d = os.path.dirname(path) or "."
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def _mutate_db(mutator):
    """Serialised, atomic read-modify-write. Do slow network work BEFORE this."""
    with _DB_LOCK:
        with _FileLock(_LOCK_PATH):
            db = _load_db()
            result = mutator(db)
            _save_db(db)
            return result

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
    _atomic_write_json(DB_PATH, db)
    ok = [v for v in db["items"].values() if v.get("status") in ("ok", "pending", "blank", "error")]
    ok.sort(key=lambda x: x.get("created") or "", reverse=True)
    try:
        _atomic_write_json(PUBLIC_SNAPSHOT, {"items": ok, "updatedAt": db["updatedAt"]})
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


def _fetch_all_created_decals(s, max_pages=200):
    """Pull all Decals created by the account (paginated) via the creations API.
    Pass max_pages=1 to fetch only the newest ~50 (fast; used by live refresh)."""
    items = []
    cursor = ""
    for _ in range(max_pages):  # safety cap
        params = {"assetType": "Decal", "isArchived": "false", "limit": 50, "sortOrder": "Desc"}
        if cursor:
            params["cursor"] = cursor
        r = s.get("https://itemconfiguration.roblox.com/v1/creations/get-assets",
                  params=params, timeout=30)
        _bo = 0
        while r.status_code == 429 and _bo < 3:
            _bo += 1
            _log("roblox.rate_limited", api="creations", tries=_bo)
            time.sleep(1.5 * _bo)
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
    """Immediately add a freshly-uploaded decal to the shared DB so it shows on
    /decal right away. Roblox calls run FIRST (no lock); the DB write is then a
    short, serialised, atomic read-modify-write so concurrent uploads/refreshes
    can never clobber each other."""
    aid = str(asset_id or "").strip()
    if not aid.isdigit():
        return None
    cookie = cookie or os.environ.get("ROBLOX_COOKIE", "").strip()
    net = {}
    try:
        if cookie:
            sess = _session(cookie)
            uid, uname = _whoami(sess)
            if uname:
                net["owner"] = uname
                net["ownerId"] = uid or ""
            cd = _fetch_created_dates(sess, [aid])
            if cd.get(aid):
                net["created"] = cd[aid]
            net["thumb"] = _fetch_thumbnails(sess, [aid]).get(aid)
            net["ok"] = True
        else:
            net["nocookie"] = True
    except Exception as e:
        print("[decal_db] register_uploaded_decal error:", e)
        net["err"] = str(e)

    def _apply(db):
        prev = db["items"].get(aid, {})
        entry = {"assetId": aid,
                 "name": (name or "").strip() or prev.get("name") or ("Decal " + aid),
                 "created": net.get("created") or prev.get("created", ""),
                 "addedAt": prev.get("addedAt") or int(time.time()),
                 "owner": net.get("owner") or prev.get("owner", ""),
                 "ownerId": net.get("ownerId") or prev.get("ownerId", "")}
        if net.get("ok"):
            _classify(entry, net.get("thumb"))
        elif net.get("nocookie"):
            entry.setdefault("status", "ok")
        else:
            entry.setdefault("status", "pending")
        db["items"][aid] = entry
        return entry

    entry = _mutate_db(_apply)
    _log("upload.register", assetId=aid, status=entry.get("status"),
         state=entry.get("state", ""), hasImage=bool(entry.get("imageUrl")),
         hasCookie=bool(cookie), name=entry.get("name"))
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
        _log("sync.start", cookies=len(cookies))
        fetched = {}          # aid -> entry (built from Roblox, no lock held)
        accounts = []
        errors = []
        for idx, cookie in enumerate(cookies, 1):
            try:
                s = _session(cookie)
                uid, uname = _whoami(s)
                owner = uname or ("account #%d" % idx)
                created = _fetch_all_created_decals(s)
                print("[decal_db] %s: found %d decals, checking thumbnails..." % (owner, len(created)))
                ids = [c["assetId"] for c in created]
                thumbs = _fetch_thumbnails(s, ids)
                cdates = _fetch_created_dates(s, ids)
                for c in created:
                    aid = c["assetId"]
                    entry = {"assetId": aid, "name": c["name"],
                             "created": cdates.get(aid) or c["created"] or "",
                             "owner": owner, "ownerId": uid or ""}
                    _classify(entry, thumbs.get(aid))
                    fetched[aid] = entry
                accounts.append({"owner": owner, "ownerId": uid or "",
                                 "found": len(created), "newlyAdded": 0})
            except Exception as e:
                print("[decal_db] sync error (account #%d):" % idx, e)
                errors.append({"account": idx, "error": str(e)})

        def _apply(db):
            items = db["items"]
            added = 0
            new_by_owner = {}
            for aid, entry in fetched.items():
                prev = items.get(aid, {})
                entry["addedAt"] = prev.get("addedAt") or int(time.time())
                if not entry.get("created"):
                    entry["created"] = prev.get("created", "")
                if aid not in items:
                    added += 1
                    ow = entry.get("ownerId") or ""
                    new_by_owner[ow] = new_by_owner.get(ow, 0) + 1
                items[aid] = entry
            return added, new_by_owner

        added, new_by_owner = _mutate_db(_apply)
        for acc in accounts:
            acc["newlyAdded"] = new_by_owner.get(acc.get("ownerId") or "", 0)
        db_after = _load_db()
        counts = {}
        for v in db_after["items"].values():
            counts[v.get("status", "?")] = counts.get(v.get("status", "?"), 0) + 1
        total = len(db_after["items"])
        _log("sync.done", total=total, newlyAdded=added,
             accounts=accounts, errors=errors, counts=counts)
        return jsonify({"ok": True, "total": total, "newlyAdded": added,
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
        """PUBLIC (no admin). Runs on every gallery Refresh. Two jobs the browser
        cannot do (Roblox blocks CORS): (1) DISCOVER brand-new uploads across ALL
        configured accounts by pulling each account newest page, and (2) RE-CHECK
        unsettled items so thumbnails resolve. To stay safe when many mods refresh
        at the same time, Roblox calls are throttled by a short cooldown and the
        DB write is a single serialised, atomic merge."""
        cookies = _cookies()
        if not cookies:
            _log("recheck.no_cookie")
            return jsonify({"error": "ROBLOX_COOKIE is not set."}), 500

        def _counts(items):
            c = {}
            for v in items.values():
                c[v.get("status", "?")] = c.get(v.get("status", "?"), 0) + 1
            return c

        body = request.get_json(silent=True) or {}
        want = [str(x).strip() for x in (body.get("ids") or []) if str(x).strip().isdigit()]
        now = time.time()
        with _RECHECK_LOCK:
            since = now - _RECHECK_STATE["ts"]
            in_cooldown = (not want) and _RECHECK_STATE["ts"] > 0 and since < _DISCOVERY_COOLDOWN
            if not in_cooldown:
                _RECHECK_STATE["ts"] = now
        if in_cooldown:
            db = _load_db()
            _log("recheck.cooldown", sinceSec=round(since, 1), coolSec=_DISCOVERY_COOLDOWN)
            return jsonify({"ok": True, "cooldown": True,
                            "cooldownRemaining": round(_DISCOVERY_COOLDOWN - since, 1),
                            "discovered": 0, "discoveredItems": [], "discoverError": None,
                            "rechecked": 0, "recheckReport": [], "resolved": 0,
                            "total": len(db["items"]), "counts": _counts(db["items"])})

        _log("recheck.start", wantIds=want, cookies=len(cookies))
        try:
            db = _load_db()
            existing = set(db["items"].keys())
            discovered = {}
            disc_error = None
            per_account = []
            for idx, cookie in enumerate(cookies, 1):
                try:
                    sess = _session(cookie)
                    latest = _fetch_all_created_decals(sess, max_pages=1)
                    new_ids = [c["assetId"] for c in latest
                               if c["assetId"] not in existing and c["assetId"] not in discovered]
                    acc_new = 0
                    if new_ids:
                        uid, uname = _whoami(sess)
                        dthumbs = _fetch_thumbnails(sess, new_ids)
                        dcdates = _fetch_created_dates(sess, new_ids)
                        for c in latest:
                            aid = c["assetId"]
                            if aid in existing or aid in discovered:
                                continue
                            entry = {"assetId": aid, "name": c["name"],
                                     "created": dcdates.get(aid) or c["created"] or "",
                                     "addedAt": int(now),
                                     "owner": uname or "", "ownerId": uid or ""}
                            _classify(entry, dthumbs.get(aid))
                            discovered[aid] = entry
                            acc_new += 1
                    per_account.append({"account": idx, "latestPage": len(latest), "newCount": acc_new})
                except Exception as de:
                    disc_error = str(de)
                    _log("recheck.discover_error", account=idx, error=disc_error)
            _log("recheck.discovered", accounts=per_account, newCount=len(discovered),
                 newItems=[{"assetId": a, "status": e.get("status"), "state": e.get("state", "")}
                           for a, e in discovered.items()])

            if want:
                targets = [a for a in want if a in existing or a in discovered]
            else:
                targets = [aid for aid, v in db["items"].items()
                           if v.get("status") in ("pending", "error", "")]
            targets = targets[:80]
            thumbs = _fetch_thumbnails(_session(cookies[0]), targets) if targets else {}

            def _apply(fresh):
                items = fresh["items"]
                for aid, entry in discovered.items():
                    if aid not in items:
                        items[aid] = entry
                rep_list = []
                res = 0
                for aid in targets:
                    if aid not in items:
                        continue
                    before = items[aid].get("status")
                    _classify(items[aid], thumbs.get(aid))
                    after = items[aid].get("status")
                    th = thumbs.get(aid) or {}
                    rep_list.append({"assetId": aid, "before": before, "after": after,
                                     "state": th.get("state", ""), "hasImage": bool(th.get("imageUrl"))})
                    if after != "pending":
                        res += 1
                return {"report": rep_list, "resolved": res,
                        "total": len(items), "counts": _counts(items)}

            out = _mutate_db(_apply)
            _log("recheck.done", discovered=len(discovered), rechecked=len(targets),
                 resolved=out["resolved"], counts=out["counts"])
            return jsonify({"ok": True, "discovered": len(discovered),
                            "discoveredItems": [{"assetId": a, "status": e.get("status"),
                                                 "state": e.get("state", "")}
                                                for a, e in discovered.items()],
                            "discoverError": disc_error, "rechecked": len(targets),
                            "recheckReport": out["report"], "resolved": out["resolved"],
                            "total": out["total"], "counts": out["counts"]})
        except Exception as e:
            _log("recheck.error", error=str(e))
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

    @app.get("/decals/log")
    def decals_log():
        """PUBLIC: recent raw server events (upload/sync/recheck/list) as JSON."""
        try:
            limit = int(request.args.get("limit") or 120)
        except Exception:
            limit = 120
        limit = max(1, min(limit, 400))
        evs = list(_EVENTS)[-limit:]
        return jsonify({"events": evs, "count": len(evs), "now": int(time.time() * 1000)})

    @app.get("/decals/diag")
    def decals_diag():
        """PUBLIC: config + DB snapshot for debugging. Never exposes cookie values."""
        db = _load_db()
        items = list(db["items"].values())
        counts = {}
        for v in items:
            counts[v.get("status", "?")] = counts.get(v.get("status", "?"), 0) + 1
        items_sorted = sorted(items, key=lambda x: (x.get("created") or "", x.get("addedAt") or 0), reverse=True)
        sample = [{"assetId": v.get("assetId"), "status": v.get("status"),
                   "state": v.get("state", ""), "hasImage": bool(v.get("imageUrl")),
                   "created": v.get("created", ""), "addedAt": v.get("addedAt", 0),
                   "owner": v.get("owner", "")} for v in items_sorted[:15]]
        return jsonify({"ok": True, "cookiesConfigured": len(_cookies()),
                        "adminKeySet": bool(_admin_key()), "dbPath": DB_PATH, "dbDir": _DIR,
                        "totalStored": len(items), "counts": counts,
                        "updatedAt": db.get("updatedAt", 0), "latest": sample})

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
        _log("list", showAll=show_all, returned=len(items), totalStored=len(db["items"]))
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

    print("[decal_db] addon active → /decals, /decals/sync, /decals/rescan, /decals/recheck, /decals/add, /decals/log, /decals/diag, /decals/export, /decals/import, /gallery"
          + (" (admin-gated)" if _admin_key() else " (OPEN - set DECAL_ADMIN_KEY for public!)"))
