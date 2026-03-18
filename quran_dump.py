#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "requests>=2.31.0",
# ]
# ///
"""
quran_dump.py — Complete Quran.com content archiver (v2).

Downloads EVERYTHING from Quran.com and stores it in a fully relational
SQLite database. Resumable — re-run any time and it picks up where it left off.

APIs used:
  QDC proxy:  https://quran.com/api/proxy/content/api/qdc   (verses, words, translations, tafsirs, footnotes)
  Public V4:  https://api.quran.com/api/v4                  (chapter info, verse audio, chapter audio)
  Word audio: https://audio.qurancdn.com/wbw/               (CDN — URLs stored, not downloaded)
  Verse audio:https://download.quranicaudio.com/            (CDN — URLs stored, not downloaded)

Content captured:
  ✓ 114 chapters with full metadata
  ✓ 30 juzs with verse mappings
  ✓ 6236 verses (4 Arabic text variants + structural fields)
  ✓ ~77 000 words (4 scripts + transliteration + EN translation + audio URL)
  ✓ Translations  — 9 default (EN×6 + UR×3), or --all-translations for all 146
  ✓ Footnotes     — all unique footnotes referenced in downloaded translations
  ✓ Tafsirs       — Ibn Kathir EN default, or --all-tafsirs for all 23
  ✓ Chapter intro text (Maududi background) in EN + UR + AR
  ✓ Verse-level audio URLs for all 20 reciters
  ✓ Full-chapter audio URLs for all 20 reciters
  ✓ 79 languages, 146 translation resources, 23 tafsir resources
  ✓ 20 audio reciters + recitation styles
  ✓ Pages layout (Medina Mushaf)

Usage:
  python quran_dump.py                    # full run (default translations + Ibn Kathir)
  python quran_dump.py --all-translations # all 146 translations
  python quran_dump.py --all-tafsirs      # all 23 tafsirs
  python quran_dump.py --chapter 2       # single chapter (testing)
  python quran_dump.py --skip-tafsirs    # skip tafsir download
  python quran_dump.py --skip-footnotes  # skip footnote download
  python quran_dump.py --delay 1.0       # be more polite
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Matches both quoted and unquoted foot_note attributes:
#   <sup foot_note=227140>1</sup>
#   <sup foot_note="176997">1</sup>
FOOTNOTE_RE = re.compile(r'<sup\s+foot_note="?(\d+)"?>', re.IGNORECASE)

import requests
from requests.adapters import HTTPAdapter

# ── API bases ─────────────────────────────────────────────────────────────────

QDC_URL = "https://quran.com/api/proxy/content/api/qdc"   # verses, words, tafsirs
V4_URL  = "https://api.quran.com/api/v4"                  # chapter info, audio

# ── CDN base URLs (stored in DB, not downloaded) ──────────────────────────────
WBW_AUDIO_CDN    = "https://audio.qurancdn.com/"          # + "wbw/001_001_001.mp3"
VERSE_AUDIO_CDN  = "https://download.quranicaudio.com/"   # + reciter-specific path

# ── Defaults ──────────────────────────────────────────────────────────────────

DEFAULT_DB       = "quran_dump.sqlite3"
DEFAULT_DELAY    = 0.6     # base seconds between requests
JITTER           = 0.3     # ± random jitter
DEFAULT_PER_PAGE = 50
DEFAULT_MUSHAF   = 7       # Medina Mushaf

# Default translation IDs (SELECT id,name,language_name FROM resources_translations for full list)
DEFAULT_TRANSLATION_IDS = [
    131,   # Khattab – The Clear Quran (EN)
     85,   # Abdel Haleem (EN)
     95,   # Saheeh International (EN)
     97,   # Pickthall (EN)
     22,   # Yusuf Ali original (EN)
     20,   # Muhammad Sarwar (EN)
    203,   # Wahiduddin Khan (UR)
     54,   # Fatah Muhammad Jalandhari (UR)
    149,   # Muhammad Taqi Usmani (UR)
]

# Default tafsirs (SELECT slug,name,language_name FROM resources_tafsirs for full list)
DEFAULT_TAFSIR_SLUGS = ["en-tafisr-ibn-kathir"]   # note API typo

# Languages to fetch chapter info for
CHAPTER_INFO_LANGS = ["en", "ur", "ar"]

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

QDC_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",   # NO brotli — requests can't decode it
    "Cache-Control": "no-cache",
    "Referer": "https://quran.com/",
    "Origin": "https://quran.com",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

V4_HEADERS = {
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate",
}

# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging(log_file: str = "quran_dump.log") -> logging.Logger:
    logger = logging.getLogger("quran_dump")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger

log = setup_logging()

# ── HTTP ──────────────────────────────────────────────────────────────────────

_qdc_session: requests.Session | None = None
_v4_session:  requests.Session | None = None
_delay: float = DEFAULT_DELAY
_req_count = 0

def _make_session(extra_headers: dict) -> requests.Session:
    s = requests.Session()
    s.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=4))
    h = dict(extra_headers)
    h["User-Agent"] = random.choice(USER_AGENTS)
    s.headers.update(h)
    return s

def _get(base: str, path: str, params: dict | None = None) -> dict:
    global _qdc_session, _v4_session, _req_count

    if base == QDC_URL:
        if _qdc_session is None:
            _qdc_session = _make_session(QDC_HEADERS)
        session = _qdc_session
    else:
        if _v4_session is None:
            _v4_session = _make_session(V4_HEADERS)
        session = _v4_session

    _req_count += 1
    if _req_count % 60 == 0:
        session.headers["User-Agent"] = random.choice(USER_AGENTS)
        log.debug("Rotated user-agent (req %d)", _req_count)

    sleep_time = max(0.15, _delay + random.uniform(-JITTER, JITTER))
    time.sleep(sleep_time)

    url = base + path
    log.debug("GET %s%s  params=%s", base, path, params)

    last_exc: Exception | None = None
    for attempt in range(5):
        try:
            r = session.get(url, params=params, timeout=25)
            if r.status_code == 429:
                wait = 45 + attempt * 20
                log.warning("Rate-limited (429). Sleeping %ds …", wait)
                time.sleep(wait)
                continue
            if r.status_code == 404:
                log.debug("404 → %s", url)
                return {}
            r.raise_for_status()
            text = r.text.strip()
            if not text:
                return {}
            return json.loads(text)
        except requests.exceptions.ConnectionError as e:
            last_exc = e
            wait = 4 * (2 ** attempt)
            log.warning("Connection error [%d/5]: %s. Retrying in %ds", attempt + 1, e, wait)
            time.sleep(wait)
            if attempt >= 2:
                if base == QDC_URL:
                    _qdc_session = _make_session(QDC_HEADERS)
                    session = _qdc_session
                else:
                    _v4_session = _make_session(V4_HEADERS)
                    session = _v4_session
        except requests.exceptions.Timeout as e:
            last_exc = e
            wait = 10 * (attempt + 1)
            log.warning("Timeout [%d/5]. Retrying in %ds", attempt + 1, wait)
            time.sleep(wait)
        except requests.exceptions.HTTPError as e:
            log.error("HTTP error: %s", e)
            raise

    log.error("All retries exhausted for %s", url)
    if last_exc:
        raise last_exc
    return {}

def qdc(path: str, params: dict | None = None) -> dict:
    return _get(QDC_URL, path, params)

def v4(path: str, params: dict | None = None) -> dict:
    return _get(V4_URL, path, params)

# ── Database ──────────────────────────────────────────────────────────────────

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA cache_size   = -131072;

-- ═══ Reference / lookup tables ══════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS languages (
    id                 INTEGER PRIMARY KEY,
    name               TEXT    NOT NULL,
    iso_code           TEXT,
    native_name        TEXT,
    direction          TEXT,
    translations_count INTEGER,
    raw_json           TEXT
);

CREATE TABLE IF NOT EXISTS recitation_styles (
    style_key   TEXT PRIMARY KEY,
    description TEXT
);

CREATE TABLE IF NOT EXISTS resources_translations (
    id            INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL,
    author_name   TEXT,
    slug          TEXT,
    language_name TEXT,
    language_iso  TEXT,
    raw_json      TEXT
);

CREATE TABLE IF NOT EXISTS resources_tafsirs (
    id            INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL,
    author_name   TEXT,
    slug          TEXT,
    language_name TEXT,
    raw_json      TEXT
);

CREATE TABLE IF NOT EXISTS audio_reciters (
    id          INTEGER PRIMARY KEY,   -- QDC id (used in V4 recitations endpoint)
    reciter_id  INTEGER,               -- QDC reciter_id (internal)
    name        TEXT    NOT NULL,
    style_name  TEXT,
    style_desc  TEXT,
    raw_json    TEXT
);

-- ═══ Core Quran structure ════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS chapters (
    id               INTEGER PRIMARY KEY,
    name_simple      TEXT    NOT NULL,
    name_complex     TEXT,
    name_arabic      TEXT,
    revelation_place TEXT,
    revelation_order INTEGER,
    bismillah_pre    INTEGER DEFAULT 0,
    verses_count     INTEGER NOT NULL,
    pages_from       INTEGER,
    pages_to         INTEGER,
    slug             TEXT,
    translated_name  TEXT,
    raw_json         TEXT
);

CREATE TABLE IF NOT EXISTS chapter_info (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id    INTEGER NOT NULL REFERENCES chapters(id),
    language_name TEXT    NOT NULL,
    short_text    TEXT,
    text          TEXT,    -- full HTML intro from Maududi / other source
    source        TEXT,
    raw_json      TEXT,
    UNIQUE(chapter_id, language_name)
);

CREATE INDEX IF NOT EXISTS idx_chapter_info_ch ON chapter_info(chapter_id);

CREATE TABLE IF NOT EXISTS juzs (
    juz_number     INTEGER PRIMARY KEY,
    db_id          INTEGER,
    verse_mapping  TEXT,
    first_verse_id INTEGER,
    last_verse_id  INTEGER,
    verses_count   INTEGER,
    raw_json       TEXT
);

CREATE TABLE IF NOT EXISTS verses (
    id                  INTEGER PRIMARY KEY,
    verse_number        INTEGER NOT NULL,
    verse_key           TEXT    UNIQUE NOT NULL,
    chapter_id          INTEGER NOT NULL REFERENCES chapters(id),
    juz_number          INTEGER,
    hizb_number         INTEGER,
    rub_el_hizb_number  INTEGER,
    ruku_number         INTEGER,
    manzil_number       INTEGER,
    sajdah_type         TEXT,
    sajdah_number       INTEGER,
    page_number         INTEGER,
    text_uthmani        TEXT,
    text_imlaei_simple  TEXT,
    text_indopak        TEXT,
    has_related_verses  INTEGER DEFAULT 0,
    raw_json            TEXT
);

CREATE INDEX IF NOT EXISTS idx_verses_chapter ON verses(chapter_id);
CREATE INDEX IF NOT EXISTS idx_verses_juz     ON verses(juz_number);
CREATE INDEX IF NOT EXISTS idx_verses_page    ON verses(page_number);

-- ═══ Word-level data ═════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS words (
    id                 INTEGER PRIMARY KEY,
    verse_id           INTEGER NOT NULL REFERENCES verses(id),
    verse_key          TEXT    NOT NULL,
    position           INTEGER NOT NULL,
    char_type          TEXT,
    page_number        INTEGER,
    line_number        INTEGER,
    text_uthmani       TEXT,
    text_imlaei_simple TEXT,
    text_indopak       TEXT,
    qpc_uthmani_hafs   TEXT,
    transliteration    TEXT,
    translation_en     TEXT,
    audio_url          TEXT,   -- relative path; prepend WBW_AUDIO_CDN
    raw_json           TEXT,
    UNIQUE(verse_id, position)
);

CREATE INDEX IF NOT EXISTS idx_words_verse ON words(verse_id);

-- ═══ Translations ════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS translations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    verse_id      INTEGER NOT NULL REFERENCES verses(id),
    verse_key     TEXT    NOT NULL,
    resource_id   INTEGER NOT NULL,
    resource_name TEXT,
    language_name TEXT,
    text          TEXT,
    UNIQUE(verse_key, resource_id)
);

CREATE INDEX IF NOT EXISTS idx_trans_verse    ON translations(verse_id);
CREATE INDEX IF NOT EXISTS idx_trans_resource ON translations(resource_id);

-- ═══ Tafsirs ════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS tafsirs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    verse_key     TEXT    NOT NULL,
    verse_id      INTEGER REFERENCES verses(id),
    chapter_id    INTEGER REFERENCES chapters(id),
    verse_number  INTEGER,
    resource_id   INTEGER,
    resource_name TEXT,
    slug          TEXT    NOT NULL,
    language_name TEXT,
    text          TEXT,
    UNIQUE(verse_key, slug)
);

CREATE INDEX IF NOT EXISTS idx_tafsirs_verse   ON tafsirs(verse_key);
CREATE INDEX IF NOT EXISTS idx_tafsirs_chapter ON tafsirs(chapter_id);
CREATE INDEX IF NOT EXISTS idx_tafsirs_slug    ON tafsirs(slug);

-- ═══ Audio ══════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS verse_audio (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    verse_key   TEXT    NOT NULL,
    reciter_id  INTEGER NOT NULL,   -- audio_reciters.id
    url         TEXT,               -- relative; prepend VERSE_AUDIO_CDN
    UNIQUE(verse_key, reciter_id)
);

CREATE INDEX IF NOT EXISTS idx_verse_audio_verse   ON verse_audio(verse_key);
CREATE INDEX IF NOT EXISTS idx_verse_audio_reciter ON verse_audio(reciter_id);

CREATE TABLE IF NOT EXISTS chapter_audio (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id  INTEGER NOT NULL REFERENCES chapters(id),
    reciter_id  INTEGER NOT NULL,
    file_size   REAL,
    format      TEXT,
    audio_url   TEXT,   -- full URL
    raw_json    TEXT,
    UNIQUE(chapter_id, reciter_id)
);

CREATE INDEX IF NOT EXISTS idx_chapter_audio_ch ON chapter_audio(chapter_id);

-- ═══ Pages ══════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS pages_lookup (
    chapter_id  INTEGER NOT NULL REFERENCES chapters(id),
    mushaf      INTEGER NOT NULL,
    raw_json    TEXT,
    PRIMARY KEY (chapter_id, mushaf)
);

-- ═══ Footnotes ══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS footnotes (
    id            INTEGER PRIMARY KEY,   -- quran.com foot_note ID
    text          TEXT,                  -- footnote content
    language_name TEXT,
    language_id   INTEGER
);

-- ═══ Progress tracking ══════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS download_progress (
    task_key        TEXT    PRIMARY KEY,
    status          TEXT    NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','in_progress','completed','error')),
    started_at      TEXT,
    completed_at    TEXT,
    records_fetched INTEGER DEFAULT 0,
    error_msg       TEXT
);
"""

