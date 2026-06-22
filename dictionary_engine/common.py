from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path


SOURCE_TATOEBA = 1
SOURCE_OPENSUBTITLES = 2

SOURCE_NAMES = {
    SOURCE_TATOEBA: "tatoeba",
    SOURCE_OPENSUBTITLES: "opensubtitles",
}

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
HTML_RE = re.compile(r"<[^>]+>")
URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
SPACE_RE = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("\u3000", " ")
    return SPACE_RE.sub(" ", value).strip()


def normalize_query(value: str) -> str:
    return normalize_text(value).replace(" ", "")


def has_cjk(value: str) -> bool:
    return bool(CJK_RE.search(value))


def cjk_count(value: str) -> int:
    return sum(1 for ch in value if CJK_RE.match(ch))


def clean_sentence(value: str) -> str:
    value = normalize_text(value)
    value = HTML_RE.sub("", value)
    value = value.strip(" \t\r\n")
    return value


def sentence_quality(zh: str, en: str | None, source: int) -> int:
    zh_norm = normalize_query(zh)
    zh_len = len(zh_norm)
    if zh_len == 0:
        return -10000

    cjk = cjk_count(zh_norm)
    if cjk == 0:
        return -10000
    cjk_ratio = cjk / max(zh_len, 1)

    score = 1000
    score += 120 if source == SOURCE_TATOEBA else 80

    if 6 <= cjk <= 24:
        score += 220
    elif 25 <= cjk <= 40:
        score += 80
    elif cjk < 3:
        score -= 180
    else:
        score -= min(500, (cjk - 40) * 10)

    if cjk_ratio >= 0.75:
        score += 120
    elif cjk_ratio < 0.45:
        score -= 300

    if en:
        score += 60

    if URL_RE.search(zh) or URL_RE.search(en or ""):
        score -= 600
    if "<" in zh or ">" in zh:
        score -= 300
    if zh.count("...") or zh.count("……"):
        score -= 30
    if len(set(zh_norm)) <= 2 and zh_len >= 4:
        score -= 300

    return score


def should_keep_sentence(zh: str, en: str | None, source: int) -> bool:
    zh_norm = normalize_query(zh)
    if not zh_norm or not has_cjk(zh_norm):
        return False
    if len(zh_norm) > 90:
        return False
    if URL_RE.search(zh) or HTML_RE.search(zh):
        return False
    if source == SOURCE_OPENSUBTITLES and cjk_count(zh_norm) < 2:
        return False
    return sentence_quality(zh, en, source) > 350


def batched(items: Iterable[Sequence[object]], size: int) -> Iterator[list[Sequence[object]]]:
    batch: list[Sequence[object]] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def configure_build_connection(conn: sqlite3.Connection, cache_mb: int) -> None:
    pragmas = [
        ("journal_mode", "OFF"),
        ("synchronous", "OFF"),
        ("temp_store", "MEMORY"),
        ("locking_mode", "EXCLUSIVE"),
        ("cache_size", -max(cache_mb, 32) * 1000),
    ]
    for key, value in pragmas:
        conn.execute(f"PRAGMA {key}={value}")


