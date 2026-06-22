from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from .common import has_cjk, normalize_query, normalize_text


RAW_BASE_URL = "https://raw.githubusercontent.com/zispace/hanzi-words/main/dict/"
REPO_URL = "https://github.com/zispace/hanzi-words/tree/main/dict"
MAX_SURFACE_LEN = 32


@dataclass(frozen=True)
class SourceSpec:
    filename: str
    source_name: str
    parser: str


@dataclass(frozen=True)
class SupplementEntry:
    source_name: str
    source_file: str
    source_row: int
    raw_surface: str
    simplified: str
    traditional: str
    pinyin_display: str

    @property
    def surfaces(self) -> tuple[str, ...]:
        items = [self.simplified]
        if self.traditional and self.traditional != self.simplified:
            items.append(self.traditional)
        seen: set[str] = set()
        result: list[str] = []
        for item in items:
            if item and item not in seen:
                result.append(item)
                seen.add(item)
        return tuple(result)


SOURCE_SPECS = [
    SourceSpec("中華語文大辭典.txt", "中華語文大辭典", "zhonghua"),
    SourceSpec("兩岸差異用詞.txt", "兩岸差異用詞", "cross_strait"),
    SourceSpec("古代汉语词典（第1版）.txt", "古代汉语词典（第1版）", "single"),
    SourceSpec("现代汉语大词典.txt", "现代汉语大词典", "single"),
    SourceSpec("现代汉语规范词典.txt", "现代汉语规范词典", "word_pinyin"),
    SourceSpec("现代汉语词典（第7版）-拼音.txt", "现代汉语词典（第7版）-拼音", "word_index_pinyin"),
    SourceSpec("现代汉语词典（第7版）.txt", "现代汉语词典（第7版）", "single"),
    SourceSpec("臺灣台語常用詞辭典.txt", "臺灣台語常用詞辭典", "taiwanese"),
    SourceSpec("近代汉语词典.txt", "近代汉语词典", "single"),
]


def dict_dir(raw_dir: Path) -> Path:
    return raw_dir / "hanzi_words" / "dict"


def source_url(filename: str) -> str:
    return RAW_BASE_URL + quote(filename)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean_surface(value: str) -> str:
    value = normalize_query(value)
    value = value.strip("[]()（）【】《》〈〉")
    return value


def valid_surface(surface: str) -> bool:
    if not surface or surface.startswith("#"):
        return False
    if len(surface) > MAX_SURFACE_LEN:
        return False
    if not has_cjk(surface):
        return False
    if "http://" in surface.lower() or "https://" in surface.lower() or "www." in surface.lower():
        return False
    return True


def make_entry(
    spec: SourceSpec,
    row_number: int,
    simplified: str,
    traditional: str | None = None,
    pinyin_display: str | None = None,
    raw_surface: str | None = None,
) -> SupplementEntry | None:
    simplified = clean_surface(simplified)
    traditional = clean_surface(traditional or simplified)
    if not valid_surface(simplified):
        return None
    if traditional and not valid_surface(traditional):
        traditional = simplified
    return SupplementEntry(
        source_name=spec.source_name,
        source_file=spec.filename,
        source_row=row_number,
        raw_surface=normalize_text(raw_surface or simplified),
        simplified=simplified,
        traditional=traditional or simplified,
        pinyin_display=normalize_text(pinyin_display or ""),
    )