def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.executescript(SCHEMA)
    conn.commit()
    log.info("Database ready: %s", db_path)
    return conn

def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _done(conn: sqlite3.Connection, key: str) -> bool:
    row = conn.execute("SELECT status FROM download_progress WHERE task_key=?", (key,)).fetchone()
    return bool(row and row[0] == "completed")

def _start(conn: sqlite3.Connection, key: str):
    conn.execute(
        "INSERT INTO download_progress(task_key,status,started_at) VALUES(?,?,?) "
        "ON CONFLICT(task_key) DO UPDATE SET status='in_progress',started_at=excluded.started_at",
        (key, "in_progress", _ts()),
    )
    conn.commit()

def _complete(conn: sqlite3.Connection, key: str, n: int = 0):
    conn.execute(
        "UPDATE download_progress SET status='completed',completed_at=?,records_fetched=? WHERE task_key=?",
        (_ts(), n, key),
    )
    conn.commit()

def _error(conn: sqlite3.Connection, key: str, msg: str):
    conn.execute(
        "UPDATE download_progress SET status='error',completed_at=?,error_msg=? WHERE task_key=?",
        (_ts(), str(msg)[:500], key),
    )
    conn.commit()

# ── Step 1: Resource lists ────────────────────────────────────────────────────

def dump_resources(conn: sqlite3.Connection) -> list[dict]:
    log.info("━━━ Step 1/7: Resource lists ━━━")

    # Languages
    if not _done(conn, "res:languages"):
        _start(conn, "res:languages")
        items = qdc("/resources/languages").get("languages", [])
        conn.executemany("INSERT OR REPLACE INTO languages VALUES(?,?,?,?,?,?,?)",
            [(l["id"], l["name"], l["iso_code"], l["native_name"],
              l["direction"], l.get("translations_count"), json.dumps(l, ensure_ascii=False))
             for l in items])
        conn.commit()
        _complete(conn, "res:languages", len(items))
        log.info("  languages       : %d", len(items))

    # Recitation styles
    if not _done(conn, "res:recitation_styles"):
        _start(conn, "res:recitation_styles")
        data = qdc("/resources/recitation_styles").get("recitation_styles", {})
        conn.executemany("INSERT OR REPLACE INTO recitation_styles VALUES(?,?)",
            [(k, v) for k, v in data.items()])
        conn.commit()
        _complete(conn, "res:recitation_styles", len(data))
        log.info("  recit. styles   : %d", len(data))

    # Translation resources (all, no language filter)
    if not _done(conn, "res:translations"):
        _start(conn, "res:translations")
        items = qdc("/resources/translations").get("translations", [])
        # 7 cols: id, name, author_name, slug, language_name, language_iso, raw_json
        conn.executemany("INSERT OR REPLACE INTO resources_translations VALUES(?,?,?,?,?,?,?)",
            [(t["id"], t["name"], t.get("author_name"), t["slug"],
              t["language_name"], None, json.dumps(t, ensure_ascii=False))
             for t in items])
        conn.commit()
        _complete(conn, "res:translations", len(items))
        log.info("  translations    : %d available", len(items))

    # Tafsir resources
    if not _done(conn, "res:tafsirs"):
        _start(conn, "res:tafsirs")
        items = qdc("/resources/tafsirs", {"language": "en"}).get("tafsirs", [])
        conn.executemany("INSERT OR REPLACE INTO resources_tafsirs VALUES(?,?,?,?,?,?)",
            [(t["id"], t["name"], t.get("author_name"), t["slug"],
              t["language_name"], json.dumps(t, ensure_ascii=False))
             for t in items])
        conn.commit()
        _complete(conn, "res:tafsirs", len(items))
        log.info("  tafsirs         : %d available", len(items))

    # Audio reciters
    if not _done(conn, "res:reciters"):
        _start(conn, "res:reciters")
        items = qdc("/audio/reciters").get("reciters", [])
        conn.executemany("INSERT OR REPLACE INTO audio_reciters VALUES(?,?,?,?,?,?)",
            [(r["id"], r.get("reciter_id"), r["name"],
              r.get("style", {}).get("name") if isinstance(r.get("style"), dict) else None,
              r.get("style", {}).get("description") if isinstance(r.get("style"), dict) else None,
              json.dumps(r, ensure_ascii=False))
             for r in items])
        conn.commit()
        _complete(conn, "res:reciters", len(items))
        log.info("  audio reciters  : %d", len(items))

    # Chapters
    if not _done(conn, "res:chapters"):
        _start(conn, "res:chapters")
        items = qdc("/chapters", {"language": "en"}).get("chapters", [])
        conn.executemany("INSERT OR REPLACE INTO chapters VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(c["id"], c["name_simple"], c["name_complex"], c["name_arabic"],
              c["revelation_place"], c["revelation_order"], int(c["bismillah_pre"]),
              c["verses_count"],
              c["pages"][0] if c.get("pages") else None,
              c["pages"][1] if c.get("pages") else None,
              c.get("slug", {}).get("slug") if isinstance(c.get("slug"), dict) else c.get("slug"),
              c.get("translated_name", {}).get("name"),
              json.dumps(c, ensure_ascii=False))
             for c in items])
        conn.commit()
        _complete(conn, "res:chapters", len(items))
        log.info("  chapters        : %d", len(items))

    # Juzs
    if not _done(conn, "res:juzs"):
        _start(conn, "res:juzs")
        juz_map: dict[int, dict] = {}
        for j in qdc("/juzs").get("juzs", []):
            juz_map[j["juz_number"]] = j
        conn.executemany("INSERT OR REPLACE INTO juzs VALUES(?,?,?,?,?,?,?)",
            [(j["juz_number"], j["id"], json.dumps(j["verse_mapping"], ensure_ascii=False),
              j["first_verse_id"], j["last_verse_id"], j["verses_count"],
              json.dumps(j, ensure_ascii=False))
             for j in juz_map.values()])
        conn.commit()
        _complete(conn, "res:juzs", len(juz_map))
        log.info("  juzs            : %d", len(juz_map))

    chapters = [dict(zip(
        ["id","name_simple","name_complex","name_arabic","revelation_place","revelation_order",
         "bismillah_pre","verses_count","pages_from","pages_to","slug","translated_name","raw_json"], row))
        for row in conn.execute("SELECT * FROM chapters ORDER BY id").fetchall()]
    return chapters


