from __future__ import annotations

import argparse
import bz2
import csv
import gzip
import heapq
import io
import json
import re
import sqlite3
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .common import (
    SOURCE_OPENSUBTITLES,
    SOURCE_TATOEBA,
    batched,
    clean_sentence,
    configure_build_connection,
    create_indexes,
    create_schema,
    create_selected_fts,
    normalize_query,
    sentence_quality,
    should_keep_sentence,
    write_metadata,
)
from .dedupe import dedupe_sentences
from .hanzi_words import SupplementEntry, iter_supplement_entries
from .hsk import rebuild_hsk_serving_tables
from .matcher import SurfaceMatcher
from .suggest import rebuild_suggestion_table


CEDICT_RE = re.compile(r"^(\S+)\s+(\S+)\s+\[(.+?)\]\s+/(.+)/$")
SUPPLEMENT_SURFACE_KIND = 3


@dataclass(frozen=True)
class HskWord:
    level: str
    rank: int


@dataclass(order=True)
class Candidate:
    score: int
    seq: int
    zh: str
    en: str
    hit_kind: int


def level_rank(level: str | None) -> int | None:
    if not level:
        return None
    digits = "".join(ch for ch in level if ch.isdigit())
    if not digits:
        return None
    return int(digits)


def best_hsk(existing: HskWord | None, level: str | None) -> HskWord | None:
    rank = level_rank(level)
    if rank is None:
        return existing
    candidate = HskWord(level=str(level), rank=rank)
    if existing is None or candidate.rank < existing.rank:
        return candidate
    return existing


def assign_best_hsk(target: dict[str, HskWord], surface: str, level: str | None) -> None:
    if not surface:
        return
    best = best_hsk(target.get(surface), level)
    if best is not None:
        target[surface] = best


def load_hsk(raw_dir: Path) -> tuple[dict[str, HskWord], dict[str, dict[str, str]]]:
    hsk_dir = raw_dir / "hsk"
    word_levels: dict[str, HskWord] = {}
    char_rows: dict[str, dict[str, str]] = {}

    expanded = hsk_dir / "hsk30-expanded.csv"
    with expanded.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            level = row.get("Level")
            for field in ("Simplified", "Traditional", "OCR"):
                surface = normalize_query(row.get(field, ""))
                assign_best_hsk(word_levels, surface, level)

    chars = hsk_dir / "hsk30-chars.csv"
    with chars.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            hanzi = normalize_query(row.get("Hanzi", ""))
            if not hanzi:
                continue
            char_rows[hanzi] = {
                "hanzi": hanzi,
                "hsk_level": row.get("Level", ""),
                "writing_level": row.get("WritingLevel", ""),
                "traditional": normalize_query(row.get("Traditional", "")),
                "examples": row.get("Examples", ""),
            }
            trad = normalize_query(row.get("Traditional", ""))
            assign_best_hsk(word_levels, trad, row.get("Level"))
            assign_best_hsk(word_levels, hanzi, row.get("Level"))

    complete = hsk_dir / "complete-hsk-vocabulary.json"
    if complete.exists():
        with complete.open("r", encoding="utf-8") as f:
            data = json.load(f)
        for item in data:
            levels = item.get("level") or []
            level = levels[0] if levels else None
            simplified = normalize_query(item.get("simplified", ""))
            assign_best_hsk(word_levels, simplified, level)
            for form in item.get("forms") or []:
                trad = normalize_query(form.get("traditional", ""))
                assign_best_hsk(word_levels, trad, level)

    return word_levels, char_rows


