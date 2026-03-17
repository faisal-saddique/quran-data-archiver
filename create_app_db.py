#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
create_app_db.py — Build a lean, app-optimised SQLite database from quran_dump.sqlite3.

Reads the full dump database and writes a smaller, cleaned database suitable
for bundling inside a mobile / desktop Flutter app.

What is kept (raw_json stripped everywhere):
  chapters          — 114 rows, all fields except raw_json
  juzs              — 30 rows, all fields except raw_json / db_id
  verses            — 6 236 rows, all fields except raw_json / rub_el_hizb_number
  words             — ~77 k rows (all char types), all fields except raw_json
  translations      — all available translations for all verses
  chapter_info      — EN + UR rows (Maududi intro text)
  resources_translations — full lookup table
  audio_reciters    — full lookup table (small)

What is omitted:
  tafsirs           — ~69 MB, too large for bundling
  resources_tafsirs — companion table to tafsirs
  verse_audio       — CDN URL table; fetch at runtime if needed
  chapter_audio     — same
  pages_lookup      — derivable from chapters.pages_from/to
  download_progress — internal archiver state

Usage:
  python create_app_db.py                                  # defaults
  python create_app_db.py --src /path/to/quran_dump.sqlite3 --dst one_muslim_quran.sqlite3
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path


# ── helpers ───────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(msg, flush=True)


def run(con: sqlite3.Connection, sql: str, *args) -> sqlite3.Cursor:
    return con.execute(sql, args)


# ── schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
-- ── Reference tables ──────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS resources_translations (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    author_name   TEXT,
    slug          TEXT,
    language_name TEXT,
    language_iso  TEXT
);

CREATE TABLE IF NOT EXISTS audio_reciters (
    id            INTEGER PRIMARY KEY,
    reciter_id    INTEGER,
    name          TEXT NOT NULL,
    style_name    TEXT,
    style_desc    TEXT
);

-- ── Core tables ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS chapters (
    id                INTEGER PRIMARY KEY,   -- 1–114
    name_simple       TEXT    NOT NULL,      -- "Al-Fatihah"
    name_complex      TEXT,                  -- with diacritics/transliteration
    name_arabic       TEXT,                  -- "الفاتحة"
    revelation_place  TEXT,                  -- "makkah" | "madinah"
    revelation_order  INTEGER,
    bismillah_pre     INTEGER DEFAULT 0,     -- 1 = starts with Bismillah
    verses_count      INTEGER NOT NULL,
    pages_from        INTEGER,
    pages_to          INTEGER,
    translated_name   TEXT                   -- "The Opener"
);

CREATE TABLE IF NOT EXISTS juzs (
    juz_number    INTEGER PRIMARY KEY,       -- 1–30
    verse_mapping TEXT,                      -- JSON: {"1":"1-7", ...}
    first_verse_id INTEGER,
    last_verse_id  INTEGER,
    verses_count   INTEGER
);

CREATE TABLE IF NOT EXISTS verses (
    id                  INTEGER PRIMARY KEY,
    verse_number        INTEGER NOT NULL,
    verse_key           TEXT    UNIQUE NOT NULL,  -- "1:1"
    chapter_id          INTEGER NOT NULL REFERENCES chapters(id),
    juz_number          INTEGER REFERENCES juzs(juz_number),
    hizb_number         INTEGER,
    ruku_number         INTEGER,
    manzil_number       INTEGER,
    sajdah_type         TEXT,
    sajdah_number       INTEGER,
    page_number         INTEGER,
    text_uthmani        TEXT,
    text_imlaei_simple  TEXT,
    text_indopak        TEXT
);

CREATE INDEX IF NOT EXISTS idx_verses_chapter ON verses(chapter_id);
CREATE INDEX IF NOT EXISTS idx_verses_juz     ON verses(juz_number);
CREATE INDEX IF NOT EXISTS idx_verses_page    ON verses(page_number);