# ── Step 2: Verses + Words + Translations ────────────────────────────────────

def _parse_word(w: dict, verse_id: int, verse_key: str) -> tuple:
    tr = w.get("transliteration")
    tr = tr.get("text") if isinstance(tr, dict) else tr
    tl = w.get("translation")
    tl = tl.get("text") if isinstance(tl, dict) else tl
    return (w.get("id"), verse_id, verse_key, w.get("position"),
            w.get("char_type_name") or w.get("char_type"),
            w.get("page_number"), w.get("line_number"),
            w.get("text_uthmani"), w.get("text_imlaei_simple"),
            w.get("text_indopak"), w.get("qpc_uthmani_hafs"),
            tr, tl, w.get("audio_url"), json.dumps(w, ensure_ascii=False))

def dump_verses(conn: sqlite3.Connection, chapters: list[dict],
                translation_ids: list[int], mushaf: int):
    trans_param = ",".join(str(t) for t in translation_ids)
    log.info("━━━ Step 2/7: Verses + Words + Translations  [%s] ━━━", trans_param)
    total = len(chapters)

    for idx, ch in enumerate(chapters, 1):
        cid, expected = ch["id"], ch["verses_count"]
        key = f"verses:ch:{cid}"
        if _done(conn, key):
            log.info("  [%3d/%d] Ch%3d %-22s ✓", idx, total, cid, ch["name_simple"])
            continue

        _start(conn, key)
        log.info("  [%3d/%d] Ch%3d %-22s (%d verses)…", idx, total, cid, ch["name_simple"], expected)

        fetched, page = 0, 1
        try:
            while True:
                data = qdc(f"/verses/by_chapter/{cid}", {
                    "language": "en", "words": "true",
                    "per_page": DEFAULT_PER_PAGE, "page": page,
                    "fields": ("text_uthmani,text_imlaei_simple,text_indopak,verse_key,"
                               "hizb_number,rub_el_hizb_number,ruku_number,manzil_number,"
                               "sajdah_type,sajdah_number,page_number,juz_number,has_related_verses"),
                    "translations": trans_param,
                    "translation_fields": "resource_name,language_name,resource_id",
                    "word_fields": ("verse_key,verse_id,position,text_uthmani,text_imlaei_simple,"
                                    "text_indopak,qpc_uthmani_hafs,transliteration,translation,"
                                    "audio_url,char_type_name,page_number,line_number"),
                    "word_translation_language": "en", "mushaf": mushaf,
                })
                verses = data.get("verses", [])
                if not verses:
                    break

                vrows, wrows, trows = [], [], []
                for v in verses:
                    vrows.append((v["id"], v["verse_number"], v["verse_key"], cid,
                                  v.get("juz_number"), v.get("hizb_number"), v.get("rub_el_hizb_number"),
                                  v.get("ruku_number"), v.get("manzil_number"),
                                  v.get("sajdah_type"), v.get("sajdah_number"), v.get("page_number"),
                                  v.get("text_uthmani"), v.get("text_imlaei_simple"), v.get("text_indopak"),
                                  int(bool(v.get("has_related_verses"))), json.dumps(v, ensure_ascii=False)))
                    for w in v.get("words", []):
                        wrows.append(_parse_word(w, v["id"], v["verse_key"]))
                    for t in v.get("translations", []):
                        trows.append((v["id"], v["verse_key"], t.get("resource_id"),
                                      t.get("resource_name"), t.get("language_name"), t.get("text")))

                conn.executemany(
                    "INSERT OR REPLACE INTO verses "
                    "(id,verse_number,verse_key,chapter_id,juz_number,hizb_number,rub_el_hizb_number,"
                    "ruku_number,manzil_number,sajdah_type,sajdah_number,page_number,"
                    "text_uthmani,text_imlaei_simple,text_indopak,has_related_verses,raw_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", vrows)
                conn.executemany(
                    "INSERT OR IGNORE INTO words "
                    "(id,verse_id,verse_key,position,char_type,page_number,line_number,"
                    "text_uthmani,text_imlaei_simple,text_indopak,qpc_uthmani_hafs,"
                    "transliteration,translation_en,audio_url,raw_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", wrows)
                conn.executemany(
                    "INSERT OR IGNORE INTO translations "
                    "(verse_id,verse_key,resource_id,resource_name,language_name,text) "
                    "VALUES(?,?,?,?,?,?)", trows)
                conn.commit()

                fetched += len(verses)
                log.debug("    page %d: +%d verses (total %d/%d)", page, len(verses), fetched, expected)
                pag = data.get("pagination", {})
                if pag.get("next_page") is None or fetched >= expected:
                    break
                page += 1

            _complete(conn, key, fetched)
            log.info("    → %d verses, %d pages", fetched, page)
        except Exception as e:
            _error(conn, key, str(e))
            log.error("    ERROR ch%d: %s", cid, e)

    # Pages layout
    log.info("  Pages lookup (mushaf %d)…", mushaf)
    for ch in chapters:
        cid = ch["id"]
        pkey = f"pages:m{mushaf}:ch:{cid}"
        if _done(conn, pkey):
            continue
        _start(conn, pkey)
        data = qdc("/pages/lookup", {"mushaf": mushaf, "chapter_number": cid})
        if data:
            conn.execute("INSERT OR REPLACE INTO pages_lookup VALUES(?,?,?)",
                         (cid, mushaf, json.dumps(data, ensure_ascii=False)))
            conn.commit()
        _complete(conn, pkey, 1)
    log.info("  Pages lookup done for %d chapters.", len(chapters))


