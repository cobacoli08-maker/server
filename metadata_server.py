from flask import Flask, jsonify, request
from flask_cors import CORS
from ytmusicapi import YTMusic
import pykakasi
import os
import json
import time
import requests
import re

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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
