# ══════════════════════════════════════════════════════════════════════
#  LYRIC SYNC ADD-ON v3  —  complete timed lyrics + kanji sync (linebreak-safe)
# ----------------------------------------------------------------------
#  Endpoints (added to bot_server.py Flask :5000):
#    POST /search_songs  { query }                 -> preview list to pick from
#    POST /fetch_lyrics  { query | videoId | url }  -> timed lines + debug
#    POST /sync_lyrics   { ytLines, refLines, track }
#
#  Timestamp sources tried in order (see debug.sourceTrail):
#    1) yt-dlp subtitles (creator captions .ja / then auto) — most complete
#    2) YouTube Music timed lyrics (tolerant manual parse; survives 'cueRange')
#    3) LRCLIB synced lyrics (cleaned queries + /get + /search)
#    4) YouTube Music plain lyrics (no timestamps -> estimated)
#
#  Wire-up in bot_server.py:
#      import lyric_sync_addon
#      lyric_sync_addon.register(app, ytmusic_ja)
# ══════════════════════════════════════════════════════════════════════
import os
import re
import glob
import json
import shutil
import difflib
import tempfile
import traceback
import subprocess
import html
import unicodedata
import urllib.parse
import urllib.request

from flask import request, jsonify
import pykakasi

_furi = pykakasi.kakasi()

# ── char helpers ────────────────────────────────────────────────────
_KANJI_RE = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002a6df々〆ヶ]')
SMALL_KANA = set('ぁぃぅぇぉゃゅょゎゕゖァィゥェォャュョヮ')
PUNCT_MERGE = set('！!？?。、,.…‥・♪ーﾞﾟ゛゜”"゙゚')


def _has_kanji(s):
    return bool(_KANJI_RE.search(s or ''))


def _is_kana(ch):
    return ('\u3040' <= ch <= '\u309f') or ('\u30a0' <= ch <= '\u30ff') or ch == 'ー'