# ── Step 3: Footnotes ─────────────────────────────────────────────────────────

def dump_footnotes(conn: sqlite3.Connection) -> None:
    """Fetch every unique footnote referenced across all stored translations.

    Translation texts contain markers like ``<sup foot_note=227140>1</sup>``.
    This step resolves each unique foot_note ID to its full text by calling
    ``GET /foot_notes/{id}`` on the QDC proxy, then stores the result in the
    ``footnotes`` table.

    Fully resumable: any IDs already in the table are skipped.
    """
    log.info("━━━ Step 3/7: Footnotes ━━━")
    task_key = "footnotes:all"

    # Scan all translation texts to collect unique footnote IDs
    log.info("  Scanning translations for footnote IDs…")
    rows = conn.execute("SELECT text FROM translations WHERE text IS NOT NULL").fetchall()
    all_ids: set[int] = set()
    for (text,) in rows:
        for m in FOOTNOTE_RE.finditer(text):
            all_ids.add(int(m.group(1)))

    existing: set[int] = {r[0] for r in conn.execute("SELECT id FROM footnotes").fetchall()}
    pending = sorted(all_ids - existing)

    log.info(
        "  Unique footnote IDs: %d  (already stored: %d, to fetch: %d)",
        len(all_ids), len(existing), len(pending),
    )

    if not pending:
        if not _done(conn, task_key):
            _complete(conn, task_key, len(existing))
        log.info("  All footnotes already downloaded ✓")
        return

    _start(conn, task_key)
    fetched = 0
    errors = 0

    for fn_id in pending:
        data = qdc(f"/foot_notes/{fn_id}")
        fn = data.get("foot_note", {})
        if fn:
            conn.execute(
                "INSERT OR REPLACE INTO footnotes(id, text, language_name, language_id) "
                "VALUES(?, ?, ?, ?)",
                (fn["id"], fn.get("text"), fn.get("language_name"), fn.get("language_id")),
            )
            conn.commit()
            fetched += 1
        else:
            log.debug("  footnote %d: empty response", fn_id)
            errors += 1

        if fetched % 500 == 0 and fetched:
            log.info("  … %d / %d fetched", fetched, len(pending))

    _complete(conn, task_key, len(existing) + fetched)
    log.info("  → %d footnotes fetched (%d errors)", fetched, errors)


