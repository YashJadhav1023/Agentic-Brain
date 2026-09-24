#!/usr/bin/env python3
"""Loopback-only, read-only semantic retrieval sidecar for the Shared Brain.

The sidecar keeps Basic Memory's embedding/reranker process warm while adding a
small, explicit service contract: canonical store generation, bounded admission,
cache invalidation, deadlines, request provenance, and truthful readiness.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3334
DEFAULT_PROJECT = "brain"
BRAIN_DIR = Path(os.environ.get("BRAIN_DIR", Path.home() / "agentic-brain")).resolve()
CACHE_TTL_SECONDS = 60.0
CACHE_MAX_ENTRIES = 64
MAX_CONCURRENT_SEARCHES = 2
MAX_INFLIGHT_REQUESTS = 6
SEARCH_TIMEOUT_SECONDS = 15.0
WARMUP_TIMEOUT_SECONDS = 45.0


class SearchEngine:
    """One warm Basic Memory backend with bounded, observable retrieval."""

    def __init__(self, project: str):
        self.project = project
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="brain-search-loop", daemon=True)
        self._search_fn = None
        self._semaphore: asyncio.Semaphore | None = None
        self._cache: dict[tuple, tuple[float, dict]] = {}
        self._cache_lock = threading.Lock()
        self._admission = threading.BoundedSemaphore(MAX_INFLIGHT_REQUESTS)
        self._metrics_lock = threading.Lock()
        self.ready = False
        self.warmup_error: str | None = None
        self.warmup_seconds: float | None = None
        self.last_successful_search_at: float | None = None
        self.last_failure: str | None = None
        self.active_requests = 0
        self.total_requests = 0
        self.cache_hits = 0
        self.cache_misses = 0

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def start(self) -> None:
        self._thread.start()
        threading.Thread(target=self._warmup, name="brain-search-warmup", daemon=True).start()

    def _warmup(self) -> None:
        started = time.monotonic()
        try:
            from basic_memory.mcp.tools import search_notes
            self._search_fn = getattr(search_notes, "fn", search_notes)
            asyncio.run_coroutine_threadsafe(self._make_semaphore(), self._loop).result(timeout=30)
            self.search("warmup query", page_size=1, use_cache=False, timeout=WARMUP_TIMEOUT_SECONDS)
            self.warmup_seconds = time.monotonic() - started
            self.ready = True
            print(f"[sidecar] ready after {self.warmup_seconds:.1f}s; project={self.project}", flush=True)
        except Exception as exc:  # surfaced by readiness endpoints
            self.warmup_error = f"{type(exc).__name__}: {exc}"
            self.last_failure = self.warmup_error
            print(f"[sidecar] warmup failed: {self.warmup_error}", file=sys.stderr, flush=True)

    async def _make_semaphore(self) -> None:
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)

    def generation(self) -> str:
        """A stable digest of visible Markdown paths/mtimes/sizes for cache safety."""
        digest = hashlib.sha256()
        if BRAIN_DIR.exists():
            for path in sorted(BRAIN_DIR.rglob("*.md")):
                if ".git" in path.parts:
                    continue
                try:
                    stat = path.stat()
                    digest.update(str(path.relative_to(BRAIN_DIR)).encode())
                    digest.update(f"\0{stat.st_mtime_ns}\0{stat.st_size}\n".encode())
                except OSError:
                    continue
        return digest.hexdigest()[:16]

    def invalidate_cache(self) -> str:
        with self._cache_lock:
            self._cache.clear()
        return self.generation()

    def try_admit(self) -> bool:
        if not self._admission.acquire(blocking=False):
            return False
        with self._metrics_lock:
            self.active_requests += 1
            self.total_requests += 1
        return True

    def release_admission(self) -> None:
        with self._metrics_lock:
            self.active_requests = max(0, self.active_requests - 1)
        self._admission.release()

    def search(self, query: str, page_size: int = 10, use_cache: bool = True, timeout: float | None = None) -> dict:
        if self._search_fn is None:
            raise RuntimeError("search backend not initialised")
        effective_timeout = timeout or SEARCH_TIMEOUT_SECONDS
        generation = self.generation()
        key = (generation, query, page_size)
        if use_cache:
            cached = self._cache_get(key)
            if cached is not None:
                with self._metrics_lock:
                    self.cache_hits += 1
                return cached
        with self._metrics_lock:
            self.cache_misses += 1
        future = asyncio.run_coroutine_threadsafe(self._search(query, page_size), self._loop)
        try:
            result = future.result(timeout=effective_timeout)
        except TimeoutError:
            future.cancel()
            raise TimeoutError(f"search exceeded {effective_timeout}s deadline")
        result["retrieval"] = {
            "mode": "hybrid",
            "index_generation": generation,
            "source": "basic-memory",
            "cached": False,
        }
        self.last_successful_search_at = time.time()
        self.last_failure = None
        if use_cache:
            self._cache_put(key, result)
        return copy.deepcopy(result)

    async def _search(self, query: str, page_size: int) -> dict:
        assert self._search_fn is not None
        if self._semaphore is None:
            await self._make_semaphore()
        assert self._semaphore is not None
        async with self._semaphore:
            raw = await self._search_fn(query=query, project=self.project, page_size=page_size, output_format="json")
        return _normalise(raw)

    def _cache_get(self, key: tuple) -> dict | None:
        with self._cache_lock:
            item = self._cache.get(key)
            if item and time.time() - item[0] < CACHE_TTL_SECONDS:
                result = copy.deepcopy(item[1])
                result.setdefault("retrieval", {})["cached"] = True
                return result
            if item:
                self._cache.pop(key, None)
        return None

    def _cache_put(self, key: tuple, value: dict) -> None:
        with self._cache_lock:
            if len(self._cache) >= CACHE_MAX_ENTRIES:
                oldest = min(self._cache.items(), key=lambda item: item[1][0])[0]
                self._cache.pop(oldest, None)
            self._cache[key] = (time.time(), copy.deepcopy(value))

    def health(self) -> dict:
        with self._metrics_lock:
            return {
                "ready": self.ready and self.last_successful_search_at is not None,
                "project": self.project,
                "warmup_seconds": self.warmup_seconds,
                "warmup_error": self.warmup_error,
                "last_successful_search_at": self.last_successful_search_at,
                "last_failure": self.last_failure,
                "index_generation": self.generation(),
                "active_requests": self.active_requests,
                "total_requests": self.total_requests,
                "cache_hits": self.cache_hits,
                "cache_misses": self.cache_misses,
                "cache_entries": len(self._cache),
            }


def _normalise(raw: object) -> dict:
    if isinstance(raw, dict):
        result = raw
    elif isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Basic Memory returned non-JSON search output") from exc
        result = parsed if isinstance(parsed, dict) else {"results": parsed}
    else:
        dump = getattr(raw, "model_dump", None)
        if not callable(dump):
            raise ValueError("Basic Memory returned an unsupported search output")
        result = dump(mode="json")
    if not isinstance(result.get("results", []), list):
        raise ValueError("Basic Memory search results must be a list")
    result.setdefault("results", [])
    return result


class SidecarHandler(BaseHTTPRequestHandler):
    server_version = "BrainSearchSidecar/2.0"
    engine: SearchEngine

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("[sidecar] %s\n" % (fmt % args))

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if urllib.parse.urlparse(self.path).path == "/invalidate":
            self._send(200, {"invalidated": True, "index_generation": self.server.engine.invalidate_cache()})
            return
        self._send(404, {"error": "not found", "results": []})

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        engine: SearchEngine = self.server.engine  # type: ignore[attr-defined]
        if parsed.path == "/livez":
            self._send(200, {"alive": True, "project": engine.project})
            return
        if parsed.path in {"/healthz", "/readyz", "/stats"}:
            health = engine.health()
            self._send(200 if parsed.path != "/readyz" or health["ready"] else 503, health)
            return
        if parsed.path != "/search":
            self._send(404, {"error": "not found", "results": []})
            return
        query = (params.get("q", [""])[0] or "").strip()
        if not query:
            self._send(400, {"error": "empty query", "results": []})
            return
        try:
            page_size = max(1, min(50, int(params.get("page_size", ["10"])[0])))
        except ValueError:
            page_size = 10
        if not engine.ready:
            self._send(503, {"error": engine.warmup_error or "search backend warming up", "warming_up": engine.warmup_error is None, "results": []})
            return
        if not engine.try_admit():
            self._send(503, {"error": "search overloaded; retry shortly", "retry_after_seconds": 1, "results": []})
            return
        started = time.monotonic()
        request_id = uuid.uuid4().hex
        try:
            payload = engine.search(query, page_size=page_size)
            payload["request_id"] = request_id
            payload["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
            self._send(200, payload)
        except TimeoutError as exc:
            engine.last_failure = str(exc)
            self._send(504, {"error": str(exc), "request_id": request_id, "results": []})
        except ValueError as exc:
            engine.last_failure = str(exc)
            self._send(502, {"error": str(exc), "request_id": request_id, "results": []})
        except Exception as exc:
            engine.last_failure = f"{type(exc).__name__}: {exc}"
            self._send(502, {"error": engine.last_failure, "request_id": request_id, "results": []})
        finally:
            engine.release_admission()


class ThreadedSidecar(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    parser = argparse.ArgumentParser(description="Persistent Brain semantic retrieval sidecar")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    args = parser.parse_args()
    engine = SearchEngine(args.project)
    engine.start()
    server = ThreadedSidecar((args.host, args.port), SidecarHandler)
    server.engine = engine  # type: ignore[attr-defined]
    print(f"[sidecar] listening on http://{args.host}:{args.port}; warming", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