CREATE TABLE IF NOT EXISTS words (
    id                 INTEGER PRIMARY KEY,
    verse_id           INTEGER NOT NULL REFERENCES verses(id),
    verse_key          TEXT    NOT NULL,
    position           INTEGER NOT NULL,
    char_type          TEXT,                -- "word" | "end" | "pause"
    page_number        INTEGER,
    text_uthmani       TEXT,
    text_imlaei_simple TEXT,
    text_indopak       TEXT,
    qpc_uthmani_hafs   TEXT,
    transliteration    TEXT,
    translation_en     TEXT,
    audio_url          TEXT,                -- relative CDN path
    UNIQUE(verse_id, position)
);

CREATE INDEX IF NOT EXISTS idx_words_verse ON words(verse_id);
CREATE INDEX IF NOT EXISTS idx_words_key   ON words(verse_key);

CREATE TABLE IF NOT EXISTS translations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    verse_id      INTEGER NOT NULL REFERENCES verses(id),
    verse_key     TEXT    NOT NULL,
    resource_id   INTEGER NOT NULL REFERENCES resources_translations(id),
    resource_name TEXT,
    language_name TEXT,
    text          TEXT,
    UNIQUE(verse_key, resource_id)
);

CREATE INDEX IF NOT EXISTS idx_trans_verse    ON translations(verse_id);
CREATE INDEX IF NOT EXISTS idx_trans_resource ON translations(resource_id);
CREATE INDEX IF NOT EXISTS idx_trans_verse_key ON translations(verse_key);

CREATE TABLE IF NOT EXISTS chapter_info (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id    INTEGER NOT NULL REFERENCES chapters(id),
    language_name TEXT    NOT NULL,
    short_text    TEXT,
    text          TEXT,
    source        TEXT,
    UNIQUE(chapter_id, language_name)
);

