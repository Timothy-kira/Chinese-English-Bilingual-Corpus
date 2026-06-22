from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .common import normalize_text, open_readonly_db


LEVEL_RE = re.compile(r"\d+")
MAX_LIMIT = 500


def canonical_hsk_bucket(level: str | int | None) -> int | None:
    if level is None:
        return None
    text = normalize_text(str(level)).lower()
    if not text or text in {"all", "*"}:
        return None
    numbers = [int(part) for part in LEVEL_RE.findall(text)]
    if not numbers:
        return None
    first = numbers[0]
    if first >= 7:
        return 7
    if 1 <= first <= 6:
        return first
    return None


def hsk_bucket_label(bucket: int | None) -> str:
    if bucket is None:
        return "all"
    return "7-9" if bucket >= 7 else str(bucket)


def clamp_limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_LIMIT))


def create_hsk_serving_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS hsk_wordbook (
          hsk_bucket INTEGER NOT NULL,
          level_label TEXT NOT NULL,
          lexeme_id INTEGER NOT NULL,
          simplified TEXT NOT NULL,
          traditional TEXT NOT NULL,
          pinyin_numbered TEXT NOT NULL,
          word_len INTEGER NOT NULL,
          PRIMARY KEY (hsk_bucket, lexeme_id)
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS idx_hsk_wordbook_order
        ON hsk_wordbook(hsk_bucket, word_len, simplified, lexeme_id);

        CREATE TABLE IF NOT EXISTS hsk_charbook (
          hsk_bucket INTEGER NOT NULL,
          level_label TEXT NOT NULL,
          hanzi TEXT NOT NULL,
          traditional TEXT,
          writing_level TEXT,
          examples TEXT,
          PRIMARY KEY (hsk_bucket, hanzi)
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS idx_hsk_charbook_order
        ON hsk_charbook(hsk_bucket, hanzi);
        """
    )


def rebuild_hsk_serving_tables(conn: sqlite3.Connection) -> dict[str, int]:
    create_hsk_serving_schema(conn)
    conn.execute("DELETE FROM hsk_wordbook")
    conn.execute("DELETE FROM hsk_charbook")

    word_rows: list[tuple[Any, ...]] = []
    for row in conn.execute(
        """
        SELECT lexeme_id, simplified, traditional, pinyin_numbered, hsk_word_level
        FROM lexeme
        WHERE hsk_word_level IS NOT NULL
          AND hsk_word_level != ''
        """
    ):
        lexeme_id, simplified, traditional, pinyin, level_label = row
        bucket = canonical_hsk_bucket(level_label)
        if bucket is None:
            continue
        word_rows.append(
            (
                bucket,
                str(level_label),
                int(lexeme_id),
                simplified,
                traditional,
                pinyin,
                len(simplified),
            )
        )

    conn.executemany(
        """
        INSERT OR REPLACE INTO hsk_wordbook(
          hsk_bucket, level_label, lexeme_id, simplified, traditional,
          pinyin_numbered, word_len
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        word_rows,
    )

    char_rows: list[tuple[Any, ...]] = []
    for row in conn.execute(
        """
        SELECT hanzi, hsk_level, writing_level, traditional, examples
        FROM hanzi_char
        WHERE hsk_level IS NOT NULL
          AND hsk_level != ''
        """
    ):
        hanzi, level_label, writing_level, traditional, examples = row
        bucket = canonical_hsk_bucket(level_label)
        if bucket is None:
            continue
        char_rows.append((bucket, str(level_label), hanzi, traditional, writing_level, examples))

    conn.executemany(
        """
        INSERT OR REPLACE INTO hsk_charbook(
          hsk_bucket, level_label, hanzi, traditional, writing_level, examples
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        char_rows,
    )
    conn.commit()
    return {"words": len(word_rows), "chars": len(char_rows)}


def hsk_levels(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    word_counts = {
        int(row["hsk_bucket"]): int(row["count"])
        for row in conn.execute(
            "SELECT hsk_bucket, count(*) AS count FROM hsk_wordbook GROUP BY hsk_bucket"
        )
    }
    char_counts = {
        int(row["hsk_bucket"]): int(row["count"])
        for row in conn.execute(
            "SELECT hsk_bucket, count(*) AS count FROM hsk_charbook GROUP BY hsk_bucket"
        )
    }
    buckets = sorted(set(word_counts) | set(char_counts))
    return [
        {
            "level": hsk_bucket_label(bucket),
            "bucket": bucket,
            "word_count": word_counts.get(bucket, 0),
            "char_count": char_counts.get(bucket, 0),
        }
        for bucket in buckets
    ]


def list_hsk_words(
    conn: sqlite3.Connection,
    level: str | int | None,
    limit: int = 100,
    offset: int = 0,
    min_len: int = 1,
) -> dict[str, Any]:
    bucket = canonical_hsk_bucket(level)
    limit = clamp_limit(limit)
    offset = max(0, int(offset))
    min_len = max(1, int(min_len))
    params: list[Any] = []
    filters: list[str] = ["wb.word_len >= ?"]
    params.append(min_len)
    if bucket is not None:
        filters.insert(0, "wb.hsk_bucket = ?")
        params.insert(0, bucket)
    where = "WHERE " + " AND ".join(filters)

    count_sql = f"SELECT count(*) FROM hsk_wordbook wb {where}"
    total = int(conn.execute(count_sql, params).fetchone()[0])

    rows = conn.execute(
        f"""
        SELECT
          wb.hsk_bucket,
          wb.level_label,
          wb.lexeme_id,
          wb.simplified,
          wb.traditional,
          wb.pinyin_numbered,
          l.definitions_json
        FROM hsk_wordbook wb
        JOIN lexeme l ON l.lexeme_id = wb.lexeme_id
        {where}
        ORDER BY wb.hsk_bucket, wb.word_len, wb.simplified, wb.lexeme_id
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()

    return {
        "kind": "words",
        "level": hsk_bucket_label(bucket),
        "bucket": bucket,
        "limit": limit,
        "offset": offset,
        "min_len": min_len,
        "total": total,
        "items": [
            {
                "lexeme_id": row["lexeme_id"],
                "level": hsk_bucket_label(int(row["hsk_bucket"])),
                "level_label": row["level_label"],
                "simplified": row["simplified"],
                "traditional": row["traditional"],
                "pinyin": row["pinyin_numbered"],
                "definitions": json.loads(row["definitions_json"]),
            }
            for row in rows
        ],
    }


def list_hsk_chars(
    conn: sqlite3.Connection,
    level: str | int | None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    bucket = canonical_hsk_bucket(level)
    limit = clamp_limit(limit)
    offset = max(0, int(offset))
    params: list[Any] = []
    where = ""
    if bucket is not None:
        where = "WHERE hsk_bucket = ?"
        params.append(bucket)

    total = int(conn.execute(f"SELECT count(*) FROM hsk_charbook {where}", params).fetchone()[0])
    rows = conn.execute(
        f"""
        SELECT hsk_bucket, level_label, hanzi, traditional, writing_level, examples
        FROM hsk_charbook
        {where}
        ORDER BY hsk_bucket, hanzi
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()

    return {
        "kind": "chars",
        "level": hsk_bucket_label(bucket),
        "bucket": bucket,
        "limit": limit,
        "offset": offset,
        "total": total,
        "items": [
            {
                "level": hsk_bucket_label(int(row["hsk_bucket"])),
                "level_label": row["level_label"],
                "hanzi": row["hanzi"],
                "traditional": row["traditional"],
                "writing_level": row["writing_level"],
                "examples": row["examples"],
            }
            for row in rows
        ],
    }


def explain_hsk_plans(conn: sqlite3.Connection, level: str | int | None, limit: int) -> dict[str, list[str]]:
    bucket = canonical_hsk_bucket(level)
    params: list[Any] = []
    where_word = ""
    where_char = ""
    if bucket is not None:
        where_word = "WHERE wb.hsk_bucket = ?"
        where_char = "WHERE hsk_bucket = ?"
        params.append(bucket)
    word_rows = conn.execute(
        "EXPLAIN QUERY PLAN "
        + f"""
        SELECT wb.lexeme_id
        FROM hsk_wordbook wb
        JOIN lexeme l ON l.lexeme_id = wb.lexeme_id
        {where_word}
        ORDER BY wb.hsk_bucket, wb.word_len, wb.simplified, wb.lexeme_id
        LIMIT ?
        """,
        [*params, clamp_limit(limit)],
    ).fetchall()
    char_rows = conn.execute(
        "EXPLAIN QUERY PLAN "
        + f"""
        SELECT hanzi
        FROM hsk_charbook
        {where_char}
        ORDER BY hsk_bucket, hanzi
        LIMIT ?
        """,
        [*params, clamp_limit(limit)],
    ).fetchall()
    return {
        "words": [row[3] for row in word_rows],
        "chars": [row[3] for row in char_rows],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HSK wordbook utilities.")
    parser.add_argument("--db", type=Path, default=Path("data/processed/dictionary_compact.db"))
    parser.add_argument("--rebuild", action="store_true", help="Rebuild HSK serving tables in the DB.")
    parser.add_argument("--kind", choices=["levels", "words", "chars"], default="levels")
    parser.add_argument("--level", default=None)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--min-len", type=int, default=1)
    parser.add_argument("--explain", action="store_true")
    parser.add_argument("--cache-mb", type=int, default=96)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.rebuild:
        conn = sqlite3.connect(args.db)
        conn.row_factory = sqlite3.Row
        result = rebuild_hsk_serving_tables(conn)
        conn.execute("PRAGMA optimize")
        conn.close()
        print(json.dumps({"rebuilt": result}, ensure_ascii=False, indent=2))
        return

    with open_readonly_db(args.db, immutable=True, cache_mb=args.cache_mb) as conn:
        if args.kind == "levels":
            result: dict[str, Any] = {"levels": hsk_levels(conn)}
        elif args.kind == "words":
            result = list_hsk_words(
                conn,
                args.level,
                limit=args.limit,
                offset=args.offset,
                min_len=args.min_len,
            )
        else:
            result = list_hsk_chars(conn, args.level, limit=args.limit, offset=args.offset)
        if args.explain:
            result["query_plan"] = explain_hsk_plans(conn, args.level, args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
