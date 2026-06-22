from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Match:
    key: str
    kind: int


class SurfaceMatcher:
    """Small memory Chinese surface matcher.

    It indexes candidate lengths by first character. This avoids scanning every
    dictionary key for every sentence, while keeping build dependencies simple.
    """

    def __init__(self, keys: Iterable[str], max_key_len: int) -> None:
        key_set: set[str] = set()
        lengths_by_first: dict[str, set[int]] = {}

        for raw in keys:
            key = raw.strip()
            if not key:
                continue
            if len(key) > max_key_len:
                continue
            key_set.add(key)
            lengths_by_first.setdefault(key[0], set()).add(len(key))

        self.key_set = key_set
        self.lengths_by_first = {
            ch: tuple(sorted(lengths, reverse=True))
            for ch, lengths in lengths_by_first.items()
        }

    def find(self, text: str, max_hits: int = 64) -> list[Match]:
        n = len(text)
        seen: dict[str, int] = {}

        for i, ch in enumerate(text):
            lengths = self.lengths_by_first.get(ch)
            if not lengths:
                continue
            remaining = n - i
            for length in lengths:
                if length > remaining:
                    continue
                candidate = text[i : i + length]
                if candidate in self.key_set:
                    kind = 1 if length == 1 else (2 if length == 2 else 3)
                    old = seen.get(candidate)
                    if old is None or kind > old:
                        seen[candidate] = kind

        if len(seen) <= max_hits:
            return [Match(key, kind) for key, kind in seen.items()]

        items = sorted(seen.items(), key=lambda item: (len(item[0]), item[1]), reverse=True)
        singles = [item for item in items if len(item[0]) == 1][:16]
        longer = [item for item in items if len(item[0]) > 1][: max_hits - len(singles)]
        selected = longer + singles
        return [Match(key, kind) for key, kind in selected]
