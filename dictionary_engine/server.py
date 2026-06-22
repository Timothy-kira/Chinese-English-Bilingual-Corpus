from __future__ import annotations

import argparse
import json
import queue
import sqlite3
import time
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .common import open_readonly_db
from .hsk import hsk_levels, list_hsk_chars, list_hsk_words
from .query import lookup
from .suggest import suggest_words, init_conversion_map


def parse_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(minimum, min(maximum, parsed))


class SQLitePool:
    def __init__(self, db_path: Path, size: int, cache_mb: int) -> None:
        self._pool: queue.LifoQueue[sqlite3.Connection] = queue.LifoQueue(maxsize=size)
        for _ in range(size):
            self._pool.put(
                open_readonly_db(
                    db_path,
                    immutable=True,
                    cache_mb=cache_mb,
                    check_same_thread=False,
                )
            )

    @contextmanager
    def connection(self) -> Any:
        conn = self._pool.get()
        try:
            yield conn
        finally:
            self._pool.put(conn)

    def close(self) -> None:
        while not self._pool.empty():
            self._pool.get_nowait().close()


class DictionaryHandler(BaseHTTPRequestHandler):
    server_version = "DictionaryHTTP/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        if getattr(self.server, "quiet", False):
            return
        super().log_message(fmt, *args)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_common_headers()
        self.end_headers()

    def do_GET(self) -> None:
        started = time.perf_counter()
        try:
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            route = parsed.path.rstrip("/") or "/"
            if route == "/health":
                self.send_json({"ok": True})
                return
            if route == "/api/query":
                self.handle_query(params, started)
                return
            if route == "/api/suggest":
                self.handle_suggest(params, started)
                return
            if route == "/api/hsk/levels":
                self.handle_hsk_levels(started)
                return
            if route == "/api/hsk/words":
                self.handle_hsk_words(params, started)
                return
            if route == "/api/hsk/chars":
                self.handle_hsk_chars(params, started)
                return
            self.send_json({"error": "not_found", "path": route}, status=HTTPStatus.NOT_FOUND)
        except Exception as exc:  # pragma: no cover - defensive server boundary
            self.send_json(
                {"error": "internal_error", "message": str(exc)},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def first_param(self, params: dict[str, list[str]], key: str, default: str | None = None) -> str | None:
        values = params.get(key)
        if not values:
            return default
        return values[0]

    def handle_query(self, params: dict[str, list[str]], started: float) -> None:
        q = self.first_param(params, "q", "")
        if not q:
            self.send_json({"error": "missing_query"}, status=HTTPStatus.BAD_REQUEST)
            return
        limit = parse_int(self.first_param(params, "limit"), default=8, minimum=1, maximum=50)
        with self.server.pool.connection() as conn:
            result = lookup(conn, q, limit=limit)
        result["server_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        self.send_json(result)

    def handle_suggest(self, params: dict[str, list[str]], started: float) -> None:
        q = self.first_param(params, "q", "")
        if not q:
            self.send_json({"query": "", "normalized_query": "", "items": [], "limit": 0})
            return
        limit = parse_int(self.first_param(params, "limit"), default=12, minimum=1, maximum=50)
        hsk_level = self.first_param(params, "hsk_level", None)
        with self.server.pool.connection() as conn:
            result = suggest_words(conn, q, limit=limit, hsk_level=hsk_level)
        result["server_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        self.send_json(result)

    def handle_hsk_levels(self, started: float) -> None:
        with self.server.pool.connection() as conn:
            result = {"levels": hsk_levels(conn)}
        result["server_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        self.send_json(result)

    def handle_hsk_words(self, params: dict[str, list[str]], started: float) -> None:
        level = self.first_param(params, "level", None)
        limit = parse_int(self.first_param(params, "limit"), default=100, minimum=1, maximum=500)
        offset = parse_int(self.first_param(params, "offset"), default=0, minimum=0, maximum=10_000_000)
        min_len = parse_int(self.first_param(params, "min_len"), default=1, minimum=1, maximum=16)
        with self.server.pool.connection() as conn:
            result = list_hsk_words(conn, level, limit=limit, offset=offset, min_len=min_len)
        result["server_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        self.send_json(result)

    def handle_hsk_chars(self, params: dict[str, list[str]], started: float) -> None:
        level = self.first_param(params, "level", None)
        limit = parse_int(self.first_param(params, "limit"), default=100, minimum=1, maximum=500)
        offset = parse_int(self.first_param(params, "offset"), default=0, minimum=0, maximum=10_000_000)
        with self.server.pool.connection() as conn:
            result = list_hsk_chars(conn, level, limit=limit, offset=offset)
        result["server_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        self.send_json(result)

    def send_common_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")

    def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class DictionaryServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[DictionaryHandler],
        pool: SQLitePool,
        quiet: bool,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.pool = pool
        self.quiet = quiet


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the low-resource dictionary HTTP API.")
    parser.add_argument("--db", type=Path, default=Path("data/processed/dictionary_compact.db"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--pool-size", type=int, default=2)
    parser.add_argument("--cache-mb", type=int, default=64)
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    pool_size = max(1, min(args.pool_size, 4))
    pool = SQLitePool(args.db, size=pool_size, cache_mb=args.cache_mb)
    with pool.connection() as conn:
        init_conversion_map(conn)
    server = DictionaryServer((args.host, args.port), DictionaryHandler, pool=pool, quiet=args.quiet)
    print(f"serving http://{args.host}:{args.port} db={args.db} pool={pool_size}", flush=True)
    try:
        server.serve_forever()
    finally:
        pool.close()


if __name__ == "__main__":
    main()