def configure_read_connection(conn: sqlite3.Connection, cache_mb: int = 96) -> None:
    pragmas = [
        ("query_only", "ON"),
        ("temp_store", "MEMORY"),
        ("cache_size", -max(cache_mb, 16) * 1000),
        ("mmap_size", 268435456),
    ]
    for key, value in pragmas:
        conn.execute(f"PRAGMA {key}={value}")


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE metadata (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE lexeme (
          lexeme_id INTEGER PRIMARY KEY,
          simplified TEXT NOT NULL,
          traditional TEXT NOT NULL,
          pinyin_numbered TEXT NOT NULL,
          pinyin_display TEXT,
          definitions_json TEXT NOT NULL,
          hsk_word_level TEXT,
          hsk_char_level TEXT,
          cedict_line TEXT NOT NULL,
          definition_status TEXT NOT NULL DEFAULT 'cedict'
        );

        CREATE TABLE lexeme_surface (
          surface TEXT NOT NULL,
          lexeme_id INTEGER NOT NULL,
          surface_kind INTEGER NOT NULL,
          word_len INTEGER NOT NULL,
          hsk_rank INTEGER,
          PRIMARY KEY (surface, lexeme_id, surface_kind)
        ) WITHOUT ROWID;

        CREATE TABLE lexeme_source (
          lexeme_id INTEGER NOT NULL,
          source_name TEXT NOT NULL,
          source_file TEXT NOT NULL,
          source_row INTEGER NOT NULL,
          raw_surface TEXT NOT NULL,
          normalized_surface TEXT NOT NULL,
          status TEXT NOT NULL,
          pinyin_display TEXT,
          PRIMARY KEY (lexeme_id, source_file, source_row, normalized_surface)
        ) WITHOUT ROWID;

        CREATE TABLE hanzi_char (
          hanzi TEXT PRIMARY KEY,
          hsk_level TEXT,
          writing_level TEXT,
          traditional TEXT,
          examples TEXT
        ) WITHOUT ROWID;

        CREATE TABLE sentence (
          sentence_id INTEGER PRIMARY KEY,
          source INTEGER NOT NULL,
          external_id TEXT,
          zh TEXT NOT NULL,
          en TEXT,
          zh_norm TEXT NOT NULL,
          quality_score INTEGER NOT NULL,
          length_chars INTEGER NOT NULL
        );

        CREATE TABLE example_hit (
          query_key TEXT NOT NULL,
          source INTEGER NOT NULL,
          score INTEGER NOT NULL,
          sentence_id INTEGER NOT NULL,
          hit_kind INTEGER NOT NULL,
          PRIMARY KEY (query_key, source, score DESC, sentence_id)
        ) WITHOUT ROWID;

        CREATE TABLE hsk_wordbook (
          hsk_bucket INTEGER NOT NULL,
          level_label TEXT NOT NULL,
          lexeme_id INTEGER NOT NULL,
          simplified TEXT NOT NULL,
          traditional TEXT NOT NULL,
          pinyin_numbered TEXT NOT NULL,
          word_len INTEGER NOT NULL,
          PRIMARY KEY (hsk_bucket, lexeme_id)
        ) WITHOUT ROWID;

        CREATE TABLE hsk_charbook (
          hsk_bucket INTEGER NOT NULL,
          level_label TEXT NOT NULL,
          hanzi TEXT NOT NULL,
          traditional TEXT,
          writing_level TEXT,
          examples TEXT,
          PRIMARY KEY (hsk_bucket, hanzi)
        ) WITHOUT ROWID;

        CREATE TABLE lexeme_suggestion (
          surface TEXT NOT NULL,
          lexeme_id INTEGER NOT NULL,
          pinyin_numbered TEXT NOT NULL,
          hsk_rank INTEGER,
          word_len INTEGER NOT NULL,
          first_char TEXT NOT NULL,
          PRIMARY KEY (surface, lexeme_id)
        ) WITHOUT ROWID;

        CREATE TABLE lexeme_char_suggestion (
          char TEXT NOT NULL,
          position INTEGER NOT NULL,
          surface TEXT NOT NULL,
          lexeme_id INTEGER NOT NULL,
          pinyin_numbered TEXT NOT NULL,
          hsk_rank INTEGER,
          word_len INTEGER NOT NULL,
          PRIMARY KEY (char, lexeme_id, surface, position)
        ) WITHOUT ROWID;

        CREATE TABLE lexeme_english_suggestion (
          term TEXT NOT NULL,
          surface TEXT NOT NULL,
          lexeme_id INTEGER NOT NULL,
          pinyin_numbered TEXT NOT NULL,
          hsk_rank INTEGER,
          word_len INTEGER NOT NULL,
          PRIMARY KEY (term, lexeme_id, surface)
        ) WITHOUT ROWID;
        """
    )


def create_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_lexeme_surface_rank
        ON lexeme_surface(surface, hsk_rank, lexeme_id);

        CREATE INDEX IF NOT EXISTS idx_lexeme_source_surface
        ON lexeme_source(normalized_surface, lexeme_id);

        CREATE INDEX IF NOT EXISTS idx_sentence_source_quality
        ON sentence(source, quality_score DESC, sentence_id);

        CREATE INDEX IF NOT EXISTS idx_hsk_wordbook_order
        ON hsk_wordbook(hsk_bucket, word_len, simplified, lexeme_id);

        CREATE INDEX IF NOT EXISTS idx_hsk_charbook_order
        ON hsk_charbook(hsk_bucket, hanzi);

        CREATE INDEX IF NOT EXISTS idx_lexeme_suggestion_prefix
        ON lexeme_suggestion(first_char, word_len, hsk_rank, surface, lexeme_id);

        CREATE INDEX IF NOT EXISTS idx_lexeme_suggestion_surface
        ON lexeme_suggestion(surface, word_len, hsk_rank, lexeme_id);

        CREATE INDEX IF NOT EXISTS idx_lexeme_char_suggestion_char
        ON lexeme_char_suggestion(char, position, hsk_rank, word_len, surface, lexeme_id);

        CREATE INDEX IF NOT EXISTS idx_lexeme_english_suggestion_term
        ON lexeme_english_suggestion(term, hsk_rank, word_len, surface, lexeme_id);
        """
    )


def create_selected_fts(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS sentence_fts
        USING fts5(
          zh_norm,
          content='sentence',
          content_rowid='sentence_id',
          tokenize='trigram',
          detail=none,
          columnsize=0
        );

        INSERT INTO sentence_fts(sentence_fts) VALUES('rebuild');
        INSERT INTO sentence_fts(sentence_fts) VALUES('optimize');
        """
    )


def write_metadata(conn: sqlite3.Connection, **values: object) -> None:
    rows = [(key, json.dumps(value, ensure_ascii=False, sort_keys=True)) for key, value in values.items()]
    conn.executemany(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
        rows,
    )


def quote_fts5_token(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def make_trigram_match_query(value: str) -> str:
    value = normalize_query(value)
    if len(value) < 3:
        return ""
    trigrams = []
    seen = set()
    for index in range(0, len(value) - 2):
        trigram = value[index : index + 3]
        if trigram in seen:
            continue
        seen.add(trigram)
        trigrams.append(quote_fts5_token(trigram))
    return " AND ".join(trigrams)


def open_readonly_db(
    path: Path,
    immutable: bool = True,
    cache_mb: int = 96,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    if immutable:
        uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=check_same_thread)
    else:
        conn = sqlite3.connect(
            f"file:{path.resolve()}?mode=ro",
            uri=True,
            check_same_thread=check_same_thread,
        )
    configure_read_connection(conn, cache_mb=cache_mb)
    conn.row_factory = sqlite3.Row
    return conn
