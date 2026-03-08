# Quran Data Archiver

A single-file Python script that downloads **every piece of content** from [Quran.com](https://quran.com) and stores it in a fully normalised, relational SQLite database.

Built by reverse-engineering the live network requests made by the Quran.com browser app.

---

## What it downloads

| Content | Detail |
|---|---|
| **114 chapters** | Arabic names (3 scripts), revelation order/place, page range, translated name |
| **30 Juz** | Verse mapping, first/last verse IDs |
| **6,236 verses** | 4 Arabic text variants (Uthmani, Imlaei, Indopak, QPC-Hafs), juz/hizb/ruku/manzil/page, sajdah markers |
| **~83,000 words** | Per-word text in 4 scripts, transliteration, English translation, word-audio CDN URL |
| **Translations** | 9 by default (6 English + 3 Urdu); `--all-translations` for all **146** across **79 languages** |
| **Tafsirs** | Ibn Kathir (EN) by default; `--all-tafsirs` for all **23** available |
| **Chapter intro text** | Maududi's background text in **English, Urdu, Arabic** for all 114 chapters |
| **Verse audio URLs** | Per-verse MP3 CDN paths for all **20 reciters** (~124,000 rows) |
| **Chapter audio URLs** | Full-chapter MP3 URL for all **20 reciters** |
| **Recitation styles** | Murattal, Mujawwad, Muallim, Ijaza — with descriptions |
| **79 languages** | ISO codes, native names, direction (LTR/RTL), translation count |
| **146 translation resources** | Name, author, slug, language for every translator |
| **23 tafsir resources** | Name, author, slug, language for every tafsir |
| **20 audio reciters** | Name, style, full metadata |
| **Mushaf page layout** | Page-to-verse mapping for the Medina Mushaf (mushaf 7) |

**Total: ~175 MB SQLite database** covering the complete Quran.com content catalogue.

---

## APIs used

Two complementary APIs were identified through browser network inspection:

| API | Base URL | Used for |
|---|---|---|
| **QDC Proxy** | `https://quran.com/api/proxy/content/api/qdc` | Verses, words, translations, tafsirs, resources |
| **Public V4** | `https://api.quran.com/api/v4` | Chapter intro text, verse audio, chapter audio |

**CDN bases** (stored as relative paths in the DB — prepend to reconstruct full URLs):

| Content | CDN Base |
|---|---|
| Word-level audio | `https://audio.qurancdn.com/` |
| Verse/chapter audio | `https://download.quranicaudio.com/` |

### Confirmed QDC endpoints

```
GET /chapters?language=en
GET /juzs
GET /resources/translations
GET /resources/tafsirs?language=en
GET /resources/languages
GET /resources/recitation_styles
GET /audio/reciters
GET /pages/lookup?mushaf={n}&chapter_number={n}
GET /verses/by_chapter/{id}?words=true&translations=...&per_page=50&page=N
GET /verses/by_page/{page}?words=true&per_page=all&...
GET /verses/by_key/{verse_key}?words=true&translations=...
GET /tafsirs/{slug}/by_chapter/{chapter_id}?per_page=50&page=N&locale=en
GET /tafsirs/{slug}/by_ayah/{verse_key}?locale=en
GET /qiraat/matrix/count_within_range?from={key}&to={key}
GET /hadith_references/count_within_range?from={key}&to={key}&language=en
GET /layered_translations/count_within_range?from={key}&to={key}&language=en
```

### Confirmed V4 endpoints

```
GET /chapters/{id}/info?language={lang}
GET /recitations/{reciter_id}/by_chapter/{chapter_id}
GET /chapter_recitations/{reciter_id}/{chapter_id}
GET /verses/by_chapter/{id}?words=true&per_page=N&language=en
GET /verses/by_key/{verse_key}?words=true
GET /juzs/{juz_number}
GET /search?q={query}&size=N&page=N&language=en
GET /resources/recitation_styles
```

---

## Database schema

```
languages               ← 79 languages (ISO codes, direction)
recitation_styles       ← murattal / mujawwad / muallim / ijaza
resources_translations  ← 146 translation resources (name, slug, language)
resources_tafsirs       ← 23 tafsir resources (name, slug, language)
audio_reciters          ← 20 reciters (name, style)

chapters                ← 114 surahs
  └── chapter_info      ← intro text per chapter per language (EN/UR/AR)
  └── pages_lookup      ← page layout (Medina Mushaf)
  └── chapter_audio     ← full-chapter MP3 URL per reciter

juzs                    ← 30 juz divisions

verses  ←── chapter_id → chapters
  └── words             ← ~83k words with text/transliteration/audio
  └── translations      ←── resource_id → resources_translations
  └── tafsirs           ←── slug → resources_tafsirs
  └── verse_audio       ←── reciter_id → audio_reciters

download_progress       ← task tracking (resumable runs)
```

### Key relationships

```sql
-- Get all verses in a chapter with Khattab translation
SELECT v.verse_key, v.text_uthmani, t.text AS translation_en
FROM verses v
JOIN translations t ON t.verse_key = v.verse_key AND t.resource_id = 131
WHERE v.chapter_id = 2
ORDER BY v.verse_number;

-- Get all words in a verse with transliteration
SELECT w.position, w.text_uthmani, w.transliteration, w.translation_en
FROM words w
WHERE w.verse_key = '2:255'
ORDER BY w.position;

-- Get Ibn Kathir tafsir for a verse
SELECT t.text
FROM tafsirs t
WHERE t.verse_key = '2:255' AND t.slug = 'en-tafisr-ibn-kathir';

-- Get verse audio URLs for a reciter (Mishary Alafasy = reciter_id 7)
SELECT va.verse_key, 'https://download.quranicaudio.com/' || va.url AS full_url
FROM verse_audio va
WHERE va.reciter_id = 7 AND va.verse_key LIKE '1:%';

-- Get full word audio URL
SELECT w.verse_key, w.position, 'https://audio.qurancdn.com/' || w.audio_url AS full_url
FROM words w
WHERE w.verse_key = '1:1' AND w.char_type = 'word';

-- Get chapter intro text in Urdu
SELECT ci.short_text, ci.text, ci.source
FROM chapter_info ci
WHERE ci.chapter_id = 1 AND ci.language_name = 'urdu';

-- List all available translations
SELECT id, name, author_name, language_name FROM resources_translations ORDER BY language_name, name;

-- List all available tafsirs
SELECT id, name, author_name, language_name, slug FROM resources_tafsirs;
```

---

## Installation

This project uses [uv](https://docs.astral.sh/uv/) — a fast, modern Python package manager.

```bash
# Install uv (once, system-wide)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone
git clone https://github.com/faisal-saddique/quran-data-archiver.git
cd quran-data-archiver
```

---

## Usage

### Option A — uv project (recommended)

```bash
# Install dependencies and run (uv handles the venv automatically)
uv run quran_dump.py

# With flags
uv run quran_dump.py --all-translations
uv run quran_dump.py --all-tafsirs
uv run quran_dump.py --chapter 36
```

`uv run` reads `pyproject.toml`, creates a virtual environment, installs `requests`, and runs the script — all in one command. The lockfile (`uv.lock`) guarantees reproducible installs.

### Option B — zero-setup single script (PEP 723 inline metadata)

The script embeds its own dependency declaration at the top. You can run it directly via `uv run` **without cloning the project**:

```bash
uv run https://raw.githubusercontent.com/faisal-saddique/quran-data-archiver/main/quran_dump.py
```

Or locally without installing anything first:

```bash
uv run quran_dump.py          # uv auto-installs requests into an isolated cache
```

### Option C — plain Python

```bash
pip install requests
python quran_dump.py
```

---

## All flags

```bash
# Full download — default translations (9) + Ibn Kathir tafsir
uv run quran_dump.py

# All 146 translations across all languages
uv run quran_dump.py --all-translations

# All 23 tafsirs
uv run quran_dump.py --all-tafsirs

# Both complete
uv run quran_dump.py --all-translations --all-tafsirs

# Skip tafsirs (faster)
uv run quran_dump.py --skip-tafsirs

# Resource lists only (fast, no verses)
uv run quran_dump.py --skip-verses --skip-tafsirs --skip-chapter-info --skip-audio

# Test a single chapter
uv run quran_dump.py --chapter 36

# Slower/more polite (default: 0.6s + ±0.3s jitter)
uv run quran_dump.py --delay 1.5

# Custom DB path
uv run quran_dump.py --db /path/to/my.sqlite3

# Specific translations by ID
uv run quran_dump.py --translations 131,85,95,97
```

### Flags

| Flag | Default | Description |
|---|---|---|
| `--db` | `quran_dump.sqlite3` | Output database path |
| `--translations` | 9 popular IDs | Comma-separated translation resource IDs |
| `--all-translations` | off | Download all 146 translations |
| `--tafsirs` | `en-tafisr-ibn-kathir` | Comma-separated tafsir slugs |
| `--all-tafsirs` | off | Download all 23 tafsirs |
| `--mushaf` | `7` | Mushaf layout ID (7 = Medina Mushaf) |
| `--skip-verses` | off | Skip verse/word/translation download |
| `--skip-tafsirs` | off | Skip tafsir download |
| `--skip-chapter-info` | off | Skip chapter intro text |
| `--skip-audio` | off | Skip audio URL download |
| `--chapter` | all | Download a single chapter only |
| `--delay` | `0.6` | Base seconds between requests |

---

## Resumability

Every chapter/reciter/language combination is tracked as an individual task in the `download_progress` table. Re-running the script at any time will skip already-completed tasks and pick up exactly where it left off.

```bash
# Check progress
sqlite3 quran_dump.sqlite3 \
  "SELECT status, COUNT(*) as n FROM download_progress GROUP BY status;"

# See any errors
sqlite3 quran_dump.sqlite3 \
  "SELECT task_key, error_msg FROM download_progress WHERE status='error';"

# Reset a specific chapter to re-download it
sqlite3 quran_dump.sqlite3 \
  "UPDATE download_progress SET status='pending' WHERE task_key='verses:ch:2';"
```

---

## Default translations downloaded

| ID | Name | Language |
|---|---|---|
| 131 | Dr. Mustafa Khattab — The Clear Quran | English |
| 85 | M.A.S. Abdel Haleem | English |
| 95 | Saheeh International | English |
| 97 | Pickthall | English |
| 22 | Yusuf Ali (original) | English |
| 20 | Muhammad Sarwar | English |
| 203 | Dr. Wahiduddin Khan | Urdu |
| 54 | Fatah Muhammad Jalandhari | Urdu |
| 149 | Muhammad Taqi Usmani | Urdu |

Run with `--all-translations` to get all 146 across all 79 languages.

---

## Audio reciters

20 reciters are available. The reciter `id` in the `audio_reciters` table is used as the key in `verse_audio` and `chapter_audio`.

| ID | Name | Style |
|---|---|---|
| 7 | Mishari Rashid al-Afasy | Murattal |
| 3 | Abdur-Rahman as-Sudais | Murattal |
| 4 | Abu Bakr al-Shatri | Murattal |
| 1 | AbdulBaset AbdulSamad | Murattal |
| 2 | AbdulBaset AbdulSamad | Mujawwad |
| 9 | Mohamed Siddiq al-Minshawi | Murattal |
| 6 | Mahmoud Khalil Al-Husary | Murattal |
| 13 | Sa'ad al-Ghamdi | Murattal |
| 10 | Sa'ud ash-Shuraim | Murattal |
| … | *(14 more in DB)* | … |

```sql
-- Full reciter list
SELECT id, name, style_name FROM audio_reciters ORDER BY id;
```

---

## Notes

- **Brotli encoding**: The QDC proxy returns brotli-compressed responses when the browser's `Accept-Encoding: br` header is present. The script explicitly requests `gzip, deflate` only, since the `requests` library cannot decode brotli without an optional extra package.
- **Rate limiting**: Default delay is 0.6 s ± 0.3 s jitter between every request. User-agent rotates every 60 requests.
- **Morphology not available**: Word-level grammatical analysis (root, part-of-speech, grammar parsing) returns 404 on both the QDC and V4 APIs. It appears to be served from a pre-rendered static dataset embedded in the page JavaScript, not exposed through the API.
- **Search not archived**: The `/search` endpoint is functional and included in the API reference above, but search results are dynamic and not suitable for archiving.

---

## License

MIT
