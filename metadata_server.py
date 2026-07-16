from flask import Flask, jsonify, request
from flask_cors import CORS
from ytmusicapi import YTMusic
import pykakasi
import os
import json
import base64
import time
import requests
import re
import difflib

# ── .env loader (for LOCAL TESTING) ─────────────────────────────────────
# If there is a `.env` file in the same folder, read KEY=VALUE into os.environ.
# Existing env (e.g. Railway Variables) is NOT overwritten -> production keeps using Railway.
def _load_dotenv():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
        print("\u2705 .env loaded (local test)")
    except Exception as e:
        print("\u26a0\ufe0f  failed to read .env:", e)

_load_dotenv()

app = Flask(__name__)
CORS(app)

ytmusic_ja = YTMusic(language="ja")
ytmusic_en = YTMusic(language="en")

kks = pykakasi.kakasi()
kks.setMode("J", "a")
kks.setMode("H", "a")
kks.setMode("K", "a")
converter = kks.getConverter()


def text_to_romaji(text):
    if not text:
        return ""
    return converter.do(text).title()


def text_to_romaji_spaced(text):
    if not text:
        return ""
    particle_re = re.compile(r"(から|まで|より|だけ|って|とは|では|には|でも|[とのにはへをがでやかも])")
    try:
        chunks = [p for p in particle_re.split(text) if p]
        parts = []
        for chunk in chunks:
            for item in kks.convert(chunk):
                romaji = item.get("hepburn") or item.get("kunrei") or item.get("passport") or item.get("orig") or ""
                romaji = romaji.strip()
                if romaji:
                    parts.append(romaji)
        if parts:
            return " ".join(parts).title()
    except Exception:
        pass
    return text_to_romaji(text)


def clean_title(title):
    return (title or "Unknown").split(" - ")[0].split(" (")[0].strip()


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "karaoke-metadata"})


@app.get("/version")
def version():
    return jsonify({
        "version": "2026-07-13-mod",
        "features": ["romaji_particle_split", "upload_decal", "custom_image_decal", "yt_lyrics"],
    })