def _fmt(sec):
    if sec is None or sec < 0:
        sec = 0.0
    m = int(sec // 60)
    s = sec - m * 60
    return f"{m:02d}:{s:05.2f}"


_VID_RE = re.compile(
    r'(?:v=|/watch\?v=|youtu\.be/|/embed/|/shorts/|music\.youtube\.com/watch\?v=)([A-Za-z0-9_-]{11})')


def _extract_video_id(s):
    s = (s or '').strip()
    if not s:
        return ''
    m = _VID_RE.search(s)
    if m:
        return m.group(1)
    if re.fullmatch(r'[A-Za-z0-9_-]{11}', s):
        return s
    return ''


def _dur_to_sec(d):
    if isinstance(d, (int, float)):
        return int(d)
    if isinstance(d, str) and ':' in d:
        try:
            parts = [int(x) for x in d.split(':')]
            sec = 0
            for p in parts:
                sec = sec * 60 + p
            return sec
        except Exception:
            return None
    return None


# ── furigana ─────────────────────────────────────────────────────
def _ruby_item(orig, hira):
    if not orig or not _has_kanji(orig) or not hira or hira == orig:
        return orig
    lead = ''
    i = 0
    while i < len(orig) and _is_kana(orig[i]):
        lead += orig[i]
        i += 1
    trail = ''
    j = len(orig)
    while j > i and _is_kana(orig[j - 1]):
        trail = orig[j - 1] + trail
        j -= 1
    core = orig[i:j]
    if not core:
        return orig
    reading = hira
    if lead and reading.startswith(lead):
        reading = reading[len(lead):]
    if trail and reading.endswith(trail):
        reading = reading[:len(reading) - len(trail)]
    if not reading:
        return orig
    return f"{lead}{core}[{reading}]{trail}"


def _split_units(ruby_str):
    units = []
    pending_tsu = ''
    i, n = 0, len(ruby_str)
    while i < n:
        ch = ruby_str[i]
        if ch == ' ':
            i += 1
            continue
        if ch == '[':
            j = ruby_str.find(']', i)
            if j == -1:
                j = n - 1
            if units:
                units[-1] += ruby_str[i:j + 1]
            i = j + 1
            continue
        k = i
        while k < n and ruby_str[k] not in ' [':
            k += 1
        base = ruby_str[i:k]
        if k < n and ruby_str[k] == '[':
            j = ruby_str.find(']', k)
            if j == -1:
                j = n - 1
            units.append(pending_tsu + ruby_str[i:j + 1])
            pending_tsu = ''
            i = j + 1
            continue
        for ch2 in base:
            if ch2 in ('っ', 'ッ'):
                pending_tsu += ch2
                continue
            if (ch2 in SMALL_KANA or ch2 == 'ー' or ch2 in PUNCT_MERGE) and units:
                units[-1] += ch2
                continue
            units.append(pending_tsu + ch2)
            pending_tsu = ''
        i = k
    if pending_tsu:
        if units:
            units[-1] += pending_tsu
        else:
            units.append(pending_tsu)
    return units


# ── timing + reading tuning ──────────────────────────────────
# "Natural" per-mora pace (seconds). Used so morae are NOT smeared evenly
# across the whole inter-line gap. Main bug: short line + long instrumental gap
# make each mora stretch 1-2 seconds. Higher = slower, lower = faster.
MORA_PACE = 0.34

# Override kanji readings pykakasi often mis-guesses in lyric context.
# key = exact surface token from pykakasi, value = the correct hiragana.
# Add your own if you find others.
READING_OVERRIDES = {
    '君': 'きみ',    # default 'くん' -> in lyrics almost always 'きみ'
    '日': 'ひ',      # standalone 日 -> ひ (今日/明日 are tokenized separately, safe)
    '傍': 'そば',    # default 'ぼう'
    '何処': 'どこ',
    '何時': 'いつ',
}


def _mora_count(s):
    """Rough mora count: small-kana + ー attach to the previous mora (no increment),
    everything else counts as 1 mora per char."""
    c = 0
    for ch in s:
        if ch in SMALL_KANA or ch == 'ー':
            continue
        c += 1
    return max(1, c)


_LATIN_RE = re.compile(r"^[A-Za-z0-9'’\-]+$")
_PUNCT_ONLY = set('（）()「」『』、。,.!?！？…‥・♪~〜ー—-"\'’“” 　')


def _unit_weight(u):
    """Duration weight per unit. Kanji+furigana = reading length (mora),
    kana = 1, romaji = sub-mora (~0.35/char), punctuation = light."""
    if '[' in u and u.endswith(']'):
        reading = u[u.index('[') + 1:-1]
        return float(_mora_count(reading))
    b = u.strip()
    if not b:
        return 0.12
    if all(ch in _PUNCT_ONLY for ch in b):
        return 0.12
    if _LATIN_RE.match(b):
        return max(0.3, 0.35 * len(b))
    return float(_mora_count(b))


def convert_line(text, start, end, track=2):
    text = (text or '').strip()
    if not text:
        return None
    try:
        items = _furi.convert(text)
    except Exception:
        items = [{'orig': text, 'hira': text}]
    units = []
    started = False
    for it in items:
        orig = it.get('orig', '')
        hira = READING_OVERRIDES.get(orig, it.get('hira', ''))
        ruby = _ruby_item(orig, hira)
        sub = _split_units(ruby)
        for idx, u in enumerate(sub):
            units.append((u, idx == 0 and started))
        if sub:
            started = True
    if not units:
        return None
    if start is None:
        start = 0.0
    weights = [_unit_weight(u) for (u, sp) in units]
    total_w = sum(weights) or 1.0
    # "natural" duration if sung normally
    natural = total_w * MORA_PACE
    # span from source (end = start of next line). If None/weird -> natural.
    span = (end - start) if (end is not None and end > start) else natural
    # KEY FIX: do not smear into long gaps. If the gap is far larger than the
    # natural duration, clamp to natural (the rest becomes an instrumental gap, morae don't
    # stretch). If it's a fast part (span < natural), still fit to span.
    eff = min(span, natural)
    out = [f"[{_fmt(start)}][T:{track}]"]
    acc = 0.0
    for (u, sp), w in zip(units, weights):
        t = start + eff * (acc / total_w)
        out.append((' ' if sp else '') + f"<{_fmt(t)}>{u}")
        acc += w
    # Terminator ' /' is NOT added here — the editor (buildLRC) adds it
    # automatically on export; doubling -> '//' on import.
    return ''.join(out)


# ── normalisation + linebreak-safe matching ───────────────────────────
_NORM_STRIP = re.compile(r'\[[^\]]*\]')
# keep only meaningful chars (kana / kanji / latin / digits). ALL punctuation,
# quotes (straight + curly), spaces and symbols are dropped, so differences in
# line breaks, quote style, or spacing can never block a match.
_NORM_KEEP = re.compile(
    r'[0-9A-Za-z\u3040-\u309f\u30a0-\u30ff\u30fc\u3005\u3006\u30f6'
    r'\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002a6df]')


def _norm(s):
    if not s:
        return ''
    s = _NORM_STRIP.sub('', s)
    s = unicodedata.normalize('NFKC', s)
    return ''.join(_NORM_KEEP.findall(s)).lower()


def _ratio(a, b):
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _ref_concat(ref_lines):
    """Concatenate normalised ref text; keep (char_offset, time) index so we can
    look up a project timestamp for ANY substring regardless of line breaks."""
    buf = ''
    idx = []  # (start_char, time)
    for r in ref_lines:
        nt = _norm(r.get('text', ''))
        if not nt:
            continue
        idx.append((len(buf), r.get('time')))
        buf += nt
    return buf, idx


def _time_at(pos, idx):
    t = None
    for start_char, tm in idx:
        if start_char <= pos:
            t = tm
        else:
            break
    return t


def _build_anchors(yt_lines, ref_lines, thr=0.82):
    buf, idx = _ref_concat(ref_lines)
    ref_norm = [_norm(r.get('text', '')) for r in ref_lines]
    matches = []  # (matched, ratio, ref_time)
    raw_anchors = []
    for y in yt_lines:
        yn = _norm(y.get('text', ''))
        st = y.get('start')
        matched, ratio, rt = False, 0.0, None
        if len(yn) >= 2 and buf:
            pos = buf.find(yn)
            if pos >= 0:                       # exact substring -> linebreak-proof
                matched, ratio, rt = True, 1.0, _time_at(pos, idx)
            else:
                # longest contiguous block inside concatenated project text.
                # catches near-identical lines with a few different characters
                # (typos, kana/kanji variants) that the exact find() misses.
                sm = difflib.SequenceMatcher(None, yn, buf, autojunk=False)
                mb = sm.find_longest_match(0, len(yn), 0, len(buf))
                cov = (mb.size / len(yn)) if yn else 0.0
                if cov >= 0.72:
                    matched = True
                    ratio = round(cov, 3)
                    rt = _time_at(max(0, mb.b - mb.a), idx)
                else:                          # fuzzy per-line fallback
                    bi, br = -1, 0.0
                    for j, rn in enumerate(ref_norm):
                        if len(rn) < 2:
                            continue
                        r = _ratio(yn, rn)
                        if r > br:
                            br, bi = r, j
                    if br >= thr:
                        matched, ratio, rt = True, br, ref_lines[bi].get('time')
        matches.append((matched, ratio, rt))
        if matched and st is not None and rt is not None and len(yn) >= 3:
            raw_anchors.append((st, rt, ratio))
    raw_anchors.sort(key=lambda x: x[0])
    anchors, last = [], None
    for st, rt, br in raw_anchors:
        if last is None or rt >= last - 0.05:
            anchors.append((st, rt))
            last = rt
    return anchors, matches


def _make_mapper(anchors):
    if not anchors:
        return (lambda t: t), 0.0
    if len(anchors) == 1:
        off = anchors[0][1] - anchors[0][0]
        return (lambda t: (t + off) if t is not None else None), off
    xs = [a[0] for a in anchors]
    ys = [a[1] for a in anchors]

    def mapper(t):
        if t is None:
            return None
        if t <= xs[0]:
            return t + (ys[0] - xs[0])
        if t >= xs[-1]:
            return t + (ys[-1] - xs[-1])
        for i in range(len(xs) - 1):
            if xs[i] <= t <= xs[i + 1]:
                dx = xs[i + 1] - xs[i]
                if dx <= 1e-6:
                    return t + (ys[i] - xs[i])
                f = (t - xs[i]) / dx
                return ys[i] + f * (ys[i + 1] - ys[i])
        return t

    med = sorted(y - x for x, y in anchors)[len(anchors) // 2]
    return mapper, med


# ── SOURCE 1: yt-dlp subtitles ─────────────────────────────────────
def _ts_to_sec(ts):
    ts = ts.replace(',', '.')
    parts = ts.split(':')
    parts = [float(p) for p in parts]
    s = 0.0
    for p in parts:
        s = s * 60 + p
    return s


def _parse_vtt(text):
    lines = []
    for block in re.split(r'\n\s*\n', text):
        m = re.search(r'(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}|\d{1,2}:\d{2}[.,]\d{1,3})\s*--?>\s*'
                      r'(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}|\d{1,2}:\d{2}[.,]\d{1,3})', block)
        if not m:
            continue
        st, en = _ts_to_sec(m.group(1)), _ts_to_sec(m.group(2))
        txts = []
        for ln in block.split('\n'):
            if '-->' in ln or ln.strip().upper().startswith('WEBVTT') or re.fullmatch(r'\d+', ln.strip()):
                continue
            ln = re.sub(r'<[^>]+>', '', ln).strip()
            if ln:
                txts.append(ln)
        txt = ' '.join(txts).strip()
        if txt:
            lines.append({'start': st, 'end': en, 'text': txt})
    # de-dup consecutive identical (auto-caption rolling repeats)
    out = []
    for l in lines:
        if out and out[-1]['text'] == l['text']:
            out[-1]['end'] = l['end']
            continue
        out.append(l)
    return out


def _ytdlp_subs(video_id, dbg):
    if not shutil.which('yt-dlp'):
        dbg['sourceTrail'].append('yt-dlp not installed — skip')
        return [], None
    url = 'https://www.youtube.com/watch?v=' + video_id
    for auto in (False, True):
        d = tempfile.mkdtemp(prefix='yls_')
        try:
            cmd = ['yt-dlp', '--skip-download', '--sub-langs', 'ja.*,ja,ja-JP',
                   '--sub-format', 'vtt', '-o', os.path.join(d, '%(id)s.%(ext)s'), url]
            cmd.insert(1, '--write-auto-subs' if auto else '--write-subs')
            subprocess.run(cmd, capture_output=True, timeout=90)
            files = sorted(glob.glob(os.path.join(d, '*.vtt')))
            if not files:
                continue
            with open(files[0], 'r', encoding='utf-8', errors='ignore') as f:
                parsed = _parse_vtt(f.read())
            if parsed:
                kind = 'yt-dlp auto-caption' if auto else 'yt-dlp subtitle'
                dbg['sourceTrail'].append(f'{kind} OK ({len(parsed)} lines)')
                return parsed, kind
        except Exception as e:
            dbg['sourceTrail'].append(f'yt-dlp {"auto" if auto else "manual"} failed: {type(e).__name__}: {e}')
        finally:
            shutil.rmtree(d, ignore_errors=True)
    dbg['sourceTrail'].append('yt-dlp: no ja subtitles found')
    return [], None


# ── SOURCE 2: YT Music timed (tolerant, survives 'cueRange') ───────────────
def _walk_timed(obj, out):
    if isinstance(obj, dict):
        line = obj.get('lyricLine')
        if isinstance(line, str) and line.strip():
            cue = obj.get('cueRange') if isinstance(obj.get('cueRange'), dict) else {}
            st = cue.get('startTimeMilliseconds')
            en = cue.get('endTimeMilliseconds')
            try:
                st = float(st) / 1000.0 if st is not None else None
            except Exception:
                st = None
            try:
                en = float(en) / 1000.0 if en is not None else None
            except Exception:
                en = None
            out.append({'text': line.strip(), 'start': st, 'end': en})
        for v in obj.values():
            _walk_timed(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_timed(v, out)


def _ytmusic_timed(ytmusic, video_id, dbg):
    try:
        wp = ytmusic.get_watch_playlist(video_id)
    except Exception as e:
        dbg['sourceTrail'].append(f'get_watch_playlist failed: {type(e).__name__}: {e}')
        return [], False, None
    browse = wp.get('lyrics') if isinstance(wp, dict) else None
    tracks = wp.get('tracks') if isinstance(wp, dict) else None
    if tracks:
        t0 = tracks[0]
        arts = t0.get('artists') or []
        dbg['song'] = {'title': t0.get('title', ''),
                       'artist': (arts[0]['name'] if arts else ''),
                       'duration': t0.get('length')}
    if not browse:
        dbg['sourceTrail'].append('YTMusic: no lyrics browseId')
        return [], False, None
    # official timed
    try:
        data = ytmusic.get_lyrics(browse, timestamps=True)
        raw = data.get('lyrics') if isinstance(data, dict) else None
        if isinstance(raw, list):
            out = []
            for seg in raw:
                if isinstance(seg, dict):
                    txt, st, en = seg.get('text', ''), seg.get('start_time'), seg.get('end_time')
                else:
                    txt = getattr(seg, 'text', '')
                    st = getattr(seg, 'start_time', None)
                    en = getattr(seg, 'end_time', None)
                if txt and txt.strip():
                    out.append({'text': txt.strip(),
                                'start': (st / 1000.0) if isinstance(st, (int, float)) else None,
                                'end': (en / 1000.0) if isinstance(en, (int, float)) else None})
            if out and any(l['start'] is not None for l in out):
                dbg['sourceTrail'].append(f'YTMusic timed OK ({len(out)} lines)')
                return out, True, (data.get('source') if isinstance(data, dict) else 'YT Music')
    except Exception as e:
        dbg['sourceTrail'].append(f'YTMusic timed failed: {type(e).__name__}: {e} — trying manual parse')
        # tolerant manual parse of the raw browse response
        try:
            send = getattr(ytmusic, '_send_request', None)
            if send:
                resp = send('browse', {'browseId': browse})
                found = []
                _walk_timed(resp, found)
                found = [f for f in found if f['text']]
                if found and any(f['start'] is not None for f in found):
                    dbg['sourceTrail'].append(f'YTMusic timed (manual) OK ({len(found)} lines)')
                    return found, True, 'YT Music (manual)'
                elif found:
                    dbg['sourceTrail'].append(f'YTMusic manual got {len(found)} lines but no times')
        except Exception as e2:
            dbg['sourceTrail'].append(f'YTMusic manual parse failed: {type(e2).__name__}: {e2}')
    # plain
    try:
        data = ytmusic.get_lyrics(browse)
        raw = data.get('lyrics') if isinstance(data, dict) else None
        if isinstance(raw, str) and raw.strip():
            out = [{'text': ln.strip(), 'start': None, 'end': None}
                   for ln in raw.split('\n') if ln.strip()]
            dbg['sourceTrail'].append(f'YTMusic plain OK ({len(out)} lines, no timestamps)')
            return out, False, 'YT Music (plain)'
    except Exception as e:
        dbg['sourceTrail'].append(f'YTMusic plain failed: {type(e).__name__}: {e}')
    return [], False, None


# ── SOURCE 3: LRCLIB ──────────────────────────────────────────────
_LRC_TS = re.compile(r'\[(\d+):(\d+(?:\.\d+)?)\]')


def _parse_lrc(lrc):
    lines = []
    for raw in (lrc or '').split('\n'):
        ts = list(_LRC_TS.finditer(raw))
        if not ts:
            continue
        text = _LRC_TS.sub('', raw).strip()
        if not text:
            continue
        for m in ts:
            t = int(m.group(1)) * 60 + float(m.group(2))
            lines.append({'start': t, 'text': text})
    lines.sort(key=lambda x: x['start'])
    for i in range(len(lines)):
        lines[i]['end'] = lines[i + 1]['start'] if i + 1 < len(lines) else lines[i]['start'] + 3.0
    return lines


def _clean_title(t):
    t = t or ''
    t = t.split('/')[0]
    t = re.sub(r'feat\.?.*$', '', t, flags=re.I)
    t = re.sub(r'\(.*?\)|（.*?）|\[.*?\]', '', t)
    return t.strip()


def _clean_artist(a):
    a = a or ''
    a = re.split(r'[（(]', a)[0]
    a = a.split('/')[0]
    a = re.sub(r'feat\.?.*$', '', a, flags=re.I)
    return a.strip()


def _http_json(url):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'karaoke-editor lyric-sync-addon (https://github.com/local)'})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode('utf-8'))