def parse_cedict(raw_dir: Path, word_levels: dict[str, HskWord]) -> list[tuple]:
    cedict_path = raw_dir / "mdbg_cc_cedict" / "cedict_1_0_ts_utf-8_mdbg.txt.gz"
    rows: list[tuple] = []

    with gzip.open(cedict_path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = CEDICT_RE.match(line)
            if not match:
                continue
            traditional, simplified, pinyin, definitions = match.groups()
            simplified = normalize_query(simplified)
            traditional = normalize_query(traditional)
            defs = [part for part in definitions.rstrip("/").split("/") if part]
            word_hsk = word_levels.get(simplified) or word_levels.get(traditional)
            char_hsk = word_hsk.level if len(simplified) == 1 and word_hsk else None
            rows.append(
                (
                    simplified,
                    traditional,
                    pinyin,
                    None,
                    json.dumps(defs, ensure_ascii=False),
                    word_hsk.level if word_hsk else None,
                    char_hsk,
                    line,
                )
            )

    return rows


def insert_lexemes(
    conn: sqlite3.Connection,
    lexeme_rows: list[tuple],
    word_levels: dict[str, HskWord],
    char_rows: dict[str, dict[str, str]],
) -> set[str]:
    conn.executemany(
        """
        INSERT INTO lexeme(
          simplified, traditional, pinyin_numbered, pinyin_display,
          definitions_json, hsk_word_level, hsk_char_level, cedict_line
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        lexeme_rows,
    )

    surface_rows: list[tuple] = []
    query_keys: set[str] = set()
    cursor = conn.execute("SELECT lexeme_id, simplified, traditional FROM lexeme")
    for lexeme_id, simplified, traditional in cursor:
        surfaces = [(simplified, 1)]
        if traditional and traditional != simplified:
            surfaces.append((traditional, 2))
        for surface, kind in surfaces:
            if not surface:
                continue
            hsk = word_levels.get(surface)
            surface_rows.append((surface, lexeme_id, kind, len(surface), hsk.rank if hsk else None))
            query_keys.add(surface)

    conn.executemany(
        """
        INSERT OR IGNORE INTO lexeme_surface(surface, lexeme_id, surface_kind, word_len, hsk_rank)
        VALUES (?, ?, ?, ?, ?)
        """,
        surface_rows,
    )

    conn.executemany(
        """
        INSERT OR REPLACE INTO hanzi_char(hanzi, hsk_level, writing_level, traditional, examples)
        VALUES (:hanzi, :hsk_level, :writing_level, :traditional, :examples)
        """,
        char_rows.values(),
    )

    query_keys.update(char_rows.keys())
    for row in char_rows.values():
        trad = row.get("traditional")
        if trad:
            query_keys.add(trad)

    return {key for key in query_keys if key}


def best_entry_hsk(entry: SupplementEntry, word_levels: dict[str, HskWord]) -> HskWord | None:
    best: HskWord | None = None
    for surface in entry.surfaces:
        current = word_levels.get(surface)
        best = best_hsk(best, current.level if current else None)
    return best


def load_surface_index(conn: sqlite3.Connection) -> tuple[dict[str, int], set[tuple[str, int]]]:
    first_lexeme: dict[str, int] = {}
    surface_pairs: set[tuple[str, int]] = set()
    for surface, lexeme_id in conn.execute(
        """
        SELECT surface, lexeme_id
        FROM lexeme_surface
        ORDER BY surface, COALESCE(hsk_rank, 99), lexeme_id
        """
    ):
        surface_pairs.add((surface, int(lexeme_id)))
        first_lexeme.setdefault(surface, int(lexeme_id))
    return first_lexeme, surface_pairs


def insert_supplement_sources(
    conn: sqlite3.Connection,
    source_rows: list[tuple],
) -> int:
    before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO lexeme_source(
          lexeme_id, source_name, source_file, source_row,
          raw_surface, normalized_surface, status, pinyin_display
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        source_rows,
    )
    return conn.total_changes - before


def insert_supplement_lexemes(
    conn: sqlite3.Connection,
    raw_dir: Path,
    word_levels: dict[str, HskWord],
) -> tuple[set[str], dict[str, int]]:
    entries = iter_supplement_entries(raw_dir)
    if not entries:
        return set(), {
            "entries": 0,
            "new_lexemes": 0,
            "existing_matches": 0,
            "surface_rows": 0,
            "source_rows": 0,
            "unique_surfaces": 0,
        }

    surface_to_lexeme, surface_pairs = load_surface_index(conn)
    query_keys: set[str] = set()
    source_rows: list[tuple] = []
    surface_rows: list[tuple] = []
    existing_matches = 0
    new_lexemes = 0
    unique_surfaces: set[str] = set()

    for entry in entries:
        surfaces = entry.surfaces
        if not surfaces:
            continue
        unique_surfaces.update(surfaces)
        query_keys.update(surfaces)
        matched_id = next((surface_to_lexeme[surface] for surface in surfaces if surface in surface_to_lexeme), None)
        created_new_for_entry = False

        if matched_id is None:
            hsk = best_entry_hsk(entry, word_levels)
            char_hsk = hsk.level if hsk and len(entry.simplified) == 1 else None
            cur = conn.execute(
                """
                INSERT INTO lexeme(
                  simplified, traditional, pinyin_numbered, pinyin_display,
                  definitions_json, hsk_word_level, hsk_char_level, cedict_line, definition_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.simplified,
                    entry.traditional,
                    "",
                    entry.pinyin_display or None,
                    "[]",
                    hsk.level if hsk else None,
                    char_hsk,
                    "",
                    "supplement_pending",
                ),
            )
            matched_id = int(cur.lastrowid)
            new_lexemes += 1
            created_new_for_entry = True
        else:
            existing_matches += 1
            if entry.pinyin_display:
                conn.execute(
                    """
                    UPDATE lexeme
                    SET pinyin_display = COALESCE(NULLIF(pinyin_display, ''), ?)
                    WHERE lexeme_id = ?
                      AND definition_status = 'supplement_pending'
                    """,
                    (entry.pinyin_display, matched_id),
                )

        for surface in surfaces:
            hsk = word_levels.get(surface)
            if (surface, matched_id) not in surface_pairs:
                kind = 1 if surface == entry.simplified else 2 if surface == entry.traditional else SUPPLEMENT_SURFACE_KIND
                surface_rows.append((surface, matched_id, kind, len(surface), hsk.rank if hsk else None))
                surface_pairs.add((surface, matched_id))
            surface_to_lexeme.setdefault(surface, matched_id)
            source_rows.append(
                (
                    matched_id,
                    entry.source_name,
                    entry.source_file,
                    entry.source_row,
                    entry.raw_surface,
                    surface,
                    "supplement_pending" if created_new_for_entry else "supplement_source",
                    entry.pinyin_display or None,
                )
            )

    if surface_rows:
        conn.executemany(
            """
            INSERT OR IGNORE INTO lexeme_surface(surface, lexeme_id, surface_kind, word_len, hsk_rank)
            VALUES (?, ?, ?, ?, ?)
            """,
            surface_rows,
        )
    source_inserted = insert_supplement_sources(conn, source_rows) if source_rows else 0
    conn.commit()
    return {key for key in query_keys if key}, {
        "entries": len(entries),
        "new_lexemes": new_lexemes,
        "existing_matches": existing_matches,
        "surface_rows": len(surface_rows),
        "source_rows": source_inserted,
        "unique_surfaces": len(unique_surfaces),
    }


def load_tatoeba(conn: sqlite3.Connection, raw_dir: Path, matcher: SurfaceMatcher) -> int:
    tatoeba_dir = raw_dir / "tatoeba"
    cmn_to_eng: dict[str, list[str]] = {}
    eng_ids: set[str] = set()

    with bz2.open(tatoeba_dir / "cmn-eng_links.tsv.bz2", "rt", encoding="utf-8") as f:
        for line in f:
            cmn_id, eng_id = line.rstrip("\n").split("\t")[:2]
            cmn_to_eng.setdefault(cmn_id, []).append(eng_id)
            eng_ids.add(eng_id)

    eng_text: dict[str, str] = {}
    with bz2.open(tatoeba_dir / "eng_sentences.tsv.bz2", "rt", encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t", 2)
            if len(parts) == 3 and parts[0] in eng_ids:
                eng_text[parts[0]] = parts[2]

    sentence_rows: list[tuple] = []
    hit_rows: list[tuple] = []
    count = 0

    with bz2.open(tatoeba_dir / "cmn_sentences.tsv.bz2", "rt", encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t", 2)
            if len(parts) != 3:
                continue
            external_id, _lang, zh = parts
            zh = clean_sentence(zh)
            eng = ""
            for eng_id in cmn_to_eng.get(external_id, []):
                if eng_id in eng_text:
                    eng = eng_text[eng_id]
                    break
            if not should_keep_sentence(zh, eng, SOURCE_TATOEBA):
                continue
            zh_norm = normalize_query(zh)
            score = sentence_quality(zh, eng, SOURCE_TATOEBA)
            sentence_rows.append((SOURCE_TATOEBA, external_id, zh, eng or None, zh_norm, score, len(zh_norm)))

    for batch in batched(sentence_rows, 5000):
        conn.executemany(
            """
            INSERT INTO sentence(source, external_id, zh, en, zh_norm, quality_score, length_chars)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            batch,
        )
    conn.commit()

    for sentence_id, zh_norm, score in conn.execute(
        "SELECT sentence_id, zh_norm, quality_score FROM sentence WHERE source=?",
        (SOURCE_TATOEBA,),
    ):
        matches = matcher.find(zh_norm, max_hits=96)
        for match in matches:
            hit_rows.append((match.key, SOURCE_TATOEBA, score, sentence_id, match.kind))
        count += 1
        if len(hit_rows) >= 20000:
            conn.executemany(
                """
                INSERT OR IGNORE INTO example_hit(query_key, source, score, sentence_id, hit_kind)
                VALUES (?, ?, ?, ?, ?)
                """,
                hit_rows,
            )
            hit_rows.clear()

    if hit_rows:
        conn.executemany(
            """
            INSERT OR IGNORE INTO example_hit(query_key, source, score, sentence_id, hit_kind)
            VALUES (?, ?, ?, ?, ?)
            """,
            hit_rows,
        )
    conn.commit()
    return count


def consider_candidate(
    heaps: dict[str, list[Candidate]],
    key: str,
    candidate: Candidate,
    per_key: int,
) -> None:
    heap = heaps.get(key)
    if heap is None:
        heaps[key] = [candidate]
        return
    if len(heap) < per_key:
        heapq.heappush(heap, candidate)
        return
    if candidate.score > heap[0].score:
        heapq.heapreplace(heap, candidate)


def iter_opensubtitles(raw_dir: Path) -> Iterable[tuple[str, str]]:
    zip_path = raw_dir / "opensubtitles_opus" / "OpenSubtitles-v2018-en-zh_cn-moses.zip"
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open("OpenSubtitles.en-zh_cn.zh_cn") as zh_raw, zf.open("OpenSubtitles.en-zh_cn.en") as en_raw:
            zh_stream = io.TextIOWrapper(zh_raw, encoding="utf-8", errors="replace", newline="")
            en_stream = io.TextIOWrapper(en_raw, encoding="utf-8", errors="replace", newline="")
            for zh, en in zip(zh_stream, en_stream):
                yield zh.rstrip("\r\n"), en.rstrip("\r\n")


def load_opensubtitles_compact(
    conn: sqlite3.Connection,
    raw_dir: Path,
    matcher: SurfaceMatcher,
    per_key: int,
    line_limit: int | None,
    progress_every: int,
) -> tuple[int, int]:
    heaps: dict[str, list[Candidate]] = {}
    seq = 0
    scanned = 0
    kept_for_matching = 0
    started = time.time()

    for zh_raw, en_raw in iter_opensubtitles(raw_dir):
        scanned += 1
        if line_limit is not None and scanned > line_limit:
            break
        zh = clean_sentence(zh_raw)
        en = clean_sentence(en_raw)
        if not should_keep_sentence(zh, en, SOURCE_OPENSUBTITLES):
            continue
        zh_norm = normalize_query(zh)
        matches = matcher.find(zh_norm, max_hits=64)
        if not matches:
            continue
        score = sentence_quality(zh, en, SOURCE_OPENSUBTITLES)
        kept_for_matching += 1
        seq += 1
        for match in matches:
            consider_candidate(
                heaps,
                match.key,
                Candidate(score=score, seq=seq, zh=zh, en=en, hit_kind=match.kind),
                per_key=per_key,
            )

        if progress_every and scanned % progress_every == 0:
            elapsed = time.time() - started
            print(
                f"opensubtitles scanned={scanned:,} matched={kept_for_matching:,} "
                f"keys={len(heaps):,} elapsed={elapsed:.1f}s",
                flush=True,
            )

    sentence_id_by_pair: dict[tuple[str, str], int] = {}
    hit_rows: list[tuple] = []
    inserted_sentences = 0

    for key, heap in heaps.items():
        for candidate in sorted(heap, reverse=True):
            pair = (candidate.zh, candidate.en)
            sentence_id = sentence_id_by_pair.get(pair)
            if sentence_id is None:
                zh_norm = normalize_query(candidate.zh)
                cur = conn.execute(
                    """
                    INSERT INTO sentence(source, external_id, zh, en, zh_norm, quality_score, length_chars)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        SOURCE_OPENSUBTITLES,
                        None,
                        candidate.zh,
                        candidate.en or None,
                        zh_norm,
                        candidate.score,
                        len(zh_norm),
                    ),
                )
                sentence_id = int(cur.lastrowid)
                sentence_id_by_pair[pair] = sentence_id
                inserted_sentences += 1
            hit_rows.append((key, SOURCE_OPENSUBTITLES, candidate.score, sentence_id, candidate.hit_kind))
            if len(hit_rows) >= 20000:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO example_hit(query_key, source, score, sentence_id, hit_kind)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    hit_rows,
                )
                hit_rows.clear()

    if hit_rows:
        conn.executemany(
            """
            INSERT OR IGNORE INTO example_hit(query_key, source, score, sentence_id, hit_kind)
            VALUES (?, ?, ?, ?, ?)
            """,
            hit_rows,
        )

    conn.commit()
    return scanned, inserted_sentences


def build_database(args: argparse.Namespace) -> None:
    raw_dir = args.raw_dir.resolve()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and not args.force:
        raise SystemExit(f"Output exists: {output}. Use --force to overwrite.")
    if output.exists():
        output.unlink()

    started = time.time()
    conn = sqlite3.connect(output)
    configure_build_connection(conn, cache_mb=args.cache_mb)
    create_schema(conn)

    print("loading hsk", flush=True)
    word_levels, char_rows = load_hsk(raw_dir)

    print("loading cedict", flush=True)
    lexeme_rows = parse_cedict(raw_dir, word_levels)
    query_keys = insert_lexemes(conn, lexeme_rows, word_levels, char_rows)

    print("loading hanzi-words supplements", flush=True)
    supplement_keys, supplement_counts = insert_supplement_lexemes(conn, raw_dir, word_levels)
    query_keys.update(supplement_keys)

    hsk_counts = rebuild_hsk_serving_tables(conn)
    suggestion_counts = rebuild_suggestion_table(conn)
    query_keys = {key for key in query_keys if len(key) <= args.max_key_len}
    matcher = SurfaceMatcher(query_keys, max_key_len=args.max_key_len)
    conn.commit()
    lexeme_total = int(conn.execute("SELECT count(*) FROM lexeme").fetchone()[0])
    print(
        f"cedict_lexemes={len(lexeme_rows):,} total_lexemes={lexeme_total:,} "
        f"supplement_new={supplement_counts['new_lexemes']:,} "
        f"supplement_existing={supplement_counts['existing_matches']:,} "
        f"query_keys={len(query_keys):,} "
        f"hsk_words={hsk_counts['words']:,} hsk_chars={hsk_counts['chars']:,} "
        f"zh_suggestions={suggestion_counts['zh']:,} "
        f"char_suggestions={suggestion_counts.get('char', 0):,} "
        f"en_suggestions={suggestion_counts['en']:,}",
        flush=True,
    )

    print("loading tatoeba", flush=True)
    tatoeba_count = load_tatoeba(conn, raw_dir, matcher)
    print(f"tatoeba_sentences={tatoeba_count:,}", flush=True)

    print("loading opensubtitles compact index", flush=True)
    os_scanned, os_inserted = load_opensubtitles_compact(
        conn,
        raw_dir,
        matcher,
        per_key=args.opensubtitles_per_key,
        line_limit=args.opensubtitles_limit,
        progress_every=args.progress_every,
    )
    print(f"opensubtitles_scanned={os_scanned:,} selected_sentences={os_inserted:,}", flush=True)

    print("deduplicating examples", flush=True)
    dedupe_counts = dedupe_sentences(conn, rebuild_fts=False, dry_run=False)
    conn.commit()
    print(
        f"dedupe_sentence_deletes={dedupe_counts['deleted_sentences']:,} "
        f"dedupe_hit_deletes={dedupe_counts['deleted_example_hits']:,}",
        flush=True,
    )

    print("creating indexes", flush=True)
    create_indexes(conn)
    if args.fts:
        print("creating selected fts", flush=True)
        create_selected_fts(conn)

    print("optimizing", flush=True)
    conn.execute("PRAGMA optimize")
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"SQLite integrity check failed: {integrity}")

    elapsed = time.time() - started
    write_metadata(
        conn,
        build_profile="compact",
        elapsed_seconds=round(elapsed, 3),
        raw_dir=str(raw_dir),
        lexemes=lexeme_total,
        cedict_lexemes=len(lexeme_rows),
        supplement_entries=supplement_counts["entries"],
        supplement_unique_surfaces=supplement_counts["unique_surfaces"],
        supplement_new_lexemes=supplement_counts["new_lexemes"],
        supplement_existing_matches=supplement_counts["existing_matches"],
        supplement_source_rows=supplement_counts["source_rows"],
        supplement_surface_rows=supplement_counts["surface_rows"],
        hsk_words=len(word_levels),
        hsk_chars=len(char_rows),
        hsk_serving_words=hsk_counts["words"],
        hsk_serving_chars=hsk_counts["chars"],
        zh_suggestion_surfaces=suggestion_counts["zh"],
        en_suggestion_terms=suggestion_counts["en"],
        query_keys=len(query_keys),
        tatoeba_sentences=tatoeba_count,
        opensubtitles_scanned=os_scanned,
        opensubtitles_selected_sentences=os_inserted,
        opensubtitles_per_key=args.opensubtitles_per_key,
        opensubtitles_limit=args.opensubtitles_limit,
        dedupe_deleted_sentences=dedupe_counts["deleted_sentences"],
        dedupe_deleted_example_hits=dedupe_counts["deleted_example_hits"],
        max_key_len=args.max_key_len,
        fts=bool(args.fts),
    )
    conn.commit()
    conn.close()

    size_mb = output.stat().st_size / 1024 / 1024
    print(f"done output={output} size_mb={size_mb:.1f} elapsed={elapsed:.1f}s", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the Chinese dictionary SQLite database.")
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--output", type=Path, default=Path("data/processed/dictionary_compact.db"))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--cache-mb", type=int, default=192)
    parser.add_argument("--max-key-len", type=int, default=8)
    parser.add_argument("--opensubtitles-per-key", type=int, default=8)
    parser.add_argument("--opensubtitles-limit", type=int, default=None)
    parser.add_argument("--progress-every", type=int, default=500000)
    parser.add_argument("--fts", action="store_true", help="Build FTS5 over selected serving sentences.")
    return parser.parse_args()


def main() -> None:
    build_database(parse_args())


if __name__ == "__main__":
    main()
