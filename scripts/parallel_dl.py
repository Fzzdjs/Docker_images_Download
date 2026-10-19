#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Concurrent Range downloader.

Why this exists:
    GitHub serves artifacts and release assets from Azure Blob / Fastly.
    Measured from mainland China, a **single connection** is often throttled to
    30-80 KB/s, so a 1 GB image takes 5-9 hours. With 8-16 parallel Range
    requests the aggregate reaches roughly 0.7-1.5 MB/s, i.e. 15-25 minutes
    for the same file.

Design notes:
1. **Range probing first.** Read Content-Length / Accept-Ranges; fall back to a
   single stream when the server does not support ranges.
2. **One file handle per worker.** Each thread seeks to its own offset, so no
   global write lock is needed.
3. **Signed URLs expire.** Azure Blob URLs carry a TTL; the caller can pass a
   ``refresh`` callback to obtain a fresh URL and keep going.
4. **Incremental progress.** Bytes are accounted in 256 KiB pieces; waiting for
   a whole 8 MB chunk makes the progress bar jump instead of move.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
import urllib.error
import urllib.request

import progress as ui

UA = "Docker_images_Download/2.0 (+https://github.com/Fzzdjs/Docker_images_Download)"
MIN_PARALLEL_SIZE = 2 << 20          # below 2 MB a handshake costs more than it saves
DEFAULT_CONNECTIONS = 16
DEFAULT_CHUNK = 8 << 20              # 8 MB per task: good balance for retries
MIN_CHUNK = 1 << 20                  # never split finer than 1 MB


class DownloadError(RuntimeError):
    pass


def _open_range(url: str, start: int, end: int, timeout: int = 120):
    req = urllib.request.Request(url)
    req.add_header("Range", "bytes=%d-%d" % (start, end))
    req.add_header("User-Agent", UA)
    req.add_header("Accept-Encoding", "identity")   # must stay uncompressed or offsets shift
    return urllib.request.urlopen(req, timeout=timeout)


def _probe(url: str, timeout: int = 60) -> tuple[int, bool]:
    """Return ``(total_size, range_supported)``."""
    try:
        r = _open_range(url, 0, 0, timeout)
        total = r.headers.get("Content-Range", "").split("/")[-1]
        r.close()
        return int(total), True
    except urllib.error.HTTPError as e:
        if e.code in (200, 416):
            # Range not supported: the server answered with the whole body.
            size = int(e.headers.get("Content-Length") or 0)
            return size, False
        if e.code in (401, 403):
            raise DownloadError("HTTP %d: download URL expired or unauthorized" % e.code) from None
        raise
    except Exception:
        # Some services answer bytes=0-0 with a plain 200.
        req = urllib.request.Request(url)
        req.add_header("User-Agent", UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return int(r.headers.get("Content-Length") or 0), False


def _single_stream(url: str, dest: str, total: int, label: str = "",
                   enabled: bool = True) -> None:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    bar = ui.ProgressBar(total, label=label, enabled=enabled)
    done = 0
    with urllib.request.urlopen(req, timeout=180) as r, open(dest, "wb") as fh:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            bar.update(done)
    bar.close()


def download(url: str, dest: str, connections: int = DEFAULT_CONNECTIONS,
             chunk_size: int = DEFAULT_CHUNK, progress: bool = True,
             refresh=None, retries: int = 5, label: str = "") -> str:
    """Download ``url`` into ``dest``.

    refresh: optional callback returning a fresh URL; called on 401/403 so a
             long download can survive signature expiry.
    label:   prefix for the progress line, useful when fetching several files.
    """
    total, ranged = _probe(url)
    if progress:
        print("  size: %s    range: %s"
              % (ui.human(total), "supported" if ranged else "not supported, single stream"))
    if not ranged or total < MIN_PARALLEL_SIZE:
        _single_stream(url, dest, total, label=label, enabled=progress)
        return dest

    # Small files still benefit from splitting: on a throttled link a single
    # connection caps the whole download, so a 3 MB asset should not be fetched
    # over one socket either. Keep chunks at 1 MB or larger to avoid overhead.
    chunk_size = max(MIN_CHUNK, min(chunk_size, total // max(1, connections)))

    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    # Pre-allocate so that concurrent writers never race on file extension.
    with open(dest, "wb") as fh:
        fh.truncate(total)

    tasks = [(s, min(s + chunk_size, total) - 1) for s in range(0, total, chunk_size)]
    lock = threading.Lock()
    state = {"done": 0, "next": 0, "url": url, "failed": []}
    t0 = time.time()
    bar = ui.ProgressBar(total, label=label, enabled=progress)

    def worker() -> None:
        while True:
            with lock:
                if state["next"] >= len(tasks):
                    return
                idx = state["next"]
                state["next"] += 1
            start, end = tasks[idx]
            want = end - start + 1

            for attempt in range(1, retries + 1):
                got = 0
                try:
                    with lock:
                        cur_url = state["url"]
                    with _open_range(cur_url, start, end) as r, open(dest, "r+b") as fh:
                        fh.seek(start)
                        # Account in 256 KiB pieces: waiting for a whole 8 MB
                        # chunk makes the bar advance in visible jumps.
                        while got < want:
                            piece = r.read(min(1 << 18, want - got))
                            if not piece:
                                break
                            fh.write(piece)
                            got += len(piece)
                            with lock:
                                state["done"] += len(piece)
                    if got != want:
                        raise DownloadError("chunk length mismatch (%d != %d)" % (got, want))
                    break
                except urllib.error.HTTPError as e:
                    with lock:
                        state["done"] -= got        # roll back bytes counted this attempt
                    if e.code in (401, 403) and refresh is not None:
                        with lock:
                            if state["url"] == cur_url:      # refresh only once
                                state["url"] = refresh()
                        continue
                    if attempt == retries:
                        with lock:
                            state["failed"].append((start, e.code))
                        return
                    time.sleep(min(2 ** attempt, 15))
                except Exception:
                    with lock:
                        state["done"] -= got
                    if attempt == retries:
                        with lock:
                            state["failed"].append((start, "network"))
                        return
                    time.sleep(min(2 ** attempt, 15))

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(min(connections, len(tasks)))]
    for t in threads:
        t.start()

    while any(t.is_alive() for t in threads):
        time.sleep(0.2)
        with lock:
            done = state["done"]
        bar.update(done)
    for t in threads:
        t.join()

    if state["failed"]:
        bar.close("下载中断: %d 个分片失败" % len(state["failed"]))
        raise DownloadError("有 %d 个分片下载失败: %s" % (len(state["failed"]), state["failed"][:3]))

    bar.update(total, force=True)
    bar.close()
    if progress:
        cost = max(time.time() - t0, 0.001)
        human_cost = "%.1f s" % cost if cost < 60 else ui.duration(cost)
        print("  elapsed %s, average %s/s" % (human_cost, ui.human(total / cost)))
    return dest


def free_space(path: str) -> int:
    return shutil.disk_usage(os.path.dirname(os.path.abspath(path)) or ".").free