def _lrclib(title, artist, duration, dbg):
    ct, ca = _clean_title(title), _clean_artist(artist)
    tried = []
    # 1) exact /get
    if ct and ca:
        p = {'track_name': ct, 'artist_name': ca}
        if duration:
            p['duration'] = int(duration)
        try:
            d = _http_json('https://lrclib.net/api/get?' + urllib.parse.urlencode(p))
            if isinstance(d, dict) and d.get('syncedLyrics'):
                dbg['lrclib'] = {'via': '/get', 'matched': f"{d.get('artistName')} - {d.get('trackName')}"}
                return _parse_lrc(d['syncedLyrics'])
            tried.append('/get: no synced')
        except Exception as e:
            tried.append(f'/get: {type(e).__name__}')
    # 2) /search variants
    variants = []
    if ct and ca:
        variants.append({'track_name': ct, 'artist_name': ca})
    if ct:
        variants.append({'q': f'{ct} {ca}'.strip()})
        variants.append({'q': ct})
    for p in variants:
        try:
            d = _http_json('https://lrclib.net/api/search?' + urllib.parse.urlencode(p))
            if isinstance(d, list) and d:
                best = None
                for it in d:
                    if it.get('syncedLyrics'):
                        if duration and it.get('duration') and abs(it['duration'] - duration) > 10:
                            continue
                        best = it
                        break
                if not best:
                    best = next((it for it in d if it.get('syncedLyrics')), None)
                if best:
                    dbg['lrclib'] = {'via': 'search ' + json.dumps(p, ensure_ascii=False),
                                     'matched': f"{best.get('artistName')} - {best.get('trackName')}"}
                    return _parse_lrc(best['syncedLyrics'])
                tried.append(f'search {p}: no synced')
            else:
                tried.append(f'search {p}: 0 hits')
        except Exception as e:
            tried.append(f'search {p}: {type(e).__name__}')
    dbg['sourceTrail'].append('LRCLIB miss (' + '; '.join(tried) + ')')
    return []