@app.post("/cari_metadata")
def cari_metadata():
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "Search query empty"}), 400

    try:
        res_ja = ytmusic_ja.search(query, filter="songs", limit=1)
        res_en = ytmusic_en.search(query, filter="songs", limit=1)

        if not res_ja:
            return jsonify({"error": "Song not found on YT Music"}), 404

        track_ja = res_ja[0]
        track_en = res_en[0] if res_en else track_ja

        raw_title_full = track_ja.get("title", "Unknown")
        title_parts = [p.strip() for p in raw_title_full.split(" - ")]
        raw_title_ja = clean_title(title_parts[0])

        english_title = ""
        if len(title_parts) > 1:
            english_title = clean_title(title_parts[1])
        else:
            raw_title_en = clean_title(track_en.get("title", "Unknown"))
            if raw_title_en.lower() != raw_title_ja.lower():
                english_title = raw_title_en

        artists_ja = track_ja.get("artists", [])
        artists_en = track_en.get("artists", [])
        raw_artist_ja = artists_ja[0]["name"] if artists_ja else "Unknown Artist"
        raw_artist_en = artists_en[0]["name"] if artists_en else raw_artist_ja

        raw_album = track_ja.get("album", {}).get("name", "Single") if track_ja.get("album") else "Single"
        video_id = track_ja.get("videoId", "")

        romaji_title = text_to_romaji_spaced(raw_title_ja)
        if english_title and english_title.lower() not in {romaji_title.lower(), raw_title_ja.lower()}:
            title_combined = f"{raw_title_ja} ({romaji_title} / {english_title})"
        elif raw_title_ja != romaji_title:
            title_combined = f"{raw_title_ja} ({romaji_title})"
        else:
            title_combined = raw_title_ja

        if raw_artist_ja != raw_artist_en:
            artist_combined = raw_artist_en
        else:
            romaji_artist = text_to_romaji(raw_artist_ja)
            artist_combined = romaji_artist if raw_artist_ja != romaji_artist else raw_artist_ja

        release_year = track_ja.get("year")
        if not release_year and video_id:
            try:
                song_detail = ytmusic_ja.get_song(video_id)
                release_year = (
                    song_detail.get("microformat", {})
                    .get("microformatDataRenderer", {})
                    .get("uploadDate", "")[:4]
                ) or None
            except Exception:
                pass

        if not release_year:
            try:
                album_browse_id = track_ja.get("album", {}).get("id")
                if album_browse_id:
                    album_detail = ytmusic_ja.get_album(album_browse_id)
                    release_year = str(album_detail.get("year", "")) or None
            except Exception:
                pass

        thumbnails = track_ja.get("thumbnails", [])
        cover_url = ""
        if thumbnails:
            raw_url = thumbnails[-1]["url"]
            cover_url = (raw_url.split("=")[0] + "=w1024-h1024") if "=" in raw_url else raw_url

        return jsonify({
            "title_kanji": raw_title_ja,
            "title_romaji": romaji_title,
            "title_combined": title_combined,
            "artist_kanji": raw_artist_ja,
            "artist_romaji": artist_combined,
            "artist_combined": artist_combined,
            "album_name": raw_album,
            "year": release_year or "Unknown",
            "cover_url": cover_url,
        })

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/upload_decal")
def upload_decal():
    roblox_cookie = os.environ.get("ROBLOX_COOKIE", "").strip()
    if not roblox_cookie:
        return jsonify({"error": "ROBLOX_COOKIE env is not set"}), 500

    data = request.get_json(silent=True) or {}
    image_url = (data.get("image_url") or "").strip()
    image_b64 = (data.get("image_base64") or "").strip()
    decal_name = (data.get("name") or "Karaoke Cover").strip()[:40]
    if not image_url and not image_b64:
        return jsonify({"error": "image_url or image_base64 required"}), 400

    try:
        if image_b64:
            if image_b64.startswith("data:") and "," in image_b64:
                image_b64 = image_b64.split(",", 1)[1]
            try:
                img_bytes = base64.b64decode(image_b64)
            except Exception as dec_exc:
                return jsonify({"error": f"Invalid image_base64: {dec_exc}"}), 400
            content_type = (data.get("content_type") or "image/png").split(";")[0]
            file_name = (data.get("filename") or "cover.png")
        else:
            img_resp = requests.get(image_url, timeout=20)
            if img_resp.status_code != 200:
                return jsonify({"error": f"Failed to download image: HTTP {img_resp.status_code}"}), 400
            content_type = img_resp.headers.get("Content-Type", "image/png").split(";")[0]
            img_bytes = img_resp.content
            file_name = "cover.png"

        session = requests.Session()
        session.cookies[".ROBLOSECURITY"] = roblox_cookie

        csrf_resp = session.post("https://auth.roblox.com/v2/logout")
        csrf_token = csrf_resp.headers.get("x-csrf-token", "")
        if not csrf_token:
            return jsonify({"error": "Failed to get Roblox CSRF token"}), 500

        user_resp = session.get("https://users.roblox.com/v1/users/authenticated", timeout=15)
        user_id = (user_resp.json() if user_resp.ok else {}).get("id")
        if not user_id:
            return jsonify({"error": "ROBLOX_COOKIE invalid or expired"}), 500

        req_payload = {
            "assetType": "Decal",
            "displayName": decal_name,
            "description": "Karaoke Cover",
            "creationContext": {"creator": {"userId": str(user_id)}},
        }
        files = {
            "request": ("", json.dumps(req_payload), "application/json"),
            "fileContent": (file_name, img_bytes, content_type),
        }
        up_resp = session.post(
            "https://apis.roblox.com/assets/user-auth/v1/assets",
            headers={"X-CSRF-TOKEN": csrf_token},
            files=files,
            timeout=30,
        )
        if up_resp.status_code not in (200, 201):
            return jsonify({"error": f"Roblox API HTTP {up_resp.status_code}", "raw": up_resp.text[:300]}), 500

        op_id = up_resp.json().get("operationId")
        if not op_id:
            return jsonify({"error": "Upload response has no operationId"}), 500

        for _ in range(12):
            time.sleep(2)
            poll_resp = session.get(
                f"https://apis.roblox.com/assets/user-auth/v1/operations/{op_id}",
                headers={"X-CSRF-TOKEN": csrf_token},
                timeout=15,
            )
            if poll_resp.status_code == 200:
                poll_data = poll_resp.json()
                if poll_data.get("done"):
                    asset_id = poll_data.get("response", {}).get("assetId")
                    if asset_id:
                        try:
                            import decal_db_addon
                            decal_db_addon.register_uploaded_decal(asset_id, decal_name)
                        except Exception as _reg_e:
                            print("[upload_decal] auto-register to DB failed:", _reg_e)
                        return jsonify({"decal_id": str(asset_id)})
                    return jsonify({"error": "Upload done but assetId missing"}), 500

        return jsonify({"error": "Timeout waiting for Roblox asset processing"}), 504

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ───────────────────────────────────────────────
# YT LYRIC SYNC: YouTube Music lyrics -> enhanced (per-token timed) REXT
# YT-only. Line-level YT timings are expanded to per-token estimates.
# ───────────────────────────────────────────────
MORA_PACE = 0.34
READING_OVERRIDES = {"君": "きみ", "日": "ひ", "傍": "そば", "何処": "どこ", "何時": "いつ"}
_SMALL_KANA = set("ぁぃぅぇぉゃゅょゎァィゥェォャュョヮ")


