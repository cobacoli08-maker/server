from flask import Flask, jsonify, request
from flask_cors import CORS
from ytmusicapi import YTMusic
import pykakasi
import os

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


def clean_title(title):
    return (title or "Unknown").split(" - ")[0].split(" (")[0].strip()


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "karaoke-metadata"})


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

        romaji_title = text_to_romaji(raw_title_ja)
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