# ═ endpoints ══════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════
#  WEB LYRIC SCRAPING v5  —  fast HTTP scrape of dedicated lyric sites.
#  Rationale: sung/rap ad-libs that YT captions & YTMusic replace with
#  placeholders like (....) / (...) / ∩( ´∀`)∩ DO exist on some lyric
#  sites (e.g. letras.com). This is plain HTTP GET + regex parse, so it
#  returns in well under a second — unlike Whisper ASR.
#  Sources tried: pasted URL -> letras.com -> uta-net -> j-lyric.
# ----------------------------------------------------------------------
_BROWSER_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
               '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36')
_TAG_RE = re.compile(r'(?is)<[^>]+>')
_JP_RE = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\u3400-\u4dbf\u4e00-\u9fff]')


def _has_jp(s):
    return bool(_JP_RE.search(s or ''))


def _is_gap(t):
    """A YT line is a placeholder gap if it carries no Japanese at all
    (e.g. '(....)', '(...)', '∩( ´∀`)∩ hey there!'). Lines with kana/kanji
    inside parens are treated as real content."""
    t = (t or '').strip()
    if not t:
        return True
    return not _has_jp(t)


def _http_text(url, timeout=15):
    req = urllib.request.Request(url, headers={
        'User-Agent': _BROWSER_UA, 'Accept-Language': 'ja,en;q=0.8'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        ctype = r.headers.get('Content-Type', '') or ''
    enc = 'utf-8'
    m = re.search(r'charset=([\w-]+)', ctype, re.I)
    if m:
        enc = m.group(1)
    try:
        return raw.decode(enc, 'replace')
    except LookupError:
        return raw.decode('utf-8', 'replace')


def _http_json2(url, timeout=15):
    return json.loads(_http_text(url, timeout))


def _html_to_lines(raw_html):
    h = raw_html or ''
    h = re.sub(r'(?is)<rt[^>]*>.*?</rt>', '', h)        # drop furigana ruby text
    h = re.sub(r'(?is)<rp[^>]*>.*?</rp>', '', h)
    h = re.sub(r'(?is)<\s*br\s*/?\s*>', '\n', h)
    h = re.sub(r'(?is)</\s*(p|div|li)\s*>', '\n', h)
    h = _TAG_RE.sub('', h)
    h = html.unescape(h)
    out = []
    for ln in h.split('\n'):
        ln = ln.replace('\u3000', ' ').strip()
        if ln:
            out.append(ln)
    return out


def _letras_search(query, dbg):
    trail = dbg.setdefault('web', {}).setdefault('trail', [])
    try:
        u = 'https://solr.sscdn.co/letras/m1/?' + urllib.parse.urlencode(
            {'q': query, 'wt': 'json'})
        d = _http_json2(u)
        docs = (((d or {}).get('response') or {}).get('docs')) or []
        for doc in docs:
            path = (doc.get('url') or '').strip('/')
            if path and '/' in path:
                dns = doc.get('dns') or 'www.letras.com'
                return 'https://%s/%s/' % (dns, path)
        trail.append('letras search: 0 docs')
    except Exception as e:
        trail.append('letras search: %s' % type(e).__name__)
    return None


def _scrape_letras(url, dbg):
    s = _http_text(url)
    m = re.search(r'(?is)<div[^>]*class="[^"]*lyric-original[^"]*"[^>]*>(.*?)'
                  r'</div>\s*(?:<div[^>]*class="[^"]*(?:lyric-translation|'
                  r'letra-trad|cnt-trad))', s)
    if not m:
        m = re.search(r'(?is)<div[^>]*class="[^"]*lyric-original[^"]*"[^>]*>'
                      r'(.*?)</div>', s)
    frag = m.group(1) if m else ''
    return [ln for ln in _html_to_lines(frag) if _has_jp(ln)]


def _scrape_utanet(query, dbg):
    s = _http_text('https://www.uta-net.com/search/?' + urllib.parse.urlencode(
        {'Keyword': query, 'Aselect': '2', 'Bselect': '3'}))
    m = re.search(r'/song/(\d+)/', s)
    if not m:
        return []
    sp = _http_text('https://www.uta-net.com/song/%s/' % m.group(1))
    mm = re.search(r'(?is)<div[^>]*id="kashi_area"[^>]*>(.*?)</div>', sp)
    if not mm:
        return []
    return [ln for ln in _html_to_lines(mm.group(1)) if _has_jp(ln)]


def _scrape_jlyric(title, artist, dbg):
    s = _http_text('http://search.j-lyric.net/index.php?' + urllib.parse.urlencode(
        {'kt': title, 'ct': '2', 'ca': artist or '', 'cl': '0'}))
    m = re.search(r'(https?://j-lyric\.net/artist/[^"\']+\.html)', s)
    if not m:
        return []
    sp = _http_text(m.group(1))
    mm = re.search(r'(?is)<p[^>]*id="Lyric"[^>]*>(.*?)</p>', sp)
    if not mm:
        return []
    return [ln for ln in _html_to_lines(mm.group(1)) if _has_jp(ln)]


def _scrape_any(url, dbg):
    u = (url or '').lower()
    trail = dbg.setdefault('web', {}).setdefault('trail', [])
    # IMPORTANT: YouTube / non-lyric URLs are REJECTED. Old bug: user pastes a
    # youtu.be link -> generic scrape grabs random Japanese text from the YT page ->
    # 6 junk lines end up in the project. Now only lyric sites are scraped.
    if 'youtube.com' in u or 'youtu.be' in u or 'music.youtube' in u:
        trail.append('scrape: YouTube URL rejected (not a lyric site)')
        return []
    if 'letras.com' in u or 'letras.mus.br' in u:
        return _scrape_letras(url, dbg)
    if 'uta-net.com' in u:
        sp = _http_text(url)
        mm = re.search(r'(?is)<div[^>]*id="kashi_area"[^>]*>(.*?)</div>', sp)
        return [ln for ln in _html_to_lines(mm.group(1)) if _has_jp(ln)] if mm else []
    if 'j-lyric.net' in u:
        sp = _http_text(url)
        mm = re.search(r'(?is)<p[^>]*id="Lyric"[^>]*>(.*?)</p>', sp)
        return [ln for ln in _html_to_lines(mm.group(1)) if _has_jp(ln)] if mm else []
    trail.append('scrape: domain not supported (only letras/uta-net/j-lyric)')
    return []


def _web_lyrics(title, artist, query, url, pasted, dbg):
    trail = dbg.setdefault('web', {}).setdefault('trail', [])
    q = (query or ('%s %s' % (title, artist))).strip()
    # SOURCE 1 (most reliable): lyrics the user pasted themselves from the web.
    # Needs no internet on the bot side & cannot fetch the wrong thing.
    if pasted:
        plines = [ln.replace('\u3000', ' ').strip()
                  for ln in pasted.replace('\r', '\n').split('\n')]
        plines = [ln for ln in plines if ln and _has_jp(ln)]
        trail.append('pasted: %d JP lines' % len(plines))
        if plines:
            return plines, 'pasted'
    tasks = []
    if url:
        tasks.append(('url', lambda: _scrape_any(url, dbg)))
    tasks.append(('letras', lambda: (
        (lambda u: _scrape_letras(u, dbg) if u else [])(
            _letras_search(q or title, dbg)))))
    tasks.append(('uta-net', lambda: _scrape_utanet(title or q, dbg)))
    tasks.append(('j-lyric', lambda: _scrape_jlyric(title or q, artist, dbg)))
    best, best_src = [], None
    for name, fn in tasks:
        try:
            r = fn()
            trail.append('%s: %d lines' % (name, len(r)))
            if len(r) > len(best):
                best, best_src = r, name
            # a pasted URL or letras hit with real content wins immediately
            if name in ('url', 'letras') and len(r) >= 8:
                best, best_src = r, name
                break
        except Exception as e:
            trail.append('%s: ERR %s' % (name, type(e).__name__))
    return best, best_src


def _web_gap_fill(web_texts, base_lines, dbg):
    """Align web lyrics with the BASE timeline (editor text OR YT lines).
    Web lines NOT found in the base lines = 'missing' -> placed between
    two matching anchors with interpolated timestamps. base_lines is only a time reference:
    can be {text,start,end} (YT) or {text,time} (editor text)."""
    web_norm = [_norm(t) for t in web_texts]
    trail = dbg.setdefault('web', {}).setdefault('trail', [])

    def _btime(l):
        v = l.get('start')
        if v is None:
            v = l.get('time')
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _widx(txt):
        yn = _norm(txt)
        if len(yn) < 2:
            return -1
        for i, wn in enumerate(web_norm):
            if wn and (yn in wn or wn in yn):
                return i
        best_i, best_r = -1, 0.0
        for i, wn in enumerate(web_norm):
            if not wn:
                continue
            r = _ratio(yn, wn)
            if r > best_r:
                best_r, best_i = r, i
        return best_i if best_r >= 0.72 else -1

    # anchor = base line matching one of the web lines & having a time
    anchors = []
    for l in base_lines:
        wi = _widx(l.get('text', ''))
        t = _btime(l)
        if wi >= 0 and t is not None:
            anchors.append((wi, t))
    anchors.sort(key=lambda a: a[0])
    trail.append('align: %d web lines, %d/%d base lines became anchors' % (
        len(web_texts), len(anchors), len(base_lines)))
    if len(anchors) < 2:
        trail.append('align: need >=2 matching anchors; too few -> cannot place timeline')
        return []

    out, used = [], set()
    for (wa, ta), (wb, tb) in zip(anchors, anchors[1:]):
        if wb <= wa + 1 or tb <= ta:
            continue
        seg = [k for k in range(wa + 1, wb) if web_norm[k] and k not in used]
        if not seg:
            continue
        span = (tb - ta) / (len(seg) + 1)
        for si, k in enumerate(seg):
            used.add(k)
            st = round(ta + span * (si + 1), 2)
            en = round(min(ta + span * (si + 2), tb), 2)
            out.append({'text': web_texts[k], 'start': st,
                        'end': en, '_web': True})
    return out


def _yt_square_artwork_url(url, size=1080):
    value = str(url or '').strip()
    if not value or 'googleusercontent.com' not in value.lower():
        return value
    base = re.sub(r"=(?:w|s)\d+(?:-[A-Za-z0-9]+)*$", '', value)
    return '{}=w{}-h{}-l90-rj'.format(base, int(size), int(size))


def _pick_largest_yt_thumbnail(thumbnails):
    rows = [x for x in (thumbnails or []) if isinstance(x, dict) and x.get('url')]
    if not rows:
        return ''
    _, best = max(enumerate(rows), key=lambda pair: (
        int(pair[1].get('width') or 0) * int(pair[1].get('height') or 0), pair[0]
    ))
    return best.get('url') or ''


def register(app, ytmusic):

    @app.route('/search_songs', methods=['POST'])
    def search_songs():
        data = request.json or {}
        query = (data.get('query') or '').strip()
        if not query:
            return jsonify({'results': [], 'error': 'empty query'}), 400
        try:
            out = []
            vid = _extract_video_id(query)
            seen = set()
            filters = ('songs', 'videos')
            for filt in filters:
                try:
                    res = ytmusic.search(query, filter=filt, limit=6)
                except Exception:
                    res = []
                for it in (res or []):
                    v = it.get('videoId')
                    if not v or v in seen:
                        continue
                    seen.add(v)
                    arts = it.get('artists') or []
                    thumbs = it.get('thumbnails') or []
                    preview_thumb = _pick_largest_yt_thumbnail(thumbs)
                    out.append({
                        'videoId': v,
                        'title': it.get('title', ''),
                        'artist': ', '.join(a.get('name', '') for a in arts) if arts else '',
                        'album': (it.get('album') or {}).get('name') if isinstance(it.get('album'), dict) else '',
                        'duration': it.get('duration'),
                        'resultType': it.get('resultType'),
                        'thumbnail_preview': preview_thumb,
                        'thumbnail': _yt_square_artwork_url(preview_thumb, 1080),
                    })
            return jsonify({'results': out[:10], 'directVideoId': vid})
        except Exception as e:
            return jsonify({'results': [], 'error': f'{type(e).__name__}: {e}',
                            'traceback': traceback.format_exc()}), 500

    @app.route('/fetch_lyrics', methods=['POST'])
    def fetch_lyrics():
        data = request.json or {}
        query = (data.get('query') or '').strip()
        raw_vid = (data.get('videoId') or data.get('url') or '').strip()
        title_hint = (data.get('title') or '').strip()
        artist_hint = (data.get('artist') or '').strip()
        dbg = {'sourceTrail': [], 'input': {'query': query, 'videoId': raw_vid,
                                            'title': title_hint, 'artist': artist_hint}}
        try:
            video_id = _extract_video_id(raw_vid) or _extract_video_id(query)
            song = {}
            if not video_id and query:
                dbg['searchTried'] = []
                for filt in ('songs', 'videos', None):
                    try:
                        res = ytmusic.search(query, filter=filt, limit=3) if filt \
                            else ytmusic.search(query, limit=3)
                    except Exception as e:
                        dbg['searchTried'].append(f'{filt}: ERROR {type(e).__name__}')
                        continue
                    dbg['searchTried'].append(f'{filt}: {len(res or [])} hits')
                    hit = next((x for x in (res or []) if x.get('videoId')), None)
                    if hit:
                        video_id = hit['videoId']
                        arts = hit.get('artists') or []
                        song = {'title': hit.get('title', ''),
                                'artist': arts[0]['name'] if arts else '', 'videoId': video_id}
                        break
            dbg['resolvedVideoId'] = video_id
            if not video_id:
                return jsonify({'error': 'videoId not found. Paste a YouTube/YT Music link '
                                         'or use the Search button first.', 'debug': dbg}), 404

            lines, has_ts, source = [], False, None
            # 1) yt-dlp subtitles (most complete)
            try:
                sub, kind = _ytdlp_subs(video_id, dbg)
                if sub and any(l.get('start') is not None for l in sub):
                    lines, has_ts, source = sub, True, kind
            except Exception as e:
                dbg['sourceTrail'].append(f'yt-dlp error: {type(e).__name__}: {e}')

            # 2) YT Music timed (also fills dbg['song'])
            if not has_ts:
                yl, yts, ysrc = _ytmusic_timed(ytmusic, video_id, dbg)
                if yts:
                    lines, has_ts, source = yl, True, ysrc
                elif yl and not lines:
                    lines, source = yl, ysrc  # plain fallback held
            else:
                # still populate song hint
                try:
                    _ytmusic_timed(ytmusic, video_id, dbg)
                except Exception:
                    pass

            if not song and dbg.get('song'):
                song = {'title': dbg['song'].get('title', ''),
                        'artist': dbg['song'].get('artist', ''), 'videoId': video_id}
            title = title_hint or (dbg.get('song') or {}).get('title') or song.get('title', '')
            artist = artist_hint or (dbg.get('song') or {}).get('artist') or song.get('artist', '')
            dur = _dur_to_sec((dbg.get('song') or {}).get('duration'))

            # 3) LRCLIB if still no timestamps
            if not has_ts:
                try:
                    ll = _lrclib(title, artist, dur, dbg)
                    if ll:
                        lines, has_ts, source = ll, True, 'LRCLIB'
                        dbg['sourceTrail'].append(f'LRCLIB OK ({len(ll)} lines)')
                except Exception as e:
                    dbg['sourceTrail'].append(f'LRCLIB error: {type(e).__name__}: {e}')

            dbg['lineCount'] = len(lines)
            dbg['hasTimestamps'] = has_ts
            dbg['source'] = source
            if not lines:
                return jsonify({'error': 'videoId found but lyrics empty in all sources.',
                                'videoId': video_id, 'song': song, 'debug': dbg}), 404
            return jsonify({'videoId': video_id, 'song': song, 'hasTimestamps': has_ts,
                            'source': source, 'lines': lines, 'debug': dbg})
        except Exception as e:
            dbg['sourceTrail'].append(f'FATAL {type(e).__name__}: {e}')
            return jsonify({'error': f'{type(e).__name__}: {e}',
                            'traceback': traceback.format_exc(), 'debug': dbg}), 500

    @app.route('/web_lyrics', methods=['POST'])
    def web_lyrics():
        data = request.json or {}
        title = (data.get('title') or '').strip()
        artist = (data.get('artist') or '').strip()
        query = (data.get('query') or '').strip()
        url = (data.get('url') or '').strip()
        pasted = (data.get('pastedLyrics') or '')
        ref_lines = data.get('refLines') or []
        yt_lines = data.get('ytLines') or []
        track = int(data.get('track') or 2)
        base_lines = ref_lines if ref_lines else yt_lines
        base_src = 'editor' if ref_lines else ('yt' if yt_lines else 'none')
        dbg = {'input': {'title': title, 'artist': artist, 'query': query,
                         'url': url, 'hasPaste': bool(pasted.strip()),
                         'baseSrc': base_src, 'baseCount': len(base_lines)}}
        try:
            lines, src = _web_lyrics(title, artist, query, url, pasted, dbg)
            dbg['lineCount'] = len(lines)
            dbg['source'] = src
            gap_fill = _web_gap_fill(lines, base_lines, dbg) if base_lines else []
            # give per-line LRC so it can be imported directly without Sync again
            for g in gap_fill:
                try:
                    g['lrc'] = convert_line(g['text'], g.get('start'), g.get('end'), track)
                except Exception:
                    g['lrc'] = None
            dbg['gapFillCount'] = len(gap_fill)
            if not lines:
                return jsonify({
                    'error': 'Web lyrics empty. Paste the full lyrics into the '
                             '"Paste lyrics" field, OR provide a URL from letras.com / '
                             'uta-net / j-lyric. YouTube URLs are NOT used here.',
                    'lines': [], 'gapFill': [], 'debug': dbg}), 404
            return jsonify({'source': src,
                            'lines': [{'text': t} for t in lines],
                            'gapFill': gap_fill, 'debug': dbg})
        except Exception as e:
            return jsonify({'error': '%s: %s' % (type(e).__name__, e),
                            'traceback': traceback.format_exc(), 'debug': dbg}), 500

    @app.route('/sync_lyrics', methods=['POST'])
    def sync_lyrics():
        data = request.json or {}
        yt_lines = data.get('ytLines') or []
        ref_lines = data.get('refLines') or []
        track = int(data.get('track') or 2)
        thr = float(data.get('threshold') or 0.82)
        has_ts = any(l.get('start') is not None for l in yt_lines)
        dbg = {'ytLineCount': len(yt_lines), 'refLineCount': len(ref_lines),
               'track': track, 'ytHasTimestamps': has_ts}
        try:
            anchors, matches = _build_anchors(yt_lines, ref_lines, thr)
            mapper, offset = _make_mapper(anchors)
            dbg['anchorCount'] = len(anchors)
            dbg['offset'] = round(offset, 3)
            dbg['anchorsPreview'] = [{'yt': round(a[0], 2), 'you': round(a[1], 2)} for a in anchors[:10]]
            out, synced_ct, new_ct = [], 0, 0
            for k, y in enumerate(yt_lines):
                matched, ratio, rt = matches[k]
                if not matched:
                    new_ct += 1
                yst, yen = y.get('start'), y.get('end')
                sst = mapper(yst) if yst is not None else None
                sen = mapper(yen) if yen is not None else None
                if sst is not None and (sen is None or sen <= sst):
                    sen = sst + max(0.4, 0.18 * max(1, len(_norm(y.get('text', '')))))
                lrc = convert_line(y.get('text', ''), sst, sen, track) if sst is not None else None
                if lrc:
                    synced_ct += 1
                out.append({'text': y.get('text', ''), 'ytStart': yst, 'ytEnd': yen,
                            'syncedStart': sst, 'syncedEnd': sen, 'matched': matched,
                            'refTime': rt, 'ratio': round(ratio, 3), 'lrc': lrc})
            dbg['syncedWithTime'] = synced_ct
            dbg['newCount'] = new_ct
            note = None
            if not has_ts:
                note = ('These lyrics have NO timestamps from the source, so NEW lines cannot '
                        'be placed on the timeline. Try another source (yt-dlp caption / LRCLIB).')
            return jsonify({'anchors': len(anchors), 'offset': round(offset, 3),
                            'lines': out, 'debug': dbg, 'note': note})
        except Exception as e:
            return jsonify({'error': f'{type(e).__name__}: {e}',
                            'traceback': traceback.format_exc(), 'debug': dbg}), 500


    print('🔗 lyric_sync_addon v6: /search_songs /fetch_lyrics /sync_lyrics '
          '/web_lyrics registered '
          '(yt-dlp + YTMusic + LRCLIB + web-scrape/paste align; Whisper REMOVED)')
