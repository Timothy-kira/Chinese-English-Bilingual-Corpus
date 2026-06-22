from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from .common import has_cjk, normalize_query, normalize_text, open_readonly_db
from .hsk import canonical_hsk_bucket


_TRAD_MAP: dict[str, str] = {}


def init_conversion_map(conn: sqlite3.Connection) -> None:
    global _TRAD_MAP
    if _TRAD_MAP:
        return
    mapping = {}
    try:
        for row in conn.execute("SELECT traditional, hanzi FROM hanzi_char WHERE traditional IS NOT NULL AND traditional != ''"):
            trad, simp = row[0], row[1]
            if trad and simp and len(trad) == 1 and len(simp) == 1 and trad != simp:
                mapping[trad] = simp
    except Exception:
        pass
    try:
        for row in conn.execute("SELECT traditional, simplified FROM lexeme WHERE length(traditional) = length(simplified)"):
            trad, simp = row[0], row[1]
            if trad and simp:
                for t, s in zip(trad, simp):
                    if t != s:
                        mapping[t] = s
    except Exception:
        pass
    _TRAD_MAP = mapping


def to_simplified(text: str) -> str:
    if not text:
        return ""
    if not _TRAD_MAP:
        return text
    return "".join(_TRAD_MAP.get(c, c) for c in text)


_TONE_MAP = {
    'a': ['a', 'ā', 'á', 'ǎ', 'à', 'a'],
    'o': ['o', 'ō', 'ó', 'ǒ', 'ò', 'o'],
    'e': ['e', 'ē', 'é', 'ě', 'è', 'e'],
    'i': ['i', 'ī', 'í', 'ǐ', 'ì', 'i'],
    'u': ['u', 'ū', 'ú', 'ǔ', 'ù', 'u'],
    'ü': ['ü', 'ǖ', 'ǘ', 'ǚ', 'ǜ', 'ü'],
    'A': ['A', 'Ā', 'Á', 'Ǎ', 'À', 'A'],
    'O': ['O', 'Ō', 'Ó', 'Ǒ', 'Ò', 'O'],
    'E': ['E', 'Ē', 'É', 'Ě', 'È', 'E'],
    'I': ['I', 'Ī', 'Í', 'Ǐ', 'Ì', 'I'],
    'U': ['U', 'Ū', 'Ú', 'Ǔ', 'Ù', 'U'],
    'Ü': ['Ü', 'Ǖ', 'Ǘ', 'Ǚ', 'ǜ', 'Ü'],
}


def pinyin_tone_marked_syllable(syllable: str) -> str:
    syllable = syllable.replace("u:", "ü").replace("v", "ü").replace("U:", "Ü").replace("V", "Ü")
    if not syllable:
        return ""
    last_char = syllable[-1]
    if last_char.isdigit():
        tone = int(last_char)
        base = syllable[:-1]
    else:
        tone = 5
        base = syllable
    if tone < 1 or tone > 5:
        return base
    vowels = ['a', 'o', 'e', 'i', 'u', 'ü', 'A', 'O', 'E', 'I', 'U', 'Ü']
    if 'a' in base:
        pos = base.find('a')
        return base[:pos] + _TONE_MAP['a'][tone] + base[pos+1:]
    if 'A' in base:
        pos = base.find('A')
        return base[:pos] + _TONE_MAP['A'][tone] + base[pos+1:]
    if 'e' in base:
        pos = base.find('e')
        return base[:pos] + _TONE_MAP['e'][tone] + base[pos+1:]
    if 'E' in base:
        pos = base.find('E')
        return base[:pos] + _TONE_MAP['E'][tone] + base[pos+1:]
    if 'ou' in base:
        pos = base.find('ou')
        return base[:pos] + _TONE_MAP['o'][tone] + 'u' + base[pos+2:]
    if 'OU' in base:
        pos = base.find('OU')
        return base[:pos] + _TONE_MAP['O'][tone] + 'U' + base[pos+2:]
    last_vowel_pos = -1
    last_vowel_char = ''
    for idx, char in enumerate(base):
        if char in vowels:
            last_vowel_pos = idx
            last_vowel_char = char
    if last_vowel_pos != -1:
        return base[:last_vowel_pos] + _TONE_MAP[last_vowel_char][tone] + base[last_vowel_pos+1:]
    return base


