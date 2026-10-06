#!/usr/bin/env python3
"""AutoSync 客户端「CDN / 服务端 自动测速选源」验收脚本。

用一个本地假 CDN（HTTP + Range）和一个本地假 MSFP 服务（PING/SIZE/GET）跑真实客户端进程，
两边都带按连接限速的令牌桶，从而可以精确构造「CDN 快 / 服务端快 / 差不多 / 极慢」等各种场景，
再对客户端日志与磁盘状态做断言。

覆盖的验收点：
1. 构建产物存在
2. CDN 快 -> 选 CDN
3. 服务端快很多 -> 提线程重测后选服务端
4. CDN 略慢但在 ratio 内 -> 仍选 CDN
5. CDN 极慢 -> 触发提线程重测 -> 重测后仍慢则选服务端；重测翻盘时下载真的用提升后的线程数
6. source-mode: cdn / server 强制模式；prefer-cdn 别名兼容
7. 影子测速：不动用户任何真实文件、无临时文件残留（含进程被强杀的场景）
8. 缓存生效：第二次同步不再测速

用法::

    python tests/verify_cdn_select.py [--java <java.exe>] [--jar <AutoSync-1.0.0.jar>] [--only 场景名]

只用标准库。
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
JAR_NAME = "AutoSync-1.0.0.jar"
DEFAULT_JAR = PROJECT / "build" / "libs" / JAR_NAME
DEFAULT_JAVA = Path(os.path.expandvars(r"%USERPROFILE%\.jdks\ms-17.0.19\bin\java.exe"))

# 样本文件（>= speedtest-min-size-mb 默认 10MB），同时是本次待下载的最大文件
BIG_SIZE = 24 * 1024 * 1024
MID_SIZE = 2 * 1024 * 1024
SMALL_SIZE = 64 * 1024
SPEEDTEST_BIN_SIZE = 256 * 1024

FAILURES: list[tuple[str, str]] = []


# ----------------------------------------------------------------------
# 断言与工具
# ----------------------------------------------------------------------
def check(condition: bool, label: str, detail: str = "") -> bool:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  ->  {detail}" if detail else ""))
    if not condition:
        FAILURES.append((label, detail))
    return condition


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def payload(size: int, seed: int) -> bytes:
    block = bytes((i * 7 + seed * 13 + 3) % 251 for i in range(65536))
    reps, rem = divmod(size, len(block))
    return block * reps + block[:rem]


def decode_log(path: Path) -> str:
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("gbk", errors="replace")


def snapshot(root: Path, skip_dirs: tuple[str, ...] = (".modsync-temp",)) -> dict[str, tuple]:
    """给游戏目录里所有真实文件做一次 (大小, mtime, sha256) 快照。

    跳过两处客户端自己的状态文件（不算「用户文件」）：
    - ``.modsync-temp/``：下载/测速用的临时目录
    - ``version-label.txt``：客户端每次同步成功都会写的版本号标记
    """
    result: dict[str, tuple] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(rel == d or rel.startswith(d + "/") for d in skip_dirs):
            continue
        if rel in ("version-label.txt", "modsync/source-choice.json", "modsync/source-stats.json"):
            continue
        stat = path.stat()
        result[rel] = (stat.st_size, stat.st_mtime_ns, sha256_file(path))
    return result


def diff_snapshots(before: dict, after: dict) -> tuple[list[str], list[str], list[str]]:
    added = [k for k in after if k not in before]
    removed = [k for k in before if k not in after]
    changed = [k for k in before if k in after and before[k] != after[k]]
    return added, removed, changed


def find_residue(root: Path) -> list[str]:
    """找出游戏目录里所有疑似测速临时文件的残留。"""
    hits = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".modsync-temp/") or rel.endswith(".seg"):
            hits.append(rel)
    return hits


# ----------------------------------------------------------------------
# 假 MSFP 服务（PING / SIZE / GET，按连接限速）
# ----------------------------------------------------------------------
class MsfpHandler(socketserver.StreamRequestHandler):
    disable_nagle_algorithm = True
    rbufsize = -1
    wbufsize = 0

    def handle(self) -> None:
        self.server.track(1)
        try:
            while True:
                line = self.rfile.readline(8192)
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not text:
                    continue
                verb, _, rest = text.partition(" ")
                try:
                    if not self._dispatch(verb, rest):
                        return
                except OSError:
                    return
        finally:
            self.server.track(-1)

    def _dispatch(self, verb: str, rest: str) -> bool:
        if verb == "PING":
            self._send(b"OK 0\n")
            return True
        if verb == "SIZE":
            target = self._resolve(rest)
            if target is None:
                self._send(b"ERR not found\n")
            else:
                self._send(f"OK {target.stat().st_size}\n".encode())
            return True
        if verb == "GET":
            return self._handle_get(rest)
        self._send(b"ERR bad request\n")
        return False

    def _handle_get(self, rest: str) -> bool:
        parts = rest.split(" ", 2)
        if len(parts) < 3 or not parts[0].isdigit() or not (parts[1] == "-1" or parts[1].isdigit()):
            self._send(b"ERR bad request\n")
            return True
        start, end, raw_path = int(parts[0]), int(parts[1]), parts[2]

        target = self._resolve(raw_path)
        if target is None:
            self._send(b"ERR not found\n")
            return True

        if raw_path in getattr(self.server, "fail_get", ()):
            # 模拟服务端读这个文件时出错：SIZE 仍然正常，GET 失败
            self._send(b"ERR internal test failure\n")
            return True

        size = target.stat().st_size
        if size == 0 or start >= size:
            self._send(b"OK 0\n")
            return True

        last = size - 1 if end == -1 else min(end, size - 1)
        length = last - start + 1

        self._send(f"OK {length}\n".encode())

        rate = float(getattr(self.server, "rate", 0) or 0)
        burst = 4096
        began = time.monotonic()
        sent = 0

        try:
            with open(target, "rb") as fp:
                fp.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fp.read(min(4096, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent += len(chunk)
                    remaining -= len(chunk)
                    self.server.add_bytes(len(chunk))
                    if rate > 0:
                        target_time = began + max(0, sent - burst) / rate
                        delay = target_time - time.monotonic()
                        if delay > 0:
                            time.sleep(delay)
        except OSError:
            return False
        return True

    def _resolve(self, raw_path: str) -> Path | None:
        parts = [p for p in raw_path.replace("\\", "/").split("/") if p not in ("", ".")]
        if not parts or ".." in parts:
            return None
        candidate = self.server.dist.joinpath(*parts)
        return candidate if candidate.is_file() else None

    def _send(self, data: bytes) -> None:
        self.wfile.write(data)


class ThrottledMsfpServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256

    def __init__(self, dist: Path, rate: float) -> None:
        self.dist = Path(dist)
        self.rate = rate
        self.fail_get: set[str] = set()
        self.bytes_sent = 0
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), MsfpHandler)
        self.port = int(self.server_address[1])

    def handle_error(self, request, client_address) -> None:  # noqa: D102
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)

    def track(self, delta: int) -> None:
        with self.lock:
            self.active += delta
            self.max_active = max(self.max_active, self.active)

    def add_bytes(self, count: int) -> None:
        with self.lock:
            self.bytes_sent += count

    def reset_stats(self) -> None:
        with self.lock:
            self.bytes_sent = 0
            self.max_active = 0


# ----------------------------------------------------------------------
# 假 CDN（HTTP/1.1 + Range，按连接限速）
# ----------------------------------------------------------------------
class CdnHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True

    def log_message(self, *args) -> None:  # noqa: D102 - 静音
        pass

    def do_GET(self) -> None:  # noqa: N802
        raw_path = urllib.parse.unquote(self.path.split("?", 1)[0]).lstrip("/")
        parts = [p for p in raw_path.split("/") if p not in ("", ".")]
        target = self.server.dist.joinpath(*parts) if parts and ".." not in parts else None

        # 模拟「这个文件 CDN 上根本没有」：直接 404
        if raw_path in getattr(self.server, "missing_paths", ()):
            target = None

        if target is None or not target.is_file():
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        size = target.stat().st_size
        start, end, partial = 0, size - 1, False
        rng = self.headers.get("Range")

        if rng and rng.startswith("bytes="):
            spec = rng[len("bytes=") :]
            first, _, second = spec.partition("-")
            if first.isdigit():
                start = int(first)
            if second.isdigit():
                end = int(second)
            end = min(end, size - 1)
            partial = True
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        rate = float(getattr(self.server, "rate", 0) or 0)
        burst = 4096
        began = time.monotonic()
        sent = 0

        try:
            with open(target, "rb") as fp:
                fp.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fp.read(min(4096, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent += len(chunk)
                    remaining -= len(chunk)
                    self.server.add_bytes(len(chunk))
                    if rate > 0:
                        target_time = began + max(0, sent - burst) / rate
                        delay = target_time - time.monotonic()
                        if delay > 0:
                            time.sleep(delay)
        except OSError:
            return


class ThrottledCdnServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256

    def __init__(self, dist: Path, rate: float) -> None:
        self.dist = Path(dist)
        self.rate = rate
        self.missing_paths: set[str] = set()
        self.bytes_sent = 0
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), CdnHandler)
        self.port = int(self.server_address[1])

    def process_request_thread(self, request, client_address):  # type: ignore[override]
        self.track(1)
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.track(-1)

    def handle_error(self, request, client_address) -> None:  # noqa: D102
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)

    def track(self, delta: int) -> None:
        with self.lock:
            self.active += delta
            self.max_active = max(self.max_active, self.active)

    def add_bytes(self, count: int) -> None:
        with self.lock:
            self.bytes_sent += count

    def reset_stats(self) -> None:
        with self.lock:
            self.bytes_sent = 0
            self.max_active = 0


# ----------------------------------------------------------------------
# 工作区
# ----------------------------------------------------------------------
class Workspace:
    def __init__(self, root: Path, jar: Path, cdn_port: int) -> None:
        self.root = root
        self.prog = root / "prog"
        self.game = root / "game"
        self.dist = root / "dist"
        self.cdn_port = cdn_port
        self.client_log = root / "client.log"

        for directory in (self.prog, self.game, self.dist):
            directory.mkdir(parents=True, exist_ok=True)

        (self.game / ".minecraft").mkdir(exist_ok=True)
        shutil.copy2(jar, self.prog / JAR_NAME)

    # ---------------- 分发目录 ----------------
    def write_dist(self, version: str = "v1") -> None:
        files = self.dist / "files" / "mods"
        files.mkdir(parents=True, exist_ok=True)

        (self.dist / "files" / "mods" / "big.jar").write_bytes(payload(BIG_SIZE, 1))
        (self.dist / "files" / "mods" / "mid.jar").write_bytes(payload(MID_SIZE, 2))
        (self.dist / "files" / "mods" / "small.jar").write_bytes(payload(SMALL_SIZE, 3))
        (self.dist / "speedtest.bin").write_bytes(payload(SPEEDTEST_BIN_SIZE, 9))

        self.write_manifest(version)

    def write_manifest(self, version: str = "v1") -> None:
        entries = []
        for rel in ("files/mods/big.jar", "files/mods/mid.jar", "files/mods/small.jar"):
            path = self.dist / rel
            entries.append(
                {
                    "path": rel[len("files/") :],
                    "size": path.stat().st_size,
                    "sha256": sha256_file(path),
                    "urls": [rel, f"http://127.0.0.1:{self.cdn_port}/{rel}"],
                }
            )

        manifest = {"format": 1, "version": version, "files": entries, "deletes": []}
        (self.dist / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")

    def seed_game_with_dist(self) -> None:
        """把分发目录里的文件按清单路径原样放进游戏目录（模拟「已经是最新」）。"""
        mods = self.game / "mods"
        mods.mkdir(parents=True, exist_ok=True)
        for name in ("big.jar", "mid.jar", "small.jar"):
            shutil.copy2(self.dist / "files" / "mods" / name, mods / name)

    def reset_game(self) -> None:
        """清空游戏目录里的更新产物（保留 .minecraft 目录），用来在同一个工作区里跑第二次全量同步。"""
        for path in sorted(self.game.rglob("*"), reverse=True):
            if path.name == ".minecraft" and path.is_dir():
                continue
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass

    # ---------------- 配置 ----------------
    def write_config(self, msfp_port: int, **overrides) -> None:
        config: dict[str, object] = {
            "urls": [f"msfp://127.0.0.1:{msfp_port}"],
            "source-mode": "auto",
            "speedtest-min-size-mb": 10,
            "speedtest-duration-ms": 2000,
            "cdn-prefer-ratio": 0.5,
            "cdn-max-threads": 64,
            "download-threads": 32,
            "concurrent-files": 2,
            "auto-select-source": False,
            "detect-mod-conflicts": False,
            "allow-error": False,
            "show-no-update-message": False,
            "show-has-update-message": False,
        }
        config.update(overrides)

        lines = []
        for key, value in config.items():
            if value is None:
                continue
            if isinstance(value, bool):
                lines.append(f"{key}: {'true' if value else 'false'}")
            elif isinstance(value, list):
                lines.append(f"{key}:")
                for item in value:
                    lines.append(f"  - {item}")
            elif isinstance(value, str):
                lines.append(f"{key}: {value}")
            else:
                lines.append(f"{key}: {value}")

        (self.prog / "mcpatch.yml").write_text("\n".join(lines) + "\n", "utf-8")


def run_client(ws: Workspace, java: Path, timeout: float = 240.0) -> tuple[int, str]:
    """跑一次客户端（windowless），返回退出码与日志文本。"""
    with open(ws.client_log, "wb") as log:
        proc = subprocess.Popen(
            [
                str(java),
                "-Dfile.encoding=UTF-8",
                "-Dsun.stdout.encoding=UTF-8",
                "-Dsun.stderr.encoding=UTF-8",
                "-jar",
                str(ws.prog / JAR_NAME),
                "windowless",
            ],
            cwd=str(ws.game),
            stdout=log,
            stderr=subprocess.STDOUT,
        )

    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(30)
        code = -999

    return code, decode_log(ws.client_log)


def run_client_and_kill(ws: Workspace, java: Path, marker: str, wait_after: float, timeout: float = 120.0) -> tuple[bool, str]:
    """跑到日志里出现 marker 之后再等 wait_after 秒，然后强杀进程（模拟断电/被杀）。"""
    with open(ws.client_log, "wb") as log:
        proc = subprocess.Popen(
            [
                str(java),
                "-Dfile.encoding=UTF-8",
                "-Dsun.stdout.encoding=UTF-8",
                "-Dsun.stderr.encoding=UTF-8",
                "-jar",
                str(ws.prog / JAR_NAME),
                "windowless",
            ],
            cwd=str(ws.game),
            stdout=log,
            stderr=subprocess.STDOUT,
        )

    deadline = time.time() + timeout
    seen = False

    while time.time() < deadline:
        if proc.poll() is not None:
            break
        if marker in decode_log(ws.client_log):
            seen = True
            break
        time.sleep(0.2)

    if seen:
        time.sleep(wait_after)

    proc.kill()
    proc.wait(30)

    return seen, decode_log(ws.client_log)


# ----------------------------------------------------------------------
# 场景
# ----------------------------------------------------------------------
def scenario_auto_cdn_fast(ws, java, msfp, cdn):
    print("\n== 场景 1：CDN 明显更快 -> 应选 CDN")
    cdn.rate, msfp.rate = 0.12 * 1024 * 1024, 0.03 * 1024 * 1024
    ws.write_config(msfp.port, **{"speedtest-duration-ms": 1500})
    code, log = run_client(ws, java)

    check(code == 0, "客户端正常退出", f"exit={code}")
    check("[测速] 样本 big.jar" in log and "本次待下载" in log, "测速样本取自本次待下载列表",
          first_line(log, "[测速] 样本"))
    match = re.search(r"\[测速\] CDN ([\d.]+) MB/s\s+\|\s+服务端 ([\d.]+) MB/s\s+->\s+选 CDN", log)
    check(match is not None, "判定为「选 CDN」", match.group(0) if match else first_match(r"\[测速\] CDN [\d.]+ MB/s.*", log))
    check("提升线程" not in log, "CDN 够快，没有触发提线程重测")
    check("[CDN] big.jar 从 127.0.0.1 下载成功" in log, "big.jar 真的走了 CDN")
    check(not re.search(r"\[服务端\] big\.jar（本次测速选了服务端", log), "没有把 big.jar 判给服务端")
    check((ws.game / "mods" / "big.jar").is_file()
          and sha256_file(ws.game / "mods" / "big.jar") == sha256_file(ws.dist / "files" / "mods" / "big.jar"),
          "big.jar 内容与清单一致")
    check(not find_residue(ws.game), "没有临时文件残留", str(find_residue(ws.game)))
    print(f"  [info] 假 CDN 发送 {cdn.bytes_sent / 1048576:.2f} MB（峰值并发 {cdn.max_active}）；"
          f"假 MSFP 发送 {msfp.bytes_sent / 1048576:.2f} MB（峰值并发 {msfp.max_active}）"
          f"  限速 CDN={cdn.rate / 1024:.0f} KB/s MSFP={msfp.rate / 1024:.0f} KB/s")
    return log


def scenario_auto_server_fast(ws, java, msfp, cdn):
    print("\n== 场景 2：服务端快很多 -> 提线程重测后仍选服务端")
    cdn.rate, msfp.rate = 0.02 * 1024 * 1024, 0.15 * 1024 * 1024
    ws.write_config(msfp.port, **{"speedtest-duration-ms": 1500})
    code, log = run_client(ws, java)

    check(code == 0, "客户端正常退出", f"exit={code}")
    check(re.search(r"->\s+CDN 太慢，提升线程到 64 重测", log) is not None,
          "触发提线程到 64 重测", first_match(r"\[测速\] CDN [\d.]+ MB/s.*", log))
    check(re.search(r"\[测速\] 重测 CDN [\d.]+ MB/s\s+\|\s+服务端 [\d.]+ MB/s\s+->\s+选服务端", log) is not None,
          "重测后仍选服务端", first_match(r"\[测速\] 重测 CDN [\d.]+ MB/s.*", log))
    check("[服务端] big.jar（本次测速选了服务端，跳过 CDN）" in log, "每个文件都跳过 CDN 直接走服务端")
    check("[CDN] big.jar 从 127.0.0.1 下载成功" not in log, "没有从 CDN 下载 big.jar")
    check(sha256_file(ws.game / "mods" / "big.jar") == sha256_file(ws.dist / "files" / "mods" / "big.jar"),
          "big.jar 内容与清单一致")
    check(not find_residue(ws.game), "没有临时文件残留", str(find_residue(ws.game)))
    return log


def scenario_auto_cdn_slightly_slower(ws, java, msfp, cdn):
    print("\n== 场景 3：CDN 略慢但在 ratio 内 -> 仍选 CDN")
    cdn.rate, msfp.rate = 0.10 * 1024 * 1024, 0.15 * 1024 * 1024
    ws.write_config(msfp.port, **{"speedtest-duration-ms": 1500})
    code, log = run_client(ws, java)

    check(code == 0, "客户端正常退出", f"exit={code}")
    match = re.search(r"\[测速\] CDN ([\d.]+) MB/s\s+\|\s+服务端 ([\d.]+) MB/s\s+->\s+选 CDN（达服务端 (\d+)%）", log)
    check(match is not None, "CDN 慢于服务端但仍在 ratio 内，选了 CDN（并打出百分比）",
          match.group(0) if match else first_match(r"\[测速\] CDN [\d.]+ MB/s.*", log))
    check("提升线程" not in log, "既然判给了 CDN，就不该再提线程重测")
    check("[CDN] big.jar 从 127.0.0.1 下载成功" in log, "big.jar 真的走了 CDN")
    check(not find_residue(ws.game), "没有临时文件残留", str(find_residue(ws.game)))
    return log


def scenario_retest_flips_to_cdn(ws, java, msfp, cdn):
    print("\n== 场景 4：CDN 初始太慢 -> 提线程重测翻盘 -> 选 CDN（且下载用 64 线程）")
    cdn.rate, msfp.rate = 0.06 * 1024 * 1024, 0.15 * 1024 * 1024
    ws.write_config(msfp.port, **{"speedtest-duration-ms": 1500})
    code, log = run_client(ws, java)

    check(code == 0, "客户端正常退出", f"exit={code}")
    check(re.search(r"->\s+CDN 太慢，提升线程到 64 重测", log) is not None, "先触发提线程重测")
    check(re.search(r"\[测速\] 重测 CDN [\d.]+ MB/s\s+\|\s+服务端 [\d.]+ MB/s\s+->\s+选 CDN", log) is not None,
          "重测后翻盘选了 CDN", first_match(r"\[测速\] 重测 CDN [\d.]+ MB/s.*", log))
    check("启用多线程分块下载（CDN）：big.jar" in log and "分段数 64" in log,
          "真实下载使用了提升后的 64 条分段连接", first_line(log, "启用多线程分块下载（CDN）：big.jar"))
    check(cdn.max_active >= 60, "CDN 服务端确实观察到了 60+ 并发连接", f"max_active={cdn.max_active}")
    check(sha256_file(ws.game / "mods" / "big.jar") == sha256_file(ws.dist / "files" / "mods" / "big.jar"),
          "big.jar 内容与清单一致")
    check(not find_residue(ws.game), "没有临时文件残留", str(find_residue(ws.game)))
    return log


def scenario_source_mode_cdn(ws, java, msfp, cdn):
    print("\n== 场景 5：source-mode: cdn 强制 CDN（不测速）")
    cdn.rate, msfp.rate = 0.4 * 1024 * 1024, 2.0 * 1024 * 1024
    ws.write_config(msfp.port, **{"source-mode": "cdn"})
    code, log = run_client(ws, java)

    check(code == 0, "客户端正常退出", f"exit={code}")
    check("强制使用 CDN，跳过测速" in log, "声明了强制 CDN、跳过测速")
    check("[测速] CDN " not in log and "[测速] 重测" not in log, "确实没有做任何测速")
    check("[CDN] big.jar 从 127.0.0.1 下载成功" in log, "即使服务端更快也走 CDN")
    check(sha256_file(ws.game / "mods" / "big.jar") == sha256_file(ws.dist / "files" / "mods" / "big.jar"),
          "big.jar 内容与清单一致")
    return log


def scenario_source_mode_server(ws, java, msfp, cdn):
    print("\n== 场景 6：source-mode: server 强制服务端（不测速、不碰 CDN）")
    cdn.rate, msfp.rate = 4.0 * 1024 * 1024, 0.5 * 1024 * 1024
    ws.write_config(msfp.port, **{"source-mode": "server"})
    cdn.reset_stats()
    code, log = run_client(ws, java)

    check(code == 0, "客户端正常退出", f"exit={code}")
    check("强制使用服务端，跳过测速" in log, "声明了强制服务端、跳过测速")
    check("[测速] CDN " not in log and "[测速] 重测" not in log, "确实没有做任何测速")
    check("[CDN] " not in log, "完全没有碰 CDN")
    check(cdn.bytes_sent == 0, "假 CDN 一个字节都没被请求", f"bytes={cdn.bytes_sent}")
    check("[服务端] big.jar（source-mode: server，跳过 CDN）" in log, "每个文件都标明了跳过 CDN")
    check(sha256_file(ws.game / "mods" / "big.jar") == sha256_file(ws.dist / "files" / "mods" / "big.jar"),
          "big.jar 内容与清单一致")
    return log


def scenario_prefer_cdn_alias(ws, java, msfp, cdn):
    print("\n== 场景 7：prefer-cdn 别名兼容（false -> server，true -> cdn）")
    cdn.rate, msfp.rate = 0.4 * 1024 * 1024, 2.0 * 1024 * 1024

    ws.write_config(msfp.port, **{"source-mode": None, "prefer-cdn": False})
    cdn.reset_stats()
    code, log = run_client(ws, java)
    check(code == 0, "prefer-cdn: false 客户端正常退出", f"exit={code}")
    check("source-mode: server（来自 prefer-cdn（旧写法，等价于 source-mode: server））" in log,
          "prefer-cdn: false 被解释成 source-mode: server", first_line(log, "source-mode"))
    check("[测速] CDN " not in log, "prefer-cdn: false 不做测速")
    check(cdn.bytes_sent == 0, "prefer-cdn: false 完全不碰 CDN", f"bytes={cdn.bytes_sent}")

    ws.reset_game()
    ws.write_config(msfp.port, **{"source-mode": None, "prefer-cdn": True})
    code, log = run_client(ws, java)
    check(code == 0, "prefer-cdn: true 客户端正常退出", f"exit={code}")
    check("source-mode: cdn（来自 prefer-cdn（旧写法，等价于 source-mode: cdn））" in log,
          "prefer-cdn: true 被解释成 source-mode: cdn", first_line(log, "source-mode"))
    check("[CDN] big.jar 从 127.0.0.1 下载成功" in log, "prefer-cdn: true 走 CDN")

    ws.reset_game()
    ws.write_config(msfp.port, **{"source-mode": "server", "prefer-cdn": True})
    code, log = run_client(ws, java)
    check(code == 0, "同时写 source-mode 与 prefer-cdn 时客户端正常退出", f"exit={code}")
    check("source-mode: server（来自 source-mode）" in log, "同时出现时 source-mode 优先")
    return log


def scenario_shadow_and_cache(ws, java, msfp, cdn):
    print("\n== 场景 8：影子测速（不动真实文件、无残留）+ 缓存生效")
    cdn.rate, msfp.rate = 0.3 * 1024 * 1024, 0.1 * 1024 * 1024

    ws.seed_game_with_dist()
    ws.write_config(msfp.port, **{"speedtest-duration-ms": 1500})

    # 只让很小的 small.jar 过期：大文件都已是最新，于是没有「待下载的大文件」，只能走影子样本
    small = ws.dist / "files" / "mods" / "small.jar"
    small.write_bytes(payload(SMALL_SIZE, 77))
    ws.write_manifest("v2")

    before = snapshot(ws.game)
    big_before = before["mods/big.jar"]

    code, log = run_client(ws, java)

    check(code == 0, "客户端正常退出", f"exit={code}")
    check(re.search(r"\[测速\] 样本 big\.jar（[\d.]+ MB，本地已有）", log) is not None,
          "样本是本地已有的大文件（影子样本）", first_line(log, "[测速] 样本"))
    check("[测速] 影子样本（本地已有，仅下载临时片段，不影响原文件）" in log, "打出了影子测速说明")

    after = snapshot(ws.game)
    added, removed, changed = diff_snapshots(before, after)

    check("mods/big.jar" not in changed and "mods/big.jar" not in removed,
          "影子样本 big.jar 没有被改动、也没有被删除", f"changed={changed} removed={removed}")
    check(before["mods/big.jar"] == after["mods/big.jar"], "big.jar 的大小/mtime/sha256 完全没变",
          f"{big_before[:2]} -> {after['mods/big.jar'][:2]}")
    check(changed == ["mods/small.jar"], "唯一变化的只有本次真正要更新的 small.jar", f"changed={changed}")
    check(added == [] and removed == [], "没有多余文件被新增或删除", f"added={added} removed={removed}")
    check(not find_residue(ws.game), "测速临时目录已被删除，无任何残留", str(find_residue(ws.game)))
    check((ws.game / "mods" / "small.jar").read_bytes() == small.read_bytes(), "small.jar 更新成功")

    # ---------------- 缓存 ----------------
    print("\n== 场景 9：缓存生效，第二次同步不再测速")
    small.write_bytes(payload(SMALL_SIZE, 88))
    ws.write_manifest("v3")

    before2 = snapshot(ws.game)
    code2, log2 = run_client(ws, java)

    check(code2 == 0, "第二次同步正常退出", f"exit={code2}")
    check("命中测速缓存" in log2, "第二次同步命中了测速缓存", first_line(log2, "[测速] 命中"))
    check("[测速] 样本" not in log2, "第二次同步完全没有再挑样本测速")
    check("[CDN] small.jar 从 127.0.0.1 下载成功" in log2, "缓存里的结论（CDN）被真正用上了")

    after2 = snapshot(ws.game)
    added2, removed2, changed2 = diff_snapshots(before2, after2)
    check(changed2 == ["mods/small.jar"] and added2 == [] and removed2 == [],
          "第二次同步也只改了该改的那个文件", f"changed={changed2}")
    check(before2["mods/big.jar"] == after2["mods/big.jar"], "big.jar 依然纹丝不动")
    check(not find_residue(ws.game), "第二次同步后依然没有任何残留", str(find_residue(ws.game)))
    return log2


def scenario_interrupt(ws, java, msfp, cdn):
    print("\n== 场景 10：测速过程中进程被强杀 -> 不留用户文件损伤、残留可被下次同步清掉")
    cdn.rate, msfp.rate = 0.01 * 1024 * 1024, 0.01 * 1024 * 1024

    ws.seed_game_with_dist()
    ws.write_config(msfp.port, **{"speedtest-duration-ms": 15000})

    # 让 small.jar 过期，保证本次真的有文件要下（否则根本不会测速）
    small = ws.dist / "files" / "mods" / "small.jar"
    small.write_bytes(payload(SMALL_SIZE, 55))
    ws.write_manifest("v2")

    before = snapshot(ws.game)

    seen, log = run_client_and_kill(ws, java, "[测速] CDN 与 服务端 并发测速", wait_after=3.0)

    check(seen, "看到了「并发测速」日志（说明确实是在测速阶段被杀）")

    mid_files = [p.relative_to(ws.game).as_posix() for p in ws.game.rglob("*") if p.is_file()]
    residue = [f for f in mid_files if f.startswith(".modsync-temp/")]
    outside = [f for f in mid_files if not f.startswith(".modsync-temp/") and f not in before]

    check(len(residue) > 0, "强杀后确实留下了测速临时文件（测速目录里）", f"{len(residue)} 个：{residue[:3]}")
    check(outside == [], "除了 .modsync-temp 之外没有任何新增文件", str(outside))

    after = snapshot(ws.game)
    added, removed, changed = diff_snapshots(before, after)
    check(added == [] and removed == [] and changed == [],
          "强杀没有改动/删除任何用户文件（含影子样本 big.jar）", f"added={added} removed={removed} changed={changed}")

    # 下次同步开始时必须先把残留清掉
    cdn.rate, msfp.rate = 0.3 * 1024 * 1024, 0.1 * 1024 * 1024
    ws.write_config(msfp.port)
    code, log2 = run_client(ws, java)

    check(code == 0, "恢复后的同步正常退出", f"exit={code}")
    check(not find_residue(ws.game), "上次强杀留下的测速临时文件已被清理", str(find_residue(ws.game)))
    check(not (ws.game / ".modsync-temp").exists(), ".modsync-temp 整个目录都干净了")
    return log2


def scenario_both_sources_fail(ws, java, msfp, cdn):
    print("\n== 场景 11：两个来源都测不出速度 -> 回退到原有行为（先 CDN 后服务端）")
    cdn.rate, msfp.rate = 0.3 * 1024 * 1024, 0.1 * 1024 * 1024

    ws.seed_game_with_dist()
    ws.write_config(msfp.port, **{"speedtest-duration-ms": 1500})

    # 让影子样本 big.jar 在两个来源上都取不到数据：CDN 上 404、服务端 GET 报错（SIZE 仍然正常）
    cdn.missing_paths = {"files/mods/big.jar"}
    msfp.fail_get = {"files/mods/big.jar"}

    try:
        small = ws.dist / "files" / "mods" / "small.jar"
        small.write_bytes(payload(SMALL_SIZE, 99))
        ws.write_manifest("v2")

        code, log = run_client(ws, java)

        check(code == 0, "客户端正常退出（回退路径没有把同步搞挂）", f"exit={code}")
        check("CDN 与 服务端 都没有测出速度，回退到原有行为（先 CDN 后服务端）" in log,
              "打出了「两源都测不出 -> 回退原有行为」", first_line(log, "[测速]"))
        check(re.search(r"\[测速\] 样本 big\.jar（[\d.]+ MB，本地已有）", log) is not None, "样本仍是影子样本")
        check("[CDN] small.jar 从 127.0.0.1 下载成功" in log, "回退后依旧先试 CDN（改动前的行为）")
        check(not (ws.game / "modsync" / "source-choice.json").exists(), "没有把失败结果写进缓存")
        check(not find_residue(ws.game), "没有临时文件残留", str(find_residue(ws.game)))
        check((ws.game / "mods" / "small.jar").read_bytes() == small.read_bytes(), "该更新的文件照常更新成功")
    finally:
        cdn.missing_paths = set()
        msfp.fail_get = set()

    return log


def first_line(log: str, needle: str) -> str:
    for line in log.splitlines():
        if needle in line:
            return line.strip()
    return "（日志里没有这一行）"


def first_match(pattern: str, log: str) -> str:
    match = re.search(pattern, log)
    return match.group(0).strip() if match else "（日志里没有匹配行）"


# ----------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="AutoSync CDN/服务端自动选源验收")
    parser.add_argument("--java", default=str(DEFAULT_JAVA))
    parser.add_argument("--jar", default=str(DEFAULT_JAR))
    parser.add_argument("--only", default="", help="只跑名字里包含该子串的场景")
    parser.add_argument("--keep", action="store_true", help="保留临时工作目录")
    args = parser.parse_args()

    java, jar = Path(args.java), Path(args.jar)

    print(f"java : {java}")
    print(f"jar  : {jar}")

    if not check(jar.is_file(), "构建产物存在且是 AutoSync-1.0.0.jar", f"{jar.name} {jar.stat().st_size if jar.is_file() else 0} 字节"):
        return 1
    check(java.is_file(), "java 可执行文件存在", str(java))

    tmp_root = Path(tempfile.mkdtemp(prefix="autosync-cdn-select-"))
    print(f"工作目录: {tmp_root}")

    cdn = ThrottledCdnServer(tmp_root / "_cdn", 0)
    msfp = ThrottledMsfpServer(tmp_root / "_msfp", 0)

    cdn_thread = threading.Thread(target=cdn.serve_forever, daemon=True)
    msfp_thread = threading.Thread(target=msfp.serve_forever, daemon=True)
    cdn_thread.start()
    msfp_thread.start()

    print(f"假 CDN  : http://127.0.0.1:{cdn.port}")
    print(f"假 MSFP : msfp://127.0.0.1:{msfp.port}")

    scenarios = [
        ("auto-cdn-fast", scenario_auto_cdn_fast),
        ("auto-server-fast", scenario_auto_server_fast),
        ("auto-cdn-slightly-slower", scenario_auto_cdn_slightly_slower),
        ("retest-flips", scenario_retest_flips_to_cdn),
        ("source-mode-cdn", scenario_source_mode_cdn),
        ("source-mode-server", scenario_source_mode_server),
        ("prefer-cdn-alias", scenario_prefer_cdn_alias),
        ("shadow-and-cache", scenario_shadow_and_cache),
        ("interrupt", scenario_interrupt),
        ("both-sources-fail", scenario_both_sources_fail),
    ]

    try:
        for index, (name, runner) in enumerate(scenarios):
            if args.only and args.only not in name:
                continue

            ws_root = tmp_root / f"{index:02d}-{name}"
            ws = Workspace(ws_root, jar, cdn.port)

            # 每个场景独立的目录，共享同一对假服务（端口固定 -> 缓存签名稳定）
            cdn.dist = ws.dist
            msfp.dist = ws.dist
            ws.write_dist()
            cdn.reset_stats()
            msfp.reset_stats()

            try:
                runner(ws, java, msfp, cdn)
            except Exception as exc:  # noqa: BLE001
                import traceback

                traceback.print_exc()
                check(False, f"场景 {name} 执行时抛异常", repr(exc))
    finally:
        cdn.shutdown()
        msfp.shutdown()
        cdn.server_close()
        msfp.server_close()

        if not args.keep:
            shutil.rmtree(tmp_root, ignore_errors=True)
        else:
            print(f"保留工作目录：{tmp_root}")

    print()
    if FAILURES:
        print(f"结果：{len(FAILURES)} 项失败")
        for label, detail in FAILURES:
            print(f"  - {label}  {detail}")
        return 1

    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