def _mora_count(reading):
    if not reading:
        return 0
    n = 0
    for ch in reading:
        if ch in _SMALL_KANA:
            continue
        n += 1
    return max(1, n)


def _mora_count_text(text):
    total = 0
    try:
        for it in kks.convert(text or ""):
            total += _mora_count(it.get("hira") or it.get("kana") or "")
    except Exception:
        total = len(text or "")
    return max(1, total)


def _fmt_time(sec):
    if sec is None or sec < 0:
        sec = 0
    m = int(sec // 60)
    s = sec - m * 60
    return f"{m:02d}:{s:05.2f}"


def _has_kanji(s):
    return any("\u4e00" <= ch <= "\u9fff" for ch in (s or ""))


def _reading_of(orig, hira):
    if orig in READING_OVERRIDES:
        return READING_OVERRIDES[orig]
    return hira or ""


def _enhanced_line(text, start, end, track=1):
    text = (text or "").strip()
    if not text:
        return None
    segs = []
    try:
        for it in kks.convert(text):
            orig = it.get("orig") or ""
            hira = it.get("hira") or it.get("kana") or ""
            if not orig.strip():
                segs.append({"text": orig, "ruby": "", "w": 0})
                continue
            reading = _reading_of(orig, hira)
            w = _mora_count(reading) if reading else max(1, len(orig))
            ruby = reading if (_has_kanji(orig) and reading and reading != orig) else ""
            segs.append({"text": orig, "ruby": ruby, "w": w})
    except Exception:
        segs = [{"text": text, "ruby": "", "w": max(1, len(text))}]
    total_w = sum(s["w"] for s in segs) or 1
    if end and end > start:
        span = end - start
    else:
        span = total_w * MORA_PACE
    span = max(0.05, span)
    natural = total_w * MORA_PACE
    eff = min(span, natural) if natural > 0 else span
    out = f"[{_fmt_time(start)}][T:{track}]"
    cum = 0
    for s in segs:
        ts = start + (cum / total_w) * eff
        cum += s["w"]
        out += f"<{_fmt_time(ts)}>{s['text']}"
        if s["ruby"]:
            out += f"[{s['ruby']}]"
    out += " /"
    return out


def _yt_lyric_lines(raw):
    parsed = []
    for ll in raw:
        if isinstance(ll, dict):
            txt = ll.get("text") or ""
            st = ll.get("start_time")
            en = ll.get("end_time")
        else:
            txt = getattr(ll, "text", "") or ""
            st = getattr(ll, "start_time", None)
            en = getattr(ll, "end_time", None)
        if st is not None and st > 1000:
            st = st / 1000.0
        if en is not None and en > 1000:
            en = en / 1000.0
        parsed.append({"text": txt, "st": st, "en": en})
    return parsed


@app.post("/yt_lyrics")
def yt_lyrics():
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    video_id = (data.get("videoId") or data.get("video_id") or "").strip()
    if not query and not video_id:
        return jsonify({"error": "query or videoId required"}), 400
    try:
        yt = ytmusic_ja
        if not video_id:
            results = yt.search(query, filter="songs") or []
            if not results:
                results = yt.search(query) or []
            for r in results:
                if isinstance(r, dict) and r.get("videoId"):
                    video_id = r["videoId"]
                    break
            if not video_id:
                return jsonify({"error": "No YouTube Music result for query"}), 404
        watch = yt.get_watch_playlist(videoId=video_id)
        lyrics_browse = watch.get("lyrics") if isinstance(watch, dict) else None
        if not lyrics_browse:
            return jsonify({"error": "No lyrics available for this song on YT Music", "videoId": video_id}), 404
        timed_supported = True
        try:
            lyr = yt.get_lyrics(lyrics_browse, timestamps=True)
        except TypeError:
            lyr = yt.get_lyrics(lyrics_browse)
            timed_supported = False
        lyric_data = lyr.get("lyrics") if isinstance(lyr, dict) else None
        source = (lyr.get("source") if isinstance(lyr, dict) else "") or ""
        rext_lines = []
        has_time = False
        if isinstance(lyric_data, list):
            parsed = _yt_lyric_lines(lyric_data)
            for i, p in enumerate(parsed):
                txt = (p["text"] or "").strip()
                if not txt:
                    continue
                if p["st"] is None:
                    continue
                has_time = True
                st = float(p["st"])
                en = p["en"]
                if en is None:
                    nxt = None
                    for j in range(i + 1, len(parsed)):
                        if parsed[j]["st"] is not None:
                            nxt = float(parsed[j]["st"])
                            break
                    en = nxt if nxt is not None else st + max(1.0, _mora_count_text(txt) * MORA_PACE)
                line = _enhanced_line(txt, st, float(en))
                if line:
                    rext_lines.append(line)
        elif isinstance(lyric_data, str):
            t = 0.0
            for raw in lyric_data.split("\n"):
                raw = raw.strip()
                if not raw:
                    t += 0.6
                    continue
                dur = max(1.0, _mora_count_text(raw) * MORA_PACE)
                line = _enhanced_line(raw, t, t + dur)
                if line:
                    rext_lines.append(line)
                t += dur + 0.25
        if not rext_lines:
            return jsonify({"error": "Lyrics found but could not be parsed into synced lines", "videoId": video_id}), 422
        return jsonify({
            "rext": "\n".join(rext_lines),
            "timed": bool(has_time and timed_supported),
            "line_count": len(rext_lines),
            "videoId": video_id,
            "source": source,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ───────────────────────────────────────────────
# ANIME HUNTER: song title -> full anime info
# AnimeThemes (song<->anime mapping) enriched with AniList (rich details)
# ───────────────────────────────────────────────
ANIMETHEMES_BASE = "https://api.animethemes.moe"
ANILIST_URL = "https://graphql.anilist.co"

ANILIST_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id
    title { romaji english native }
    synonyms
    description(asHtml: false)
    episodes
    duration
    format
    season
    seasonYear
    startDate { year }
    averageScore
    genres
    studios(isMain: true) { nodes { name } }
    coverImage { extraLarge large }
    siteUrl
  }
}
"""


ANIMETHEMES_HEADERS = {"User-Agent": "karaoke-metadata/1.0"}


def _anime_norm(text):
    if not text:
        return ""
    return re.sub(r"[^0-9a-z\u3040-\u30ff\u4e00-\u9fff]", "", text.lower())


def _anime_romaji_query(query):
    try:
        rq = text_to_romaji_spaced(query)
        if rq and _anime_norm(rq) != _anime_norm(query):
            return rq
    except Exception:
        pass
    return ""


def _anime_str_score(a, b):
    a, b = _anime_norm(a), _anime_norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.80 + 0.15 * (min(len(a), len(b)) / max(len(a), len(b)))
    return difflib.SequenceMatcher(None, a, b).ratio() * 0.75


def _anime_theme_score(theme, queries):
    anime = theme.get("anime") or {}
    song = theme.get("song") or {}
    targets = [song.get("title", ""), anime.get("name", "")]
    best = 0.0
    for q in queries:
        for t in targets:
            score = _anime_str_score(q, t)
            if score > best:
                best = score
    return best


def _animethemes_search(query):
    params = {
        "q": query,
        "fields[search]": "animethemes",
        "include[animetheme]": "anime,song,song.artists",
    }
    try:
        r = requests.get(
            f"{ANIMETHEMES_BASE}/search",
            params=params,
            headers=ANIMETHEMES_HEADERS,
            timeout=20,
        )
        if r.status_code != 200:
            return [], f"HTTP {r.status_code}: {r.text[:200]}"
        themes = ((r.json() or {}).get("search") or {}).get("animethemes") or []
        return themes, ""
    except Exception as exc:
        return [], str(exc)


ANISONGDB_BASE = "https://anisongdb.com/api"


def _anisongdb_search(query):
    body = {
        "and_logic": False,
        "ignore_duplicate": False,
        "opening_filter": True,
        "ending_filter": True,
        "insert_filter": True,
        "normal_broadcast": True,
        "dub": True,
        "rebroadcast": True,
        "standard": True,
        "instrumental": True,
        "chanting": True,
        "character": True,
        "anime_search_filter": {"search": query, "partial_match": True},
        "song_name_search_filter": {"search": query, "partial_match": True},
        "artist_search_filter": {"search": query, "partial_match": True},
    }
    try:
        r = requests.post(
            f"{ANISONGDB_BASE}/search_request",
            json=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=25,
        )
        if r.status_code != 200:
            return [], f"HTTP {r.status_code}: {r.text[:200]}"
        data = r.json()
        if isinstance(data, list):
            return data, ""
        return [], "unexpected response shape"
    except Exception as exc:
        return [], str(exc)


def _anisongdb_search_pair(artist_part, song_part):
    """Intersection search: artist contains artist_part AND song contains song_part.
    Used when the user types "artist + song" (e.g. "sakura tange arigatou"),
    which no single AnisongDB field contains as a whole."""
    body = {
        "and_logic": True,
        "ignore_duplicate": False,
        "opening_filter": True,
        "ending_filter": True,
        "insert_filter": True,
        "normal_broadcast": True,
        "dub": True,
        "rebroadcast": True,
        "standard": True,
        "instrumental": True,
        "chanting": True,
        "character": True,
        "artist_search_filter": {"search": artist_part, "partial_match": True},
        "song_name_search_filter": {"search": song_part, "partial_match": True},
    }
    try:
        r = requests.post(
            f"{ANISONGDB_BASE}/search_request",
            json=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=25,
        )
        if r.status_code != 200:
            return [], f"HTTP {r.status_code}: {r.text[:200]}"
        data = r.json()
        if isinstance(data, list):
            return data, ""
        return [], "unexpected response shape"
    except Exception as exc:
        return [], str(exc)


def _anisong_targets(item):
    targets = [
        item.get("songName", ""),
        item.get("animeENName", ""),
        item.get("animeJPName", ""),
    ]
    for alt in (item.get("animeAltName") or []):
        targets.append(alt)
    art = _anisong_artists(item)
    if art:
        targets.append(art)
    return targets


def _anisong_score(item, queries):
    best = 0.0
    for q in queries:
        for t in _anisong_targets(item):
            s = _anime_str_score(q, t)
            if s > best:
                best = s
    return best


def _anisong_type_short(song_type):
    st = (song_type or "").strip()
    low = st.lower()
    if low.startswith("opening"):
        return ("OP" + st[len("opening"):].strip()).strip()
    if low.startswith("ending"):
        return ("ED" + st[len("ending"):].strip()).strip()
    return st


def _anisong_artists(item):
    if item.get("songArtist"):
        return item.get("songArtist")
    names = []
    for a in (item.get("artists") or []):
        nm = a.get("names") or []
        if nm:
            names.append(nm[0])
    return ", ".join(names)


def _anilist_media_by_id(anilist_id):
    if not anilist_id:
        return {}
    try:
        r = requests.post(
            ANILIST_URL,
            json={"query": ANILIST_QUERY, "variables": {"id": int(anilist_id)}},
            timeout=20,
        )
        if r.status_code != 200:
            return {}
        return ((r.json() or {}).get("data") or {}).get("Media") or {}
    except Exception:
        return {}


def _build_anime_from_anisong(item):
    linked = item.get("linked_ids") or {}
    anilist_id = linked.get("anilist")
    mal_id = linked.get("myanimelist")

    info = {
        "anime_title": item.get("animeENName", "") or item.get("animeJPName", ""),
        "anime_title_english": "",
        "anime_title_native": item.get("animeJPName", ""),
        "synonyms": item.get("animeAltName") or [],
        "song_title": item.get("songName", ""),
        "song_artists": _anisong_artists(item),
        "theme_type": _anisong_type_short(item.get("songType", "")),
        "song_type_full": item.get("songType", ""),
        "episodes": None,
        "cover_url": "",
        "anilist_url": (f"https://anilist.co/anime/{anilist_id}" if anilist_id else ""),
        "mal_url": (f"https://myanimelist.net/anime/{mal_id}" if mal_id else ""),
        "vintage": item.get("animeVintage", ""),
    }

    media = _anilist_media_by_id(anilist_id)
    if media:
        titles = media.get("title") or {}
        if titles.get("romaji"):
            info["anime_title"] = titles.get("romaji")
        info["anime_title_english"] = titles.get("english") or ""
        if titles.get("native"):
            info["anime_title_native"] = titles.get("native")
        if media.get("synonyms"):
            info["synonyms"] = media.get("synonyms")
        info["episodes"] = media.get("episodes")
        cover = media.get("coverImage") or {}
        info["cover_url"] = cover.get("extraLarge") or cover.get("large") or ""
        if media.get("siteUrl"):
            info["anilist_url"] = media.get("siteUrl")

    return info


def _anime_fetch_detail(slug):
    if not slug:
        return {}
    try:
        resp = requests.get(
            f"{ANIMETHEMES_BASE}/anime/{slug}",
            params={"include": "images,resources"},
            headers=ANIMETHEMES_HEADERS,
            timeout=20,
        )
        if resp.status_code == 200:
            return (resp.json() or {}).get("anime") or {}
    except Exception:
        pass
    return {}


def _anime_pick_image(images):
    if not images:
        return ""
    for facet in ("Large Cover", "Small Cover"):
        for img in images:
            if img.get("facet") == facet and img.get("link"):
                return img["link"]
    return images[0].get("link", "")


def _anime_anilist_id(resources):
    for res in resources or []:
        if (res.get("site") or "").lower() == "anilist" and res.get("external_id"):
            return res.get("external_id"), res.get("link", "")
    return None, ""


def _anime_mal_url(resources):
    for res in resources or []:
        if (res.get("site") or "").lower() == "myanimelist":
            return res.get("link", "")
    return ""


def _anime_strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _anime_fetch_anilist(anilist_id):
    try:
        resp = requests.post(
            ANILIST_URL,
            json={"query": ANILIST_QUERY, "variables": {"id": int(anilist_id)}},
            timeout=20,
        )
        if resp.status_code == 200:
            return (resp.json().get("data") or {}).get("Media")
    except Exception:
        pass
    return None


def _build_anime_info(theme):
    anime = theme.get("anime") or {}
    song = theme.get("song") or {}
    artists = song.get("artists") or []
    artist_names = ", ".join(a.get("name", "") for a in artists if a.get("name"))
    theme_type = theme.get("type") or ""
    seq = theme.get("sequence")
    theme_slug = theme.get("slug") or (f"{theme_type}{seq}" if seq else theme_type)

    detail = _anime_fetch_detail(anime.get("slug"))
    images = detail.get("images") or anime.get("images") or []
    resources = detail.get("resources") or anime.get("resources") or []
    anilist_id, anilist_url = _anime_anilist_id(resources)
    mal_url = _anime_mal_url(resources)

    info = {
        "anime_title": anime.get("name", ""),
        "anime_title_english": "",
        "anime_title_native": "",
        "synonyms": [],
        "anime_slug": anime.get("slug", ""),
        "song_title": song.get("title", ""),
        "song_artists": artist_names,
        "theme_type": theme_slug,
        "year": anime.get("year"),
        "season": anime.get("season", ""),
        "format": anime.get("media_format", ""),
        "episodes": None,
        "duration": None,
        "genres": [],
        "studios": [],
        "score": None,
        "synopsis": _anime_strip_html(detail.get("synopsis") or anime.get("synopsis", "")),
        "cover_url": _anime_pick_image(images),
        "anilist_url": anilist_url,
        "mal_url": mal_url,
        "animethemes_url": f"https://animethemes.moe/anime/{anime.get('slug', '')}",
    }

    if anilist_id:
        media = _anime_fetch_anilist(anilist_id)
        if media:
            titles = media.get("title") or {}
            info["anime_title"] = titles.get("romaji") or info["anime_title"]
            info["anime_title_english"] = titles.get("english") or ""
            info["anime_title_native"] = titles.get("native") or ""
            info["synonyms"] = media.get("synonyms") or []
            info["episodes"] = media.get("episodes")
            info["duration"] = media.get("duration")
            info["format"] = media.get("format") or info["format"]
            season_val = media.get("season")
            if isinstance(season_val, str) and season_val:
                info["season"] = season_val.title()
            info["year"] = media.get("seasonYear") or (media.get("startDate") or {}).get("year") or info["year"]
            info["score"] = media.get("averageScore")
            info["genres"] = media.get("genres") or []
            info["studios"] = [n.get("name") for n in ((media.get("studios") or {}).get("nodes") or []) if n.get("name")]
            if not info["synopsis"]:
                info["synopsis"] = _anime_strip_html(media.get("description", ""))
            info["anilist_url"] = media.get("siteUrl") or info["anilist_url"]
            cover = media.get("coverImage") or {}
            info["cover_url"] = cover.get("extraLarge") or cover.get("large") or info["cover_url"]

    parts = [p for p in [info["anime_title"], info["anime_title_english"]] if p]
    if len(parts) == 2 and parts[0].lower() != parts[1].lower():
        info["anime_title_combined"] = f"{parts[0]} ({parts[1]})"
    else:
        info["anime_title_combined"] = info["anime_title"]
    return info


@app.post("/cari_anime")
def cari_anime():
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "Search query empty"}), 400

    # Detect an explicit "A - B" separator (e.g. "artist - song" or "song - artist").
    dash_parts = None
    halves = re.split(r"\s+-\s+", query, maxsplit=1)
    if len(halves) == 2 and halves[0].strip() and halves[1].strip():
        dash_parts = (halves[0].strip(), halves[1].strip())

    romaji_q = _anime_romaji_query(query)

    # Strings used for fuzzy scoring (original + romaji of each meaningful part).
    queries = []
    for t in (list(dash_parts) if dash_parts else [query]):
        if t and t not in queries:
            queries.append(t)
        rq = _anime_romaji_query(t)
        if rq and rq not in queries:
            queries.append(rq)

    try:
        results = []
        seen = set()
        last_err = ""

        def _collect(found, err):
            nonlocal last_err
            if err:
                last_err = err
            for it in found:
                key = it.get("annSongId")
                if key is None:
                    key = id(it)
                if key in seen:
                    continue
                seen.add(key)
                results.append(it)

        if dash_parts:
            left, right = dash_parts
            # Try both orientations so order ("artist - song" or "song - artist")
            # doesn't matter, plus romaji variants of each side.
            variants = []
            for a, b in ((left, right), (right, left)):
                variants.append((a, b))
                ra, rb = _anime_romaji_query(a), _anime_romaji_query(b)
                if ra and rb and (ra, rb) not in variants:
                    variants.append((ra, rb))
            for a, b in variants:
                _collect(*_anisongdb_search_pair(a, b))
            # If the dash pairing found nothing, fall back to a plain search of each side.
            if not results:
                for t in queries:
                    _collect(*_anisongdb_search(t))
        else:
            for q in queries:
                _collect(*_anisongdb_search(q))
            # Fallback: user typed "artist song" without a dash. Split the words
            # and try each boundary as an artist/song intersection search.
            if not results:
                for q in queries:
                    tokens = q.split()
                    if len(tokens) < 2 or len(tokens) > 5:
                        continue
                    for i in range(1, len(tokens)):
                        l = " ".join(tokens[:i])
                        r = " ".join(tokens[i:])
                        _collect(*_anisongdb_search_pair(l, r))
                        _collect(*_anisongdb_search_pair(r, l))
                    if results:
                        break

        if not results:
            if last_err:
                return jsonify({"error": "AnisongDB request failed", "detail": last_err}), 502
            return jsonify({"error": "Song or anime not found"}), 404

        # Rank every candidate by fuzzy similarity to the query (kanji + romaji)
        ranked = sorted(
            results,
            key=lambda it: _anisong_score(it, queries),
            reverse=True,
        )

        best = _build_anime_from_anisong(ranked[0])
        best["match_score"] = round(_anisong_score(ranked[0], queries), 3)
        best["query_romaji"] = romaji_q

        candidates = []
        for it in ranked[1:8]:
            candidates.append({
                "anime_title": it.get("animeENName", "") or it.get("animeJPName", ""),
                "song_title": it.get("songName", ""),
                "theme_type": _anisong_type_short(it.get("songType", "")),
                "artist": _anisong_artists(it),
                "score": round(_anisong_score(it, queries), 3),
            })

        best["candidates"] = candidates
        return jsonify(best)

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── AI auto-pool: stateless multi-provider fallback ("OmniRoute-lite") ──
# Replaces the stateful OmniRoute hosting. ALL config via ENV, so:
#   host down -> new host -> paste the same ENV -> works again immediately.
# Each provider is OpenAI-compatible /chat/completions. A provider is SKIPPED if
# its ENV key is empty. Tried in order (top = priority); first success wins.
AI_TIMEOUT_SEC = int(os.environ.get("AI_TIMEOUT_SEC", "25"))

# Order = priority. Groq & Cerebras at the TOP because they are the fastest for AI Space.
# Gemini/NVIDIA/OpenRouter are backups if the two fast ones are rate-limited/down.
AI_PROVIDERS = [
    {"name": "groq",       "base": "https://api.groq.com/openai/v1/chat/completions",
     "key_envs": ["GROQ_API_KEY"],       "model_env": "GROQ_MODEL",       "default_models": ["qwen3.6-27b"]},
    # --- cerebras DISABLED (bad AI Space output). Re-enable = uncomment these 2 lines. ---
    # {"name": "cerebras",   "base": "https://api.cerebras.ai/v1/chat/completions",
    #  "key_envs": ["CEREBRAS_API_KEY"],   "model_env": "CEREBRAS_MODEL",   "default_models": ["gpt-oss-120b", "zai-glm-4.7", "gemma-4-31b"]},
    {"name": "gemini",     "base": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
     "key_envs": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],     "model_env": "GEMINI_MODEL",     "default_models": ["gemini-2.0-flash"]},
    {"name": "nvidia",     "base": "https://integrate.api.nvidia.com/v1/chat/completions",
     "key_envs": ["NVIDIA_API_KEY"],     "model_env": "NVIDIA_MODEL",     "default_models": ["qwen/qwen2.5-coder-32b-instruct"]},
    {"name": "openrouter", "base": "https://openrouter.ai/api/v1/chat/completions",
     "key_envs": ["OPENROUTER_API_KEY"], "model_env": "OPENROUTER_MODEL", "default_models": ["google/gemini-2.0-flash-exp:free"]},
]

_AI_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


def _clean_ai_output(text):
    """Strip the reasoning block <think>...</think> from thinking models (qwen/glm)."""
    if not text:
        return text
    text = _AI_THINK_RE.sub("", text)
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[-1]
    if "<think>" in text.lower():
        return ""  # reasoning truncated -> treat as failure, roll to next provider
    return text.strip()


@app.route("/ai", methods=["POST"])
def ai_generate():
    """AI Auto pool. Body: {system, user, temperature?, max_tokens?}. Returns {content, provider, model}."""
    data = request.get_json(silent=True) or {}
    system_prompt = data.get("system") or data.get("systemPrompt") or ""
    user_prompt = data.get("user") or data.get("userPrompt") or ""
    temperature = data.get("temperature", 0.1)
    if not user_prompt:
        return jsonify({"error": "user prompt is empty"}), 400

    errors = []
    tried = 0
    req_max_tokens = int(data.get("max_tokens") or os.environ.get("AI_MAX_TOKENS") or 4096)
    for p in AI_PROVIDERS:
        key = ""
        for env_name in p["key_envs"]:
            key = (os.environ.get(env_name) or "").strip()
            if key:
                break
        if not key:
            continue
        env_models = (os.environ.get(p["model_env"]) or "").strip()
        models = [m.strip() for m in (env_models.split(",") if env_models else list(p["default_models"])) if m.strip()]
        for model in models:
            tried += 1
            sys_p = system_prompt
            extra = {}
            is_thinking = any(t in model.lower() for t in ("qwen", "glm", "gpt-oss", "deepseek", "-r1"))
            if is_thinking:
                sys_p = (system_prompt or "") + "\n\n/no_think\nIMPORTANT: Output ONLY the final answer. No reasoning, no <think> tags."
                if p["name"] == "groq" and "qwen" in model.lower():
                    extra["reasoning_effort"] = "none"
            try:
                body = {
                    "model": model,
                    "temperature": temperature,
                    "max_tokens": req_max_tokens,
                    "messages": [
                        {"role": "system", "content": sys_p},
                        {"role": "user", "content": user_prompt},
                    ],
                }
                body.update(extra)
                r = requests.post(
                    p["base"],
                    headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
                    json=body,
                    timeout=AI_TIMEOUT_SEC,
                )
                if r.status_code != 200:
                    errors.append("%s[%s] HTTP %s: %s" % (p["name"], model, r.status_code, r.text[:150]))
                    continue
                content = _clean_ai_output(r.json()["choices"][0]["message"]["content"])
                if content:
                    return jsonify({"content": content, "provider": p["name"], "model": model})
                errors.append("%s[%s]: empty/reasoning-only" % (p["name"], model))
            except Exception as exc:
                errors.append("%s[%s]: %s: %s" % (p["name"], model, type(exc).__name__, exc))

    if tried == 0:
        return jsonify({"error": "AI Auto is not active yet: no provider key set in the server ENV. "
                                 "Set at least one of GEMINI_API_KEY / GROQ_API_KEY / CEREBRAS_API_KEY / "
                                 "NVIDIA_API_KEY / OPENROUTER_API_KEY on Railway."}), 503
    return jsonify({"error": "All AI providers failed", "detail": errors}), 502


# ── YT Lyric Sync add-on (ported 1:1 from the local build) ─────────────
# Adds /search_songs, /fetch_lyrics, /web_lyrics, /sync_lyrics — the exact
# working YT timed-lyric + kanji-sync system used by the local editor.
try:
    import lyric_sync_addon
    lyric_sync_addon.register(app, ytmusic_ja)
    print("✅ lyric_sync_addon registered (/search_songs, /fetch_lyrics, /web_lyrics, /sync_lyrics)")
except Exception as _e:
    print("⚠️ lyric_sync_addon not loaded:", _e)


# ── Decal Database add-on ────────────────────────────────────
# Stores all decals uploaded by the shared account + a public gallery at /decal
# (Vercel). The cookie is read from ENV ROBLOX_COOKIE. Set DECAL_ADMIN_KEY so
# sync/delete are admin-only. Requires pillow (listed in metadata_requirements.txt).
try:
    import decal_db_addon
    decal_db_addon.register(app)
    print("✅ decal_db_addon registered (/decals, /decals/sync, /decals/rescan, /gallery)")
except Exception as _e:
    print("⚠️ decal_db_addon not loaded:", _e)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