def convert_pinyin(pinyin_str: str) -> str:
    if not pinyin_str:
        return ""
    parts = pinyin_str.split(" ")
    converted_parts = [pinyin_tone_marked_syllable(part) for part in parts]
    return " ".join(converted_parts)


MAX_LIMIT = 50
EN_WORD_RE = re.compile(r"[a-z]+(?:'[a-z]+)?")
EN_STOP_TERMS = {
    "a",
    "an",
    "the",
    "to",
    "of",
    "and",
    "or",
    "in",
    "on",
    "at",
    "for",
    "from",
    "with",
    "without",
    "by",
    "as",
    "into",
    "onto",
    "only",
    "etc",
    "sb",
    "sth",
    "one",
    "some",
    "used",
    "variant",
    "form",
    "measure",
    "word",
    "classifier",
}
BE_VARIANTS = {
    "be",
    "am",
    "is",
    "are",
    "was",
    "were",
    "been",
    "being",
    "i'm",
    "you're",
    "he's",
    "she's",
    "it's",
    "we're",
    "they're",
    "that's",
    "this is",
    "there's",
}


def clamp_limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_LIMIT))


def create_suggestion_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS lexeme_suggestion (
          surface TEXT NOT NULL,
          lexeme_id INTEGER NOT NULL,
          pinyin_numbered TEXT NOT NULL,
          hsk_rank INTEGER,
          word_len INTEGER NOT NULL,
          first_char TEXT NOT NULL,
          PRIMARY KEY (surface, lexeme_id)
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS idx_lexeme_suggestion_prefix
        ON lexeme_suggestion(first_char, word_len, hsk_rank, surface, lexeme_id);

        CREATE INDEX IF NOT EXISTS idx_lexeme_suggestion_surface
        ON lexeme_suggestion(surface, word_len, hsk_rank, lexeme_id);

        CREATE TABLE IF NOT EXISTS lexeme_char_suggestion (
          char TEXT NOT NULL,
          position INTEGER NOT NULL,
          surface TEXT NOT NULL,
          lexeme_id INTEGER NOT NULL,
          pinyin_numbered TEXT NOT NULL,
          hsk_rank INTEGER,
          word_len INTEGER NOT NULL,
          PRIMARY KEY (char, lexeme_id, surface, position)
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS idx_lexeme_char_suggestion_char
        ON lexeme_char_suggestion(char, position, hsk_rank, word_len, surface, lexeme_id);

        CREATE TABLE IF NOT EXISTS lexeme_english_suggestion (
          term TEXT NOT NULL,
          surface TEXT NOT NULL,
          lexeme_id INTEGER NOT NULL,
          pinyin_numbered TEXT NOT NULL,
          hsk_rank INTEGER,
          word_len INTEGER NOT NULL,
          PRIMARY KEY (term, lexeme_id, surface)
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS idx_lexeme_english_suggestion_term
        ON lexeme_english_suggestion(term, hsk_rank, word_len, surface, lexeme_id);
        """
    )


def normalize_english_query(value: str) -> str:
    value = normalize_text(value).lower()
    value = re.sub(r"[^a-z'\s-]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def add_english_term(terms: set[str], raw: str) -> None:
    term = normalize_english_query(raw)
    if not term:
        return
    if term.startswith("to "):
        add_english_term(terms, term[3:])
    if term in {"be", "to be"}:
        terms.update(BE_VARIANTS)
        return

    words = EN_WORD_RE.findall(term)
    content_words = [word for word in words if len(word) >= 2 and word not in EN_STOP_TERMS]
    if not content_words:
        return

    if 1 < len(content_words) <= 4:
        terms.add(" ".join(content_words))

    for word in content_words:
        terms.add(word)
        if len(word) < 3:
            continue
        if word.endswith("y"):
            terms.add(word[:-1] + "ies")
        if word.endswith("e"):
            terms.add(word + "d")
            terms.add(word[:-1] + "ing")
        else:
            terms.add(word + "s")
            terms.add(word + "ed")
            terms.add(word + "ing")


def definition_terms(definitions_json: str) -> set[str]:
    try:
        definitions = json.loads(definitions_json)
    except json.JSONDecodeError:
        definitions = []
    terms: set[str] = set()
    for definition in definitions:
        cleaned = re.sub(r"\([^)]*\)", " ", str(definition))
        cleaned = re.sub(r"\[[^\]]*\]", " ", cleaned)
        for chunk in re.split(r"[;,/]| - | – | — ", cleaned):
            add_english_term(terms, chunk)
    return {term for term in terms if 2 <= len(term) <= 48}


def rebuild_suggestion_table(conn: sqlite3.Connection) -> dict[str, int]:
    create_suggestion_schema(conn)
    conn.execute("DELETE FROM lexeme_suggestion")
    conn.execute("DELETE FROM lexeme_char_suggestion")
    conn.execute("DELETE FROM lexeme_english_suggestion")

    rows = []
    for row in conn.execute(
        """
        SELECT
          s.surface,
          s.lexeme_id,
          l.pinyin_numbered,
          COALESCE(s.hsk_rank, 99) AS hsk_rank
        FROM lexeme_surface s
        JOIN lexeme l ON l.lexeme_id = s.lexeme_id
        WHERE length(s.surface) >= 2
        """
    ):
        surface, lexeme_id, pinyin, hsk_rank = row
        rows.append((surface, lexeme_id, pinyin, hsk_rank, len(surface), surface[0]))

    conn.executemany(
        """
        INSERT OR IGNORE INTO lexeme_suggestion(
          surface, lexeme_id, pinyin_numbered, hsk_rank, word_len, first_char
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )

    char_rows = []
    for surface, lexeme_id, pinyin, hsk_rank, word_len, _first_char in rows:
        seen_chars: set[str] = set()
        for position, char in enumerate(surface):
            if char in seen_chars or not has_cjk(char):
                continue
            seen_chars.add(char)
            char_rows.append((char, position, surface, lexeme_id, pinyin, hsk_rank, word_len))

    conn.executemany(
        """
        INSERT OR IGNORE INTO lexeme_char_suggestion(
          char, position, surface, lexeme_id, pinyin_numbered, hsk_rank, word_len
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        char_rows,
    )

    english_rows = []
    for row in conn.execute(
        """
        SELECT
          l.lexeme_id,
          l.simplified,
          l.pinyin_numbered,
          l.definitions_json,
          l.hsk_word_level
        FROM lexeme l
        WHERE length(l.simplified) >= 1
        """
    ):
        lexeme_id, surface, pinyin, definitions_json, hsk_word_level = row
        hsk_bucket = canonical_hsk_bucket(hsk_word_level)
        hsk_rank = hsk_bucket if hsk_bucket is not None else 99
        for term in definition_terms(definitions_json):
            english_rows.append((term, surface, lexeme_id, pinyin, hsk_rank, len(surface)))

    conn.executemany(
        """
        INSERT OR IGNORE INTO lexeme_english_suggestion(
          term, surface, lexeme_id, pinyin_numbered, hsk_rank, word_len
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        english_rows,
    )
    conn.commit()
    return {"zh": len(rows), "char": len(char_rows), "en": len(english_rows)}


def row_to_suggestion(row: sqlite3.Row, match_kind: str) -> dict[str, Any]:
    hsk_rank = row["hsk_rank"]
    definition = ""
    keys = row.keys()
    try:
        if "definitions_json" in keys:
            defs = json.loads(row["definitions_json"])
            if defs:
                definition = defs[0]
    except Exception:
        pass
    definition_status = row["definition_status"] if "definition_status" in keys else "cedict"
    if not definition and definition_status == "supplement_pending":
        definition = "暂无释义，待 LLM 清洗"
    pinyin_display = row["pinyin_display"] if "pinyin_display" in keys else ""
    return {
        "surface": to_simplified(row["surface"]),
        "lexeme_id": row["lexeme_id"],
        "pinyin": pinyin_display or convert_pinyin(row["pinyin_numbered"]),
        "hsk_level": None if hsk_rank is None or hsk_rank >= 99 else ("7-9" if hsk_rank >= 7 else str(hsk_rank)),
        "word_len": row["word_len"],
        "match_kind": match_kind,
        "definition": definition,
        "definition_status": definition_status,
    }


def row_to_english_suggestion(row: sqlite3.Row, match_kind: str) -> dict[str, Any]:
    item = row_to_suggestion(row, match_kind)
    item["matched_english"] = row["term"]
    return item


def suggest_words(
    conn: sqlite3.Connection,
    raw_query: str,
    limit: int = 12,
    hsk_level: str | int | None = None,
) -> dict[str, Any]:
    global _TRAD_MAP
    if not _TRAD_MAP:
        init_conversion_map(conn)
    started = time.perf_counter()
    query = normalize_query(raw_query)
    limit = clamp_limit(limit)
    if not query:
        return {"query": raw_query, "normalized_query": query, "limit": limit, "items": [], "elapsed_ms": 0}
    if not has_cjk(query):
        return suggest_words_by_english(conn, raw_query, limit=limit, hsk_level=hsk_level)

    hsk_bucket = canonical_hsk_bucket(hsk_level)
    if len(query) == 1:
        return suggest_words_by_char(conn, raw_query, query, limit=limit, hsk_bucket=hsk_bucket, hsk_level=hsk_level)

    hsk_filter = ""
    prefix_params: list[Any] = [query[0], query, query + "\U0010ffff"]
    if hsk_bucket is not None:
        hsk_filter = "AND s.hsk_rank = ?"
        prefix_params.append(hsk_bucket)
    prefix_params.extend([query, limit])

    prefix_rows = conn.execute(
        f"""
        SELECT
          s.surface,
          s.lexeme_id,
          s.pinyin_numbered,
          s.hsk_rank,
          s.word_len,
          l.pinyin_display,
          l.definitions_json,
          l.definition_status
        FROM lexeme_suggestion s
        JOIN lexeme l ON l.lexeme_id = s.lexeme_id
        WHERE s.first_char = ?
          AND s.surface >= ?
          AND s.surface < ?
          {hsk_filter}
        ORDER BY
          CASE WHEN s.surface = ? THEN 0 ELSE 1 END,
          COALESCE(s.hsk_rank, 99),
          s.word_len,
          s.surface,
          s.lexeme_id
        LIMIT ?
        """,
        prefix_params,
    ).fetchall()

    seen = {row["lexeme_id"] for row in prefix_rows}
    items = [row_to_suggestion(row, "prefix") for row in prefix_rows]

    if len(items) < limit:
        contains_params: list[Any] = [f"%{query}%"]
        contains_filter = ""
        if hsk_bucket is not None:
            contains_filter = "AND s.hsk_rank = ?"
            contains_params.append(hsk_bucket)
        contains_rows = conn.execute(
            f"""
            SELECT
              s.surface,
              s.lexeme_id,
              s.pinyin_numbered,
              s.hsk_rank,
              s.word_len,
              l.pinyin_display,
              l.definitions_json,
              l.definition_status
            FROM lexeme_suggestion s
            JOIN lexeme l ON l.lexeme_id = s.lexeme_id
            WHERE s.surface LIKE ?
              {contains_filter}
            ORDER BY
              instr(s.surface, ?),
              COALESCE(s.hsk_rank, 99),
              s.word_len,
              s.surface,
              s.lexeme_id
            LIMIT ?
            """,
            [*contains_params, query, limit * 4],
        ).fetchall()
        for row in contains_rows:
            if row["lexeme_id"] in seen:
                continue
            seen.add(row["lexeme_id"])
            items.append(row_to_suggestion(row, "contains"))
            if len(items) >= limit:
                break

    elapsed_ms = (time.perf_counter() - started) * 1000
    return {
        "query": raw_query,
        "normalized_query": query,
        "mode": "zh",
        "limit": limit,
        "hsk_level": hsk_level,
        "items": items,
        "elapsed_ms": round(elapsed_ms, 3),
    }


def suggest_words_by_char(
    conn: sqlite3.Connection,
    raw_query: str,
    query: str,
    limit: int,
    hsk_bucket: int | None,
    hsk_level: str | int | None,
) -> dict[str, Any]:
    started = time.perf_counter()
    hsk_filter = ""
    params: list[Any] = [query]
    if hsk_bucket is not None:
        hsk_filter = "AND cs.hsk_rank = ?"
        params.append(hsk_bucket)
    params.append(limit * 4)

    rows = conn.execute(
        f"""
        SELECT
          cs.surface,
          cs.lexeme_id,
          cs.pinyin_numbered,
          cs.hsk_rank,
          cs.word_len,
          l.pinyin_display,
          l.definitions_json,
          l.definition_status
        FROM lexeme_char_suggestion cs
        JOIN lexeme l ON l.lexeme_id = cs.lexeme_id
        WHERE cs.char = ?
          {hsk_filter}
        ORDER BY
          cs.position,
          cs.hsk_rank,
          cs.word_len,
          cs.surface,
          cs.lexeme_id
        LIMIT ?
        """,
        params,
    ).fetchall()

    seen: set[int] = set()
    items = []
    for row in rows:
        if row["lexeme_id"] in seen:
            continue
        seen.add(row["lexeme_id"])
        items.append(row_to_suggestion(row, "char"))
        if len(items) >= limit:
            break

    elapsed_ms = (time.perf_counter() - started) * 1000
    return {
        "query": raw_query,
        "normalized_query": query,
        "mode": "zh",
        "limit": limit,
        "hsk_level": hsk_level,
        "items": items,
        "elapsed_ms": round(elapsed_ms, 3),
    }


def suggest_words_by_english(
    conn: sqlite3.Connection,
    raw_query: str,
    limit: int = 12,
    hsk_level: str | int | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    query = normalize_english_query(raw_query)
    limit = clamp_limit(limit)
    if not query:
        return {"query": raw_query, "normalized_query": query, "mode": "en", "limit": limit, "items": [], "elapsed_ms": 0}

    hsk_bucket = canonical_hsk_bucket(hsk_level)
    hsk_filter = ""
    params: list[Any] = [query, query + "\U0010ffff"]
    if hsk_bucket is not None:
        hsk_filter = "AND es.hsk_rank = ?"
        params.append(hsk_bucket)
    params.extend([query, limit * 4])

    rows = conn.execute(
        f"""
        SELECT
          es.term,
          es.surface,
          es.lexeme_id,
          es.pinyin_numbered,
          es.hsk_rank,
          es.word_len,
          l.pinyin_display,
          l.definitions_json,
          l.definition_status
        FROM lexeme_english_suggestion es
        JOIN lexeme l ON l.lexeme_id = es.lexeme_id
        WHERE es.term >= ?
          AND es.term < ?
          {hsk_filter}
        ORDER BY
          CASE WHEN es.term = ? THEN 0 ELSE 1 END,
          CASE WHEN es.word_len = 1 THEN 1 ELSE 0 END,
          COALESCE(es.hsk_rank, 99),
          es.word_len,
          length(es.term),
          es.term,
          es.surface,
          es.lexeme_id
        LIMIT ?
        """,
        params,
    ).fetchall()

    seen: set[int] = set()
    items = []
    for row in rows:
        if row["lexeme_id"] in seen:
            continue
        seen.add(row["lexeme_id"])
        items.append(row_to_english_suggestion(row, "english_prefix"))
        if len(items) >= limit:
            break

    elapsed_ms = (time.perf_counter() - started) * 1000
    return {
        "query": raw_query,
        "normalized_query": query,
        "mode": "en",
        "limit": limit,
        "hsk_level": hsk_level,
        "items": items,
        "elapsed_ms": round(elapsed_ms, 3),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dictionary word suggestions.")
    parser.add_argument("query", nargs="?")
    parser.add_argument("--db", type=Path, default=Path("data/processed/dictionary_compact.db"))
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--hsk-level", default=None)
    parser.add_argument("--cache-mb", type=int, default=96)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.rebuild:
        conn = sqlite3.connect(args.db)
        conn.row_factory = sqlite3.Row
        counts = rebuild_suggestion_table(conn)
        conn.execute("PRAGMA optimize")
        conn.close()
        print(json.dumps({"rebuilt": {"suggestions": counts}}, ensure_ascii=False, indent=2))
        return
    if not args.query:
        raise SystemExit("query required unless --rebuild is used")
    with open_readonly_db(args.db, immutable=True, cache_mb=args.cache_mb) as conn:
        print(
            json.dumps(
                suggest_words(conn, args.query, limit=args.limit, hsk_level=args.hsk_level),
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