# ── Step 4: Tafsirs ───────────────────────────────────────────────────────────

def dump_tafsirs(conn: sqlite3.Connection, chapters: list[dict], slugs: list[str]):
    log.info("━━━ Step 4/7: Tafsirs  [%s] ━━━", ", ".join(slugs))
    slug_to_id = {r[0]: r[1] for r in conn.execute("SELECT slug,id FROM resources_tafsirs").fetchall()}
    total = len(chapters)

    for slug in slugs:
        rid = slug_to_id.get(slug)
        log.info("  Tafsir: %s (resource_id=%s)", slug, rid)
        for idx, ch in enumerate(chapters, 1):
            cid, expected = ch["id"], ch["verses_count"]
            key = f"tafsir:{slug}:ch:{cid}"
            if _done(conn, key):
                continue
            _start(conn, key)
            log.info("  [%3d/%d] Ch%3d %-22s…", idx, total, cid, ch["name_simple"])

            fetched, page = 0, 1
            try:
                while True:
                    data = qdc(f"/tafsirs/{slug}/by_chapter/{cid}",
                               {"per_page": DEFAULT_PER_PAGE, "page": page, "locale": "en"})
                    items = data.get("tafsirs", [])
                    if not items:
                        break

                    rows = []
                    for t in items:
                        vkey = t.get("verse_key") or f"{cid}:{t.get('verse_number','')}"
                        vrow = conn.execute("SELECT id FROM verses WHERE verse_key=?", (vkey,)).fetchone()
                        rows.append((vkey, vrow[0] if vrow else None, cid, t.get("verse_number"),
                                     rid or t.get("resource_id"), t.get("resource_name"),
                                     slug, t.get("language_name"), t.get("text")))

                    conn.executemany(
                        "INSERT OR IGNORE INTO tafsirs "
                        "(verse_key,verse_id,chapter_id,verse_number,resource_id,resource_name,slug,language_name,text) "
                        "VALUES(?,?,?,?,?,?,?,?,?)", rows)
                    conn.commit()
                    fetched += len(items)
                    pag = data.get("pagination", {})
                    if pag.get("next_page") is None or fetched >= expected:
                        break
                    page += 1

                _complete(conn, key, fetched)
                log.info("    → %d entries", fetched)
            except Exception as e:
                _error(conn, key, str(e))
                log.error("    ERROR tafsir %s ch%d: %s", slug, cid, e)