CREATE INDEX IF NOT EXISTS idx_chapter_info_ch ON chapter_info(chapter_id);
"""


# ── copy helpers ──────────────────────────────────────────────────────────────

def copy_resources_translations(src: sqlite3.Connection, dst: sqlite3.Connection) -> None:
    log("  → resources_translations…")
    rows = src.execute(
        "SELECT id, name, author_name, slug, language_name, language_iso FROM resources_translations"
    ).fetchall()
    dst.executemany(
        "INSERT OR IGNORE INTO resources_translations VALUES (?,?,?,?,?,?)", rows
    )
    log(f"     {len(rows)} rows")


def copy_audio_reciters(src: sqlite3.Connection, dst: sqlite3.Connection) -> None:
    log("  → audio_reciters…")
    # Inspect the columns present in the source table
    rows = src.execute(
        "SELECT id, reciter_id, name, style_name, style_desc FROM audio_reciters"
    ).fetchall()
    dst.executemany(
        "INSERT OR IGNORE INTO audio_reciters VALUES (?,?,?,?,?)", rows
    )
    log(f"     {len(rows)} rows")


def copy_chapters(src: sqlite3.Connection, dst: sqlite3.Connection) -> None:
    log("  → chapters…")
    rows = src.execute(
        """SELECT id, name_simple, name_complex, name_arabic, revelation_place,
                  revelation_order, bismillah_pre, verses_count, pages_from, pages_to,
                  translated_name
           FROM chapters ORDER BY id"""
    ).fetchall()
    dst.executemany(
        "INSERT OR IGNORE INTO chapters VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    log(f"     {len(rows)} chapters")


def copy_juzs(src: sqlite3.Connection, dst: sqlite3.Connection) -> None:
    log("  → juzs…")
    rows = src.execute(
        "SELECT juz_number, verse_mapping, first_verse_id, last_verse_id, verses_count FROM juzs"
    ).fetchall()
    dst.executemany(
        "INSERT OR IGNORE INTO juzs VALUES (?,?,?,?,?)", rows
    )
    log(f"     {len(rows)} juzs")


def copy_verses(src: sqlite3.Connection, dst: sqlite3.Connection) -> None:
    log("  → verses…")
    rows = src.execute(
        """SELECT id, verse_number, verse_key, chapter_id, juz_number, hizb_number,
                  ruku_number, manzil_number, sajdah_type, sajdah_number, page_number,
                  text_uthmani, text_imlaei_simple, text_indopak
           FROM verses ORDER BY id"""
    ).fetchall()
    dst.executemany(
        "INSERT OR IGNORE INTO verses VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    log(f"     {len(rows)} verses")


def copy_words(src: sqlite3.Connection, dst: sqlite3.Connection) -> None:
    log("  → words (all char types)…")
    rows = src.execute(
        """SELECT id, verse_id, verse_key, position, char_type, page_number,
                  text_uthmani, text_imlaei_simple, text_indopak, qpc_uthmani_hafs,
                  transliteration, translation_en, audio_url
           FROM words ORDER BY verse_id, position"""
    ).fetchall()
    dst.executemany(
        "INSERT OR IGNORE INTO words VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    log(f"     {len(rows)} word rows")


def copy_translations(src: sqlite3.Connection, dst: sqlite3.Connection) -> None:
    log("  → translations (all available)…")
    rows = src.execute(
        """SELECT verse_id, verse_key, resource_id, resource_name, language_name, text
           FROM translations ORDER BY verse_id, resource_id"""
    ).fetchall()
    # Re-insert without the original autoincrement id
    dst.executemany(
        "INSERT OR IGNORE INTO translations(verse_id, verse_key, resource_id, resource_name, language_name, text) VALUES (?,?,?,?,?,?)",
        rows,
    )
    log(f"     {len(rows)} translation rows")


def copy_chapter_info(src: sqlite3.Connection, dst: sqlite3.Connection, langs: list[str]) -> None:
    log(f"  → chapter_info ({', '.join(langs)})…")
    placeholders = ",".join("?" * len(langs))
    rows = src.execute(
        f"""SELECT chapter_id, language_name, short_text, text, source
            FROM chapter_info WHERE language_name IN ({placeholders})
            ORDER BY chapter_id, language_name""",
        langs,
    ).fetchall()
    dst.executemany(
        "INSERT OR IGNORE INTO chapter_info(chapter_id, language_name, short_text, text, source) VALUES (?,?,?,?,?)",
        rows,
    )
    log(f"     {len(rows)} rows")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description="Build lean app database from quran_dump.sqlite3")
    p.add_argument("--src", default="quran_dump.sqlite3", help="Source dump database path")
    p.add_argument("--dst", default="one_muslim_quran.sqlite3", help="Output app database path")
    p.add_argument(
        "--chapter-info-langs",
        default="english,urdu",
        help="Comma-separated language_name values to copy from chapter_info (default: english,urdu)",
    )
    args = p.parse_args()

    src_path = Path(args.src)
    dst_path = Path(args.dst)
    langs = [l.strip() for l in args.chapter_info_langs.split(",")]

    if not src_path.exists():
        print(f"ERROR: source database not found: {src_path}", file=sys.stderr)
        return 1

    if dst_path.exists():
        log(f"Removing existing {dst_path}")
        dst_path.unlink()

    log(f"Source : {src_path}  ({src_path.stat().st_size / 1_048_576:.1f} MB)")
    log(f"Output : {dst_path}")
    log("")

    t0 = time.time()

    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row

    dst = sqlite3.connect(dst_path)
    dst.execute("PRAGMA journal_mode=WAL")
    dst.execute("PRAGMA synchronous=NORMAL")
    dst.execute("PRAGMA foreign_keys=OFF")   # speeds up bulk inserts

    # Create schema
    for stmt in SCHEMA.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            dst.execute(stmt)
    dst.commit()

    log("Copying tables…")
    copy_resources_translations(src, dst)
    copy_audio_reciters(src, dst)
    copy_chapters(src, dst)
    copy_juzs(src, dst)
    copy_verses(src, dst)
    copy_words(src, dst)
    copy_translations(src, dst)
    copy_chapter_info(src, dst, langs)

    dst.execute("PRAGMA foreign_keys=ON")
    dst.commit()

    log("")
    log("Running VACUUM to compact…")
    dst.execute("VACUUM")
    dst.commit()

    src.close()
    dst.close()

    elapsed = time.time() - t0
    out_size = dst_path.stat().st_size / 1_048_576
    log("")
    log(f"Done in {elapsed:.1f}s  →  {dst_path}  ({out_size:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
