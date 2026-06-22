from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .common import (
    SOURCE_NAMES,
    SOURCE_OPENSUBTITLES,
    SOURCE_TATOEBA,
    make_trigram_match_query,
    normalize_query,
    open_readonly_db,
)
from .suggest import to_simplified, convert_pinyin


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def lookup_dictionary(conn: sqlite3.Connection, query: str, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
          l.lexeme_id,
          l.simplified,
          l.traditional,
          l.pinyin_numbered,
          l.pinyin_display,
          l.definitions_json,
          l.hsk_word_level,
          l.hsk_char_level,
          l.definition_status,
          s.hsk_rank,
          (
            SELECT group_concat(DISTINCT ls.source_name)
            FROM lexeme_source ls
            WHERE ls.lexeme_id = l.lexeme_id
          ) AS source_names
        FROM lexeme_surface s
        JOIN lexeme l ON l.lexeme_id = s.lexeme_id
        WHERE s.surface = ?
        ORDER BY COALESCE(s.hsk_rank, 99), length(l.simplified), l.lexeme_id
        LIMIT ?
        """,
        (query, limit),
    ).fetchall()

    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                "lexeme_id": row["lexeme_id"],
                "simplified": to_simplified(row["simplified"]),
                "traditional": row["traditional"],
                "pinyin": row["pinyin_display"] or convert_pinyin(row["pinyin_numbered"]),
                "definitions": json.loads(row["definitions_json"]),
                "hsk_word_level": row["hsk_word_level"],
                "hsk_char_level": row["hsk_char_level"],
                "definition_status": row["definition_status"],
                "sources": [item for item in (row["source_names"] or "").split(",") if item],
            }
        )
    return result


def lookup_char(conn: sqlite3.Connection, query: str) -> dict[str, Any] | None:
    if len(query) != 1:
        return None
    row = conn.execute(
        """
        SELECT hanzi, hsk_level, writing_level, traditional, examples
        FROM hanzi_char
        WHERE hanzi = ? OR traditional = ?
        LIMIT 1
        """,
        (query, query),
    ).fetchone()
    if row is None:
        return None
    return {
        "hanzi": to_simplified(row["hanzi"]),
        "hsk_level": row["hsk_level"],
        "writing_level": row["writing_level"],
        "traditional": row["traditional"],
        "examples": row["examples"],
    }


def lookup_examples(
    conn: sqlite3.Connection,
    query: str,
    source: int,
    limit: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT s.sentence_id, s.zh, s.en, h.score, h.hit_kind
        FROM example_hit h
        JOIN sentence s ON s.sentence_id = h.sentence_id
        WHERE h.query_key = ?
          AND h.source = ?
        ORDER BY h.score DESC, h.sentence_id
        LIMIT ?
        """,
        (query, source, limit),
    ).fetchall()

    return [
        {
            "sentence_id": row["sentence_id"],
            "source": SOURCE_NAMES[source],
            "zh": to_simplified(row["zh"]),
            "en": row["en"],
            "score": row["score"],
            "hit_kind": row["hit_kind"],
        }
        for row in rows
    ]