# ── Step 4: Chapter intro text ────────────────────────────────────────────────

def dump_chapter_info(conn: sqlite3.Connection, chapters: list[dict], langs: list[str]):
    log.info("━━━ Step 5/7: Chapter intro text  [langs: %s] ━━━", ", ".join(langs))
    total = len(chapters)

    for lang in langs:
        log.info("  Language: %s", lang)
        for ch_idx, ch in enumerate(chapters, 1):
            cid = ch["id"]
            key = f"chapter_info:{lang}:ch:{cid}"
            if _done(conn, key):
                continue
            _start(conn, key)

            data = v4(f"/chapters/{cid}/info", {"language": lang})
            info = data.get("chapter_info", {})
            if info:
                conn.execute(
                    "INSERT OR REPLACE INTO chapter_info "
                    "(chapter_id,language_name,short_text,text,source,raw_json) "
                    "VALUES(?,?,?,?,?,?)",
                    (cid, info.get("language_name", lang),
                     info.get("short_text"), info.get("text"),
                     info.get("source"), json.dumps(info, ensure_ascii=False)))
                conn.commit()
                _complete(conn, key, 1)
                log.info("  [%3d/%d] Ch%3d %-22s [%s] ✓", ch_idx, total, cid, ch["name_simple"], lang)
            else:
                _complete(conn, key, 0)
                log.debug("  [%3d/%d] Ch%3d no info in [%s]", ch_idx, total, cid, lang)


