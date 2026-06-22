from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from .common import open_readonly_db
from .query import explain_plans, lookup


DEFAULT_QUERIES = [
    "的",
    "我",
    "你",
    "爱",
    "你好",
    "谢谢",
    "中国",
    "学习",
    "电脑",
    "数据库",
    "我爱你",
    "你在干什么",
    "學習",
    "電腦",
    "不存在的奇怪短语zz",
]


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1))))
    return ordered[index]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark dictionary queries.")
    parser.add_argument("--db", type=Path, default=Path("data/processed/dictionary_compact.db"))
    parser.add_argument("--queries", nargs="*", default=DEFAULT_QUERIES)
    parser.add_argument("--repeat", type=int, default=20)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--cache-mb", type=int, default=96)
    parser.add_argument("--mutable", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output = {
        "db": str(args.db),
        "repeat": args.repeat,
        "limit": args.limit,
        "queries": [],
        "summary": {},
    }

    all_latencies: list[float] = []
    with open_readonly_db(args.db, immutable=not args.mutable, cache_mb=args.cache_mb) as conn:
        for query in args.queries:
            latencies: list[float] = []
            last = None
            for _ in range(args.repeat):
                started = time.perf_counter()
                last = lookup(conn, query, limit=args.limit)
                latencies.append((time.perf_counter() - started) * 1000)
            all_latencies.extend(latencies)
            entry = {
                "query": query,
                "min_ms": round(min(latencies), 3),
                "mean_ms": round(statistics.mean(latencies), 3),
                "p95_ms": round(percentile(latencies, 95), 3),
                "max_ms": round(max(latencies), 3),
                "dictionary_count": len(last["dictionary"]) if last else 0,
                "tatoeba_count": len(last["examples"]["tatoeba"]) if last else 0,
                "opensubtitles_count": len(last["examples"]["opensubtitles"]) if last else 0,
                "fts_count": len(last["examples"]["fts"]) if last else 0,
                "plans": explain_plans(conn, query, args.limit),
            }
            output["queries"].append(entry)

    output["summary"] = {
        "min_ms": round(min(all_latencies), 3),
        "mean_ms": round(statistics.mean(all_latencies), 3),
        "p95_ms": round(percentile(all_latencies, 95), 3),
        "max_ms": round(max(all_latencies), 3),
    }

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(output["summary"], ensure_ascii=False, indent=2))
        for entry in output["queries"]:
            print(
                f"{entry['query']}: p95={entry['p95_ms']}ms "
                f"dict={entry['dictionary_count']} tat={entry['tatoeba_count']} "
                f"os={entry['opensubtitles_count']} fts={entry['fts_count']}"
            )


if __name__ == "__main__":
    main()
