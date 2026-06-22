from __future__ import annotations

import argparse
import json
import sqlite3
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

from .common import SOURCE_TATOEBA, normalize_text


def strip_terminal_punctuation(value: str | None) -> str:
    text = normalize_text(value or "")
    while text:
        last = text[-1]
        if last.isspace() or unicodedata.category(last).startswith("P"):
            text = text[:-1].rstrip()
            continue
        break
    return text


def exact_key(zh: str, en: str | None) -> tuple[str, str]:
    return normalize_text(zh), normalize_text(en or "")


def punctuation_key(zh: str, en: str | None) -> tuple[str, str]:
    return strip_terminal_punctuation(zh), strip_terminal_punctuation(en or "")


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def keep_sort_key(row: sqlite3.Row) -> tuple[int, int, int, int]:
    source_rank = 0 if int(row["source"]) == SOURCE_TATOEBA else 1
    return (
        source_rank,
        -int(row["quality_score"]),
        int(row["length_chars"]),
        int(row["sentence_id"]),
    )


def collect_sentence_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    old_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return list(
            conn.execute(
                """
                SELECT sentence_id, source, zh, en, quality_score, length_chars
                FROM sentence
                """
            )
        )
    finally:
        conn.row_factory = old_factory


def duplicate_group_stats(groups: dict[tuple[str, str], list[sqlite3.Row]]) -> dict[str, int]:
    duplicate_groups = [rows for rows in groups.values() if len(rows) > 1]
    return {
        "groups": len(duplicate_groups),
        "duplicate_rows": sum(len(rows) - 1 for rows in duplicate_groups),
        "max_group": max((len(rows) for rows in duplicate_groups), default=0),
    }


def analyze_sentence_duplicates(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = collect_sentence_rows(conn)
    exact_groups: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    punctuation_groups: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        exact_groups[exact_key(row["zh"], row["en"])].append(row)
        punctuation_groups[punctuation_key(row["zh"], row["en"])].append(row)
    return {
        "sentence_rows": len(rows),
        "exact": duplicate_group_stats(exact_groups),
        "punctuation_or_exact": duplicate_group_stats(punctuation_groups),
    }


def build_dedupe_mapping(conn: sqlite3.Connection) -> dict[int, int]:
    rows = collect_sentence_rows(conn)
    groups: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        groups[punctuation_key(row["zh"], row["en"])].append(row)

    mapping: dict[int, int] = {}
    for group_rows in groups.values():
        if len(group_rows) <= 1:
            continue
        keeper = min(group_rows, key=keep_sort_key)
        keeper_id = int(keeper["sentence_id"])
        for row in group_rows:
            sentence_id = int(row["sentence_id"])
            if sentence_id != keeper_id:
                mapping[sentence_id] = keeper_id
    return mapping


def dedupe_sentences(
    conn: sqlite3.Connection,
    rebuild_fts: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    before = analyze_sentence_duplicates(conn)
    mapping = build_dedupe_mapping(conn)
    result: dict[str, Any] = {
        "before": before,
        "planned_sentence_deletes": len(mapping),
        "deleted_example_hits": 0,
        "deleted_sentences": 0,
        "rebuild_fts": False,
        "after": None,
    }
    if dry_run or not mapping:
        return result

    conn.execute("DROP TABLE IF EXISTS temp.sentence_dedupe_loser")
    conn.execute(
        """
        CREATE TEMP TABLE sentence_dedupe_loser(
          sentence_id INTEGER PRIMARY KEY,
          keeper_id INTEGER NOT NULL
        )
        """
    )
    conn.executemany(
        "INSERT INTO sentence_dedupe_loser(sentence_id, keeper_id) VALUES (?, ?)",
        sorted(mapping.items()),
    )

    before_changes = conn.total_changes
    conn.execute(
        """
        DELETE FROM example_hit
        WHERE sentence_id IN (SELECT sentence_id FROM sentence_dedupe_loser)
        """
    )
    result["deleted_example_hits"] = conn.total_changes - before_changes

    before_changes = conn.total_changes
    conn.execute(
        """
        DELETE FROM sentence
        WHERE sentence_id IN (SELECT sentence_id FROM sentence_dedupe_loser)
        """
    )
    result["deleted_sentences"] = conn.total_changes - before_changes

    if rebuild_fts and table_exists(conn, "sentence_fts"):
        conn.execute("INSERT INTO sentence_fts(sentence_fts) VALUES('rebuild')")
        conn.execute("INSERT INTO sentence_fts(sentence_fts) VALUES('optimize')")
        result["rebuild_fts"] = True

    conn.execute("DROP TABLE temp.sentence_dedupe_loser")
    conn.execute("PRAGMA optimize")
    result["after"] = analyze_sentence_duplicates(conn)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze or remove duplicate dictionary example sentences.")
    parser.add_argument("--db", type=Path, default=Path("data/processed/dictionary_compact.db"))
    parser.add_argument("--apply", action="store_true", help="Delete duplicate sentence rows and their example hits.")
    parser.add_argument("--no-rebuild-fts", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    conn = sqlite3.connect(args.db)
    try:
        result = dedupe_sentences(conn, rebuild_fts=not args.no_rebuild_fts, dry_run=not args.apply)
        if args.apply:
            conn.commit()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