# ── Step 5: Audio URLs ────────────────────────────────────────────────────────

def dump_audio(conn: sqlite3.Connection, chapters: list[dict]):
    log.info("━━━ Step 6/7: Audio URLs (verse-level + chapter-level) ━━━")

    # Get all reciter IDs from DB
    reciters = conn.execute("SELECT id, name FROM audio_reciters ORDER BY id").fetchall()
    log.info("  %d reciters to process", len(reciters))
    total_ch = len(chapters)

    for reciter_id, reciter_name in reciters:
        log.info("  Reciter %d: %s", reciter_id, reciter_name)

        for idx, ch in enumerate(chapters, 1):
            cid = ch["id"]

            # Verse-level audio (per-verse MP3 URLs)
            vkey = f"audio:verse:r{reciter_id}:ch:{cid}"
            if not _done(conn, vkey):
                _start(conn, vkey)
                try:
                    data = v4(f"/recitations/{reciter_id}/by_chapter/{cid}")
                    files = data.get("audio_files", [])
                    if files:
                        conn.executemany(
                            "INSERT OR IGNORE INTO verse_audio(verse_key,reciter_id,url) VALUES(?,?,?)",
                            [(f["verse_key"], reciter_id, f.get("url")) for f in files])
                        conn.commit()
                    _complete(conn, vkey, len(files))
                    log.debug("    verse audio ch%d r%d: %d files", cid, reciter_id, len(files))
                except Exception as e:
                    _error(conn, vkey, str(e))
                    log.warning("    WARN verse audio ch%d r%d: %s", cid, reciter_id, e)

            # Chapter-level audio (single MP3 for whole chapter)
            ckey = f"audio:chapter:r{reciter_id}:ch:{cid}"
            if not _done(conn, ckey):
                _start(conn, ckey)
                try:
                    data = v4(f"/chapter_recitations/{reciter_id}/{cid}")
                    af = data.get("audio_file", {})
                    if af:
                        conn.execute(
                            "INSERT OR IGNORE INTO chapter_audio "
                            "(chapter_id,reciter_id,file_size,format,audio_url,raw_json) "
                            "VALUES(?,?,?,?,?,?)",
                            (cid, reciter_id, af.get("file_size"), af.get("format"),
                             af.get("audio_url"), json.dumps(af, ensure_ascii=False)))
                        conn.commit()
                    _complete(conn, ckey, 1 if af else 0)
                except Exception as e:
                    _error(conn, ckey, str(e))
                    log.warning("    WARN chapter audio ch%d r%d: %s", cid, reciter_id, e)

        log.info("    → reciter %d done (%d chapters)", reciter_id, total_ch)