def lookup_fts(
    conn: sqlite3.Connection,
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    if len(query) < 3 or not table_exists(conn, "sentence_fts"):
        return []
    match_query = make_trigram_match_query(query)
    if not match_query:
        return []
    rows = conn.execute(
        """
        SELECT s.sentence_id, s.source, s.zh, s.en, s.quality_score
        FROM sentence_fts f
        JOIN sentence s ON s.sentence_id = f.rowid
        WHERE sentence_fts MATCH ?
          AND instr(s.zh_norm, ?) > 0
        ORDER BY s.quality_score DESC, s.length_chars, s.sentence_id
        LIMIT ?
        """,
        (match_query, query, limit),
    ).fetchall()
    return [
        {
            "sentence_id": row["sentence_id"],
            "source": SOURCE_NAMES.get(row["source"], str(row["source"])),
            "zh": to_simplified(row["zh"]),
            "en": row["en"],
            "score": row["quality_score"],
        }
        for row in rows
    ]


def lookup(conn: sqlite3.Connection, raw_query: str, limit: int = 8) -> dict[str, Any]:
    from .suggest import _TRAD_MAP, init_conversion_map
    if not _TRAD_MAP:
        init_conversion_map(conn)
    query = normalize_query(raw_query)
    started = time.perf_counter()

    dictionary = lookup_dictionary(conn, query, limit=limit)
    char = lookup_char(conn, query)
    tatoeba = lookup_examples(conn, query, SOURCE_TATOEBA, limit=limit)
    opensubtitles = lookup_examples(conn, query, SOURCE_OPENSUBTITLES, limit=limit)
    fts = lookup_fts(conn, query, limit=limit) if len(query) >= 3 and len(opensubtitles) < limit else []

    elapsed_ms = (time.perf_counter() - started) * 1000
    return {
        "query": raw_query,
        "normalized_query": query,
        "elapsed_ms": round(elapsed_ms, 3),
        "dictionary": dictionary,
        "char": char,
        "examples": {
            "tatoeba": tatoeba,
            "opensubtitles": opensubtitles,
            "fts": fts,
        },
    }


def explain_plans(conn: sqlite3.Connection, query: str, limit: int) -> dict[str, list[str]]:
    normalized = normalize_query(query)
    plans: dict[str, list[str]] = {}
    statements = {
        "dictionary": (
            """
            SELECT l.lexeme_id
            FROM lexeme_surface s
            JOIN lexeme l ON l.lexeme_id = s.lexeme_id
            WHERE s.surface = ?
            ORDER BY COALESCE(s.hsk_rank, 99), length(l.simplified), l.lexeme_id
            LIMIT ?
            """,
            (normalized, limit),
        ),
        "examples_tatoeba": (
            """
            SELECT s.sentence_id
            FROM example_hit h
            JOIN sentence s ON s.sentence_id = h.sentence_id
            WHERE h.query_key = ? AND h.source = ?
            ORDER BY h.score DESC, h.sentence_id
            LIMIT ?
            """,
            (normalized, SOURCE_TATOEBA, limit),
        ),
        "examples_opensubtitles": (
            """
            SELECT s.sentence_id
            FROM example_hit h
            JOIN sentence s ON s.sentence_id = h.sentence_id
            WHERE h.query_key = ? AND h.source = ?
            ORDER BY h.score DESC, h.sentence_id
            LIMIT ?
            """,
            (normalized, SOURCE_OPENSUBTITLES, limit),
        ),
    }
    for name, (sql, params) in statements.items():
        rows = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
        plans[name] = [row[3] for row in rows]
    if len(normalized) >= 3 and table_exists(conn, "sentence_fts"):
        match_query = make_trigram_match_query(normalized)
        rows = conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT s.sentence_id
            FROM sentence_fts f
            JOIN sentence s ON s.sentence_id = f.rowid
            WHERE sentence_fts MATCH ?
              AND instr(s.zh_norm, ?) > 0
            ORDER BY s.quality_score DESC, s.length_chars, s.sentence_id
            LIMIT ?
            """,
            (match_query, normalized, limit),
        ).fetchall()
        plans["fts"] = [row[3] for row in rows]
    return plans


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Query the Chinese dictionary database.")
    parser.add_argument("query")
    parser.add_argument("--db", type=Path, default=Path("data/processed/dictionary_compact.db"))
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--cache-mb", type=int, default=96)
    parser.add_argument("--mutable", action="store_true", help="Open read-only without immutable=1.")
    parser.add_argument("--explain", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with open_readonly_db(args.db, immutable=not args.mutable, cache_mb=args.cache_mb) as conn:
        result = lookup(conn, args.query, limit=args.limit)
        if args.explain:
            result["query_plan"] = explain_plans(conn, args.query, args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
