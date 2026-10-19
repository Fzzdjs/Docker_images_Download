#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""终端进度显示 —— 进度条与旋转指示器，仅依赖标准库。

设计要点:
1. **自适应宽度**: 按终端列数压缩进度条本身, 窄屏自动省略 ETA, 不换行、不刷屏。
2. **非 TTY 降级**: 输出被重定向到文件/管道时, 不做 `\\r` 覆盖, 改为每 10% 打一行,
   日志可读且不会塞满控制字符。
3. **平滑速度**: 用最近几秒的滑动窗口算瞬时速度, 避免平均值在限速抖动时来回跳。
4. **Windows 兼容**: 调用方先把 stdout 设为 UTF-8; 万一仍有编码错误, 自动退回 ASCII 字符。
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from collections import deque

RESET = "\033[0m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"


def _isatty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def supports_color(stream=None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if not _isatty(stream):
        return False
    if os.name != "nt":
        return True
    # Windows 10+ 的控制台在开启 VT 处理后支持 ANSI
    return bool(os.environ.get("WT_SESSION") or os.environ.get("TERM")
                or os.environ.get("ANSICON") or os.environ.get("ConEmuANSI") == "ON")


def terminal_width(default: int = 100) -> int:
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except Exception:
        return default


def human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0
    return "%.1f TB" % n


def duration(sec: float) -> str:
    sec = max(0, int(round(sec)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%02d:%02d" % (m, s)


class ProgressBar:
    """字节级进度条。

    >>> bar = ProgressBar(1024 ** 3, label="下载")
    >>> bar.update(512 * 1024 ** 2); bar.close()
    """

    def __init__(self, total: int = 0, label: str = "", stream=None,
                 enabled: bool = True, width: int | None = None,
                 min_interval: float = 0.1):
        self.stream = stream or sys.stdout
        self.total = max(int(total or 0), 0)
        self.label = label
        self.enabled = enabled
        self.width = width
        self.min_interval = min_interval
        self.interactive = enabled and _isatty(self.stream)
        self.color = enabled and supports_color(self.stream)
        self.ascii = False
        self._t0 = time.time()
        self._last_emit = 0.0
        self._bucket = -1
        self._samples: deque = deque(maxlen=16)
        self._closed = False
        if enabled and not self.interactive:
            self._write(self._line(0, colors=False), newline=True)
            self._bucket = 0
            self._last_emit = time.time()

    # ---------------------------------------------------------------- 内部
    def _blocks(self) -> tuple[str, str]:
        return ("=", "-") if self.ascii else ("█", "░")

    def _write(self, text: str, newline: bool = False, carriage: bool = False) -> None:
        try:
            if carriage:
                self.stream.write("\r" + text + "\033[K")
            else:
                self.stream.write(text + ("\n" if newline else ""))
            self.stream.flush()
        except UnicodeEncodeError:
            # 极端环境（老式代码页）退化为 ASCII
            self.ascii = True
            self._write(text.encode("ascii", "replace").decode("ascii"), newline, carriage)

    def _speed(self) -> float:
        if len(self._samples) < 2:
            return 0.0
        t0, d0 = self._samples[0]
        t1, d1 = self._samples[-1]
        span = t1 - t0
        if span < 0.8:                       # 样本太短, 用全程平均兜底
            span = max(t1 - self._t0, 0.001)
            d0 = 0
        return max((d1 - d0) / span, 0.0)

    def _line(self, done: int, colors: bool = True) -> str:
        done = max(0, int(done))
        if not self.total:
            text = "%s %s 已下载" % (self.label, human(done)) if self.label else "%s 已下载" % human(done)
            if colors and self.color:
                return DIM + text + RESET
            return text

        frac = min(1.0, done / float(self.total))
        # 前 1 秒样本太少, 速度与 ETA 会剧烈跳动, 先不显示
        speed = self._speed() if (time.time() - self._t0) >= 1.0 else 0.0
        eta = (self.total - done) / speed if speed > 0 else 0

        head = (self.label + " ") if self.label else ""
        suffix = " %5.1f%%  %s / %s" % (
            frac * 100.0, human(done), human(self.total))
        if speed > 0:
            suffix += "  %s/s" % human(speed)
        if eta > 0:
            suffix += "  剩余 %s" % duration(eta)

        cols = self.width or terminal_width()
        avail = cols - len(head) - len(suffix) - 3
        if avail < 8 and eta > 0:            # 窄屏先砍 ETA
            suffix = " %5.1f%%  %s / %s" % (frac * 100.0, human(done), human(self.total))
            avail = cols - len(head) - len(suffix) - 3
        bar_w = max(8, min(40, avail))
        filled = int(round(frac * bar_w))
        on, off = self._blocks()
        bar = on * filled + off * (bar_w - filled)

        if colors and self.color:
            bar = CYAN + on * filled + RESET + DIM + off * (bar_w - filled) + RESET
            if frac >= 1.0:
                suffix = GREEN + suffix + RESET
        return "%s[%s]%s" % (head, bar, suffix)

    # ---------------------------------------------------------------- 对外
    def update(self, done: int, force: bool = False) -> None:
        if not self.enabled or self._closed:
            return
        done = max(0, int(done))
        now = time.time()
        self._samples.append((now, done))
        if not force and now - self._last_emit < self.min_interval:
            return

        if self.interactive:
            self._write(self._line(done), carriage=True)
        else:
            pct_bucket = int(done * 10 / self.total) if self.total else 0
            finished = self.total and done >= self.total
            if pct_bucket == self._bucket and not (finished and self._bucket < 10):
                return
            self._bucket = pct_bucket
            self._write(self._line(done, colors=False), newline=True)
        self._last_emit = now

    def close(self, message: str | None = None) -> None:
        if not self.enabled or self._closed:
            return
        self._closed = True
        if self.interactive:
            line = message if message is not None else (
                self._line(self.total, colors=True) if self.total else "")
            self._write(line, carriage=True)
            self.stream.write("\n")
            self.stream.flush()
        elif message:
            self._write(message, newline=True)
        elif self.total and self._bucket < 10:
            # 非 TTY 下补一条 100% 收尾, 避免日志停在 90%
            self._write(self._line(self.total, colors=False), newline=True)


class Spinner:
    """带计时的不确定进度指示器（用于等待工作流、拉取镜像等无字节总量的阶段）。

    后台线程负责动画，调用方只需周期性 `update(text)`；`stop()` 之后才可打印别的日志。
    """

    FRAMES = "|/-\\"

    def __init__(self, label: str = "等待", stream=None, enabled: bool = True,
                 interval: float = 0.15):
        self.stream = stream or sys.stdout
        self.label = label
        self.enabled = enabled
        self.interactive = enabled and _isatty(self.stream)
        self.interval = interval
        self.text = ""
        self._t0 = time.time()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._frame = 0
        self._lock = threading.Lock()

    def start(self) -> "Spinner":
        if not self.enabled or not self.interactive:
            return self
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            with self._lock:
                frame = self.FRAMES[self._frame % len(self.FRAMES)]
                self._frame += 1
                text = self.text
            line = "%s %s %s  %s" % (self.label, frame,
                                     duration(time.time() - self._t0), text)
            try:
                self.stream.write("\r" + line + "\033[K")
                self.stream.flush()
            except Exception:
                return

    def update(self, text: str) -> None:
        with self._lock:
            self.text = text
        if self.enabled and not self.interactive:
            self.stream.write("%s %s  %s\n" % (self.label, duration(time.time() - self._t0), text))
            self.stream.flush()

    def stop(self, final: str | None = None) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self.enabled and self.interactive:
            line = final if final is not None else self.text
            try:
                self.stream.write("\r%s %s  %s\033[K\n" % (
                    self.label, duration(time.time() - self._t0), line))
                self.stream.flush()
            except Exception:
                pass
        elif self.enabled and final:
            self.stream.write("%s\n" % final)
            self.stream.flush()