def entries_from_parts(spec: SourceSpec, row_number: int, parts: list[str]) -> list[SupplementEntry]:
    entries: list[SupplementEntry] = []
    if spec.parser == "zhonghua":
        traditional = parts[0] if len(parts) > 0 else ""
        simplified = parts[1] if len(parts) > 1 and parts[1] else traditional
        pinyin = ""
        if len(parts) > 7 and parts[7]:
            pinyin = parts[7]
        elif len(parts) > 6:
            pinyin = parts[6]
        entry = make_entry(
            spec,
            row_number,
            simplified=simplified,
            traditional=traditional,
            pinyin_display=pinyin,
            raw_surface="\t".join(parts[:2]),
        )
        if entry:
            entries.append(entry)
    elif spec.parser == "cross_strait":
        for value in parts[:2]:
            entry = make_entry(spec, row_number, simplified=value, raw_surface=value)
            if entry:
                entries.append(entry)
    elif spec.parser == "word_pinyin":
        headword = parts[0] if len(parts) > 0 else ""
        full_word = parts[1] if len(parts) > 1 and parts[1] else headword
        pinyin = parts[2] if len(parts) > 2 else ""
        for value in (full_word, headword):
            entry = make_entry(spec, row_number, simplified=value, pinyin_display=pinyin, raw_surface="\t".join(parts))
            if entry and entry not in entries:
                entries.append(entry)
    elif spec.parser == "word_index_pinyin":
        headword = parts[0] if len(parts) > 0 else ""
        full_word = parts[2] if len(parts) > 2 and parts[2] else headword
        pinyin = parts[3] if len(parts) > 3 else ""
        for value in (full_word, headword):
            entry = make_entry(spec, row_number, simplified=value, pinyin_display=pinyin, raw_surface="\t".join(parts))
            if entry and entry not in entries:
                entries.append(entry)
    elif spec.parser == "taiwanese":
        entry = make_entry(spec, row_number, simplified=parts[0] if parts else "", raw_surface="\t".join(parts))
        if entry:
            entries.append(entry)
    else:
        entry = make_entry(spec, row_number, simplified=parts[0] if parts else "", raw_surface=parts[0] if parts else "")
        if entry:
            entries.append(entry)
    return entries


def iter_supplement_entries(raw_dir: Path) -> list[SupplementEntry]:
    entries: list[SupplementEntry] = []
    root = dict_dir(raw_dir)
    for spec in SOURCE_SPECS:
        path = root / spec.filename
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            for row_number, line in enumerate(f, start=1):
                line = line.rstrip("\r\n")
                if not line.strip() or line.startswith("#"):
                    continue
                parts = line.split("\t")
                entries.extend(entries_from_parts(spec, row_number, parts))
    return entries


def download_sources(raw_dir: Path, overwrite: bool = False) -> dict[str, object]:
    root = dict_dir(raw_dir)
    root.mkdir(parents=True, exist_ok=True)
    manifest_items = []
    for spec in SOURCE_SPECS:
        url = source_url(spec.filename)
        path = root / spec.filename
        if path.exists() and not overwrite:
            data = path.read_bytes()
        else:
            req = Request(url, headers={"User-Agent": "codex-dictionary-builder"})
            with urlopen(req, timeout=120) as response:
                data = response.read()
            path.write_bytes(data)
        manifest_items.append(
            {
                "filename": spec.filename,
                "source_name": spec.source_name,
                "url": url,
                "bytes": len(data),
                "sha256": sha256_bytes(data),
            }
        )
    manifest = {
        "repo": REPO_URL,
        "downloaded_at_unix": int(time.time()),
        "files": manifest_items,
    }
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def entry_stats(raw_dir: Path) -> dict[str, object]:
    all_entries = iter_supplement_entries(raw_dir)
    unique_surfaces = set()
    by_file: dict[str, dict[str, object]] = {}
    for entry in all_entries:
        item = by_file.setdefault(entry.source_file, {"entries": 0, "unique_surfaces": set()})
        item["entries"] = int(item["entries"]) + 1
        for surface in entry.surfaces:
            unique_surfaces.add(surface)
            item["unique_surfaces"].add(surface)  # type: ignore[union-attr]
    files = []
    for filename, item in sorted(by_file.items()):
        files.append(
            {
                "filename": filename,
                "entries": item["entries"],
                "unique_surfaces": len(item["unique_surfaces"]),  # type: ignore[arg-type]
            }
        )
    return {"entries": len(all_entries), "unique_surfaces": len(unique_surfaces), "files": files}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download and inspect hanzi-words supplemental dictionaries.")
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--stats", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result: dict[str, object] = {}
    if args.download:
        result["manifest"] = download_sources(args.raw_dir, overwrite=args.overwrite)
    if args.stats:
        result["stats"] = entry_stats(args.raw_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
