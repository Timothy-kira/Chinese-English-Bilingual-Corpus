# Dictionary Build And Runtime Notes

This project now has a low-memory Chinese dictionary serving database.

## Built Artifact

Current serving database:

```text
data/processed/dictionary_compact.db
```

Build profile:

- Full CC-CEDICT dictionary.
- hanzi-words `dict/` supplemental headwords, aligned to existing surfaces and stored as pending empty-definition lexemes when CC-CEDICT has no match.
- Full HSK supplements.
- Full Tatoeba Mandarin examples plus linked English sentences.
- Full OpenSubtitles scan, compact serving output only.
- OpenSubtitles stores top daily-sentence candidates per query key instead of all 11.2M rows.
- Example sentences are deduplicated by `(Chinese, English)` after stripping terminal punctuation from both sides.
- FTS5 trigram is built over selected serving sentences, not the full raw subtitle corpus.
- HSK serving tables are included for fast level-based wordbook and character filtering.
- Single-character search suggestions use `lexeme_char_suggestion` instead of a `%char%` scan.

This is intentional for a 3GB RAM / 2 CPU server. The server should only serve the prebuilt SQLite file; it should not rebuild from raw corpora.

## Rebuild Command

Recommended compact rebuild:

```powershell
python -m dictionary_engine.hanzi_words --raw-dir data/raw --download --overwrite --stats
```

```powershell
python -m dictionary_engine.build `
  --raw-dir data/raw `
  --output data/processed/dictionary_compact.db `
  --force `
  --opensubtitles-per-key 8 `
  --progress-every 500000 `
  --fts `
  --cache-mb 192
```

The completed local build scanned 11,203,286 OpenSubtitles sentence pairs and produced a database of about 610.5MB.

Current duplicate cleanup result:

```text
deleted duplicate sentences = 5,692
deleted duplicate example hits = 8,557
remaining sentence rows = 802,920
exact duplicate groups = 0
terminal-punctuation duplicate groups = 0
```

If the HSK serving tables need to be regenerated without rebuilding examples:

```powershell
python -m dictionary_engine.hsk --db data/processed/dictionary_compact.db --rebuild
```

## Query Command

```powershell
python -m dictionary_engine.query 学习 --db data/processed/dictionary_compact.db --limit 8 --explain
```

Benchmark:

```powershell
python -m dictionary_engine.benchmark --db data/processed/dictionary_compact.db --repeat 30 --limit 8
```

HSK levels:

```powershell
python -m dictionary_engine.hsk --db data/processed/dictionary_compact.db --kind levels
```

HSK wordbook, level 1, only multi-character words:

```powershell
python -m dictionary_engine.hsk --db data/processed/dictionary_compact.db --kind words --level 1 --min-len 2 --limit 50
```

HSK characters:

```powershell
python -m dictionary_engine.hsk --db data/processed/dictionary_compact.db --kind chars --level 7-9 --limit 50
```

## HTTP API

Start the low-resource API server:

```powershell
python -m dictionary_engine.server `
  --db data/processed/dictionary_compact.db `
  --host 127.0.0.1 `
  --port 8765 `
  --pool-size 2 `
  --cache-mb 64
```

Endpoints:

```text
GET /health
GET /api/query?q=学习&limit=8
GET /api/suggest?q=学&limit=12
GET /api/suggest?q=study&limit=12
GET /api/hsk/levels
GET /api/hsk/words?level=1&limit=50&offset=0
GET /api/hsk/words?level=1&limit=50&offset=0&min_len=2
GET /api/hsk/chars?level=7-9&limit=50&offset=0
```

HSK level normalization:

- `1` to `6` map to HSK levels 1-6.
- `7`, `7-9`, `new-7`, and `newest-7` are served as the `7-9` bucket.
- `level` omitted returns all levels paginated.

## Server Settings

Open SQLite in read-only immutable mode:

```text
file:dictionary_compact.db?mode=ro&immutable=1
```

Runtime PRAGMAs used by the query module:

```sql
PRAGMA query_only = ON;
PRAGMA temp_store = MEMORY;
PRAGMA cache_size = -96000;
PRAGMA mmap_size = 268435456;
```

For a 3GB server:

- Start with `--cache-mb 64` or `--cache-mb 96`.
- Keep one long-lived SQLite connection per worker process.
- Use 1-2 worker processes, not many workers; the DB is read-only but memory maps and page cache still cost RAM.
- Put the DB on local SSD if possible.
- Do not run the build process on the production server.

## Query Routing

Runtime query flow:

1. Normalize input with NFKC and whitespace removal.
2. Exact dictionary lookup through `lexeme_surface(surface)`.
3. Single-character HSK lookup through `hanzi_char`.
4. Tatoeba examples through `example_hit(query_key, source)`.
5. OpenSubtitles hot examples through `example_hit(query_key, source)`.
6. Duplicate examples are removed with `dictionary_engine.dedupe` during full builds.
7. For 3+ character queries, optional FTS5 trigram fallback over selected serving sentences.
8. HSK wordbook filtering through `hsk_wordbook(hsk_bucket, word_len, simplified, lexeme_id)`.
9. Chinese search-box suggestions through `lexeme_suggestion(first_char, word_len, hsk_rank, surface, lexeme_id)`.
10. Single-character Chinese suggestions through `lexeme_char_suggestion(char, position, hsk_rank, word_len, surface, lexeme_id)`.
11. English reverse suggestions through `lexeme_english_suggestion(term, hsk_rank, word_len, surface, lexeme_id)`.

The frontend also has a default-on offensive-language filter for example sentences. It hides matching raw Tatoeba/OpenSubtitles rows in the browser without changing the database.

Important: single-character and two-character queries do not use FTS5 trigram. SQLite trigram cannot serve fewer than 3 Unicode characters reliably, so these queries use the explicit inverted index.

## Current Benchmark

On the local build with hanzi-words supplements, `limit=8`, `repeat=30`:

```text
summary p95 ~= 0.91 ms
database size ~= 610.5 MB
lexemes = 274,061
supplement pending lexemes = 149,016
sentence rows after dedupe = 802,920
```

All hot dictionary/example queries use indexes. FTS fallback appears as `SCAN f VIRTUAL TABLE INDEX 0:M1`, which is the expected SQLite virtual-table plan for FTS5.