# ── Step 6: Summary ───────────────────────────────────────────────────────────

def print_summary(conn: sqlite3.Connection, db_path: str):
    log.info("━━━ Step 7/7: Summary ━━━")
    tables = ["chapters", "chapter_info", "juzs", "verses", "words",
              "translations", "footnotes", "tafsirs", "verse_audio", "chapter_audio",
              "resources_translations", "resources_tafsirs", "audio_reciters",
              "languages", "recitation_styles", "pages_lookup", "download_progress"]
    size_mb = Path(db_path).stat().st_size / 1_048_576
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print(f"  Database : {db_path}")
    print(f"  Size     : {size_mb:.1f} MB")
    print("  Rows:")
    for t in tables:
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"    {t:<28}  {n:>9,}")
    statuses = conn.execute("SELECT status,COUNT(*) FROM download_progress GROUP BY status").fetchall()
    print()
    print("  Download progress:")
    for s, c in statuses:
        print(f"    {s:<12}  {c:>6} tasks")
    errors = conn.execute(
        "SELECT task_key,error_msg FROM download_progress WHERE status='error'").fetchall()
    if errors:
        print()
        print("  ⚠ Errors:")
        for k, m in errors:
            print(f"    {k}: {m}")
    print("╚══════════════════════════════════════════════════════════╝")
    print()
    print("  CDN bases (prepend to stored relative URLs):")
    print(f"    Word audio  : {WBW_AUDIO_CDN}")
    print(f"    Verse audio : {VERSE_AUDIO_CDN}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Archive all Quran.com content to SQLite.")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--translations",
                   default=",".join(str(t) for t in DEFAULT_TRANSLATION_IDS))
    p.add_argument("--all-translations", action="store_true",
                   help="Download every available translation (146 total)")
    p.add_argument("--tafsirs", default=",".join(DEFAULT_TAFSIR_SLUGS))
    p.add_argument("--all-tafsirs", action="store_true",
                   help="Download every available tafsir (23 total)")
    p.add_argument("--mushaf", type=int, default=DEFAULT_MUSHAF)
    p.add_argument("--skip-verses", action="store_true")
    p.add_argument("--skip-footnotes", action="store_true",
                   help="Skip footnote download (requires translations to be present)")
    p.add_argument("--skip-tafsirs", action="store_true")
    p.add_argument("--skip-chapter-info", action="store_true")
    p.add_argument("--skip-audio", action="store_true")
    p.add_argument("--chapter", type=int, help="Single chapter only (for testing)")
    p.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    args = p.parse_args()

    global _delay
    _delay = args.delay

    print("╔══════════════════════════════════════════════════════════╗")
    print("║    Quran.com Complete Content Archiver v2               ║")
    print("╚══════════════════════════════════════════════════════════╝")
    log.info("DB=%s  delay=%.1fs  mushaf=%d", args.db, _delay, args.mushaf)

    conn = init_db(args.db)
    chapters = dump_resources(conn)

    if args.all_translations:
        translation_ids = [r[0] for r in conn.execute("SELECT id FROM resources_translations").fetchall()]
        log.info("Using ALL %d translations", len(translation_ids))
    else:
        translation_ids = [int(x.strip()) for x in args.translations.split(",") if x.strip()]

    if args.all_tafsirs:
        tafsir_slugs = [r[0] for r in conn.execute(
            "SELECT slug FROM resources_tafsirs WHERE slug IS NOT NULL").fetchall()]
        log.info("Using ALL %d tafsirs", len(tafsir_slugs))
    else:
        tafsir_slugs = [s.strip() for s in args.tafsirs.split(",") if s.strip()]

    if args.chapter:
        chapters = [c for c in chapters if c["id"] == args.chapter]
        if not chapters:
            log.error("Chapter %d not found", args.chapter)
            sys.exit(1)

    if not args.skip_verses:
        dump_verses(conn, chapters, translation_ids, args.mushaf)

    if not args.skip_footnotes:
        dump_footnotes(conn)

    if not args.skip_tafsirs:
        dump_tafsirs(conn, chapters, tafsir_slugs)

    if not args.skip_chapter_info:
        dump_chapter_info(conn, chapters, CHAPTER_INFO_LANGS)

    if not args.skip_audio:
        dump_audio(conn, chapters)

    print_summary(conn, args.db)
    conn.close()
    log.info("All done.")

if __name__ == "__main__":
    main()
