from flask import Flask, jsonify, request
from flask_cors import CORS
from ytmusicapi import YTMusic
import pykakasi
import os
import json
import time
import requests
import re
import difflib

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
        "version": "2026-06-06-0708",
        "features": ["romaji_particle_split", "upload_decal"],
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
    decal_name = (data.get("name") or "Karaoke Cover").strip()[:40]
    if not image_url:
        return jsonify({"error": "image_url required"}), 400

    try:
        img_resp = requests.get(image_url, timeout=20)
        if img_resp.status_code != 200:
            return jsonify({"error": f"Failed to download image: HTTP {img_resp.status_code}"}), 400

        content_type = img_resp.headers.get("Content-Type", "image/png").split(";")[0]
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
            "fileContent": ("cover.png", img_resp.content, content_type),
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
                        return jsonify({"decal_id": str(asset_id)})
                    return jsonify({"error": "Upload done but assetId missing"}), 500

        return jsonify({"error": "Timeout waiting for Roblox asset processing"}), 504

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

    queries = [query]
    romaji_q = _anime_romaji_query(query)
    if romaji_q:
        queries.append(romaji_q)

    try:
        results = []
        seen = set()
        last_err = ""
        for q in queries:
            items, err = _anisongdb_search(q)
            if err:
                last_err = err
            for it in items:
                key = it.get("annSongId")
                if key is None:
                    key = id(it)
                if key in seen:
                    continue
                seen.add(key)
                results.append(it)

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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
