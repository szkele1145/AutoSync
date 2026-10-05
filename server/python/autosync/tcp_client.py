"""MSFP v1 客户端（仅标准库）。

服务端自检、验收脚本、以及将来写管理工具都用它。要点：

* ``OK <length>\\n`` 之后必须精确认读 ``length`` 字节 —— 所以自己维护接收缓冲，
  避免 ``socket.makefile()`` 把数据行之后的二进制体一起吃掉（这正是这类协议最容易踩的坑）；
* 多线程分块下载：每块一个独立连接，各自 seek 写入同一文件的不同区间。
"""

from __future__ import annotations

import hashlib
import socket
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

__all__ = ["MsfpError", "MsfpClient", "download_multithreaded", "DownloadReport"]

DEFAULT_TIMEOUT = 20.0
_READ_CHUNK = 256 * 1024


class MsfpError(Exception):
    """服务端返回 ``ERR <message>`` 或协议被破坏。"""


class MsfpClient:
    def __init__(self, host: str, port: int, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self._sock: Optional[socket.socket] = None
        self._buf = bytearray()

    # ------------------------------------------------------------------ 连接
    def connect(self) -> None:
        if self._sock is not None:
            return
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._sock.settimeout(self.timeout)
        self._buf.clear()

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
        self._buf.clear()

    def __enter__(self) -> "MsfpClient":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ 底层
    def _read_line(self) -> bytes:
        assert self._sock is not None
        while True:
            index = self._buf.find(b"\n")
            if index >= 0:
                line = bytes(self._buf[:index])
                del self._buf[: index + 1]
                return line
            if len(self._buf) > 65536:
                raise MsfpError("响应行过长，协议被破坏")
            chunk = self._sock.recv(_READ_CHUNK)
            if not chunk:
                raise MsfpError("连接在读到完整响应行之前被关闭")
            self._buf += chunk

    def _read_exact(self, length: int) -> bytes:
        assert self._sock is not None
        if length == 0:
            return b""
        out = bytearray()
        if self._buf:
            take = min(length, len(self._buf))
            out += self._buf[:take]
            del self._buf[:take]
        while len(out) < length:
            chunk = self._sock.recv(min(_READ_CHUNK, length - len(out)))
            if not chunk:
                raise MsfpError(f"数据体提前结束：期望 {length} 字节，实际 {len(out)}")
            out += chunk
        return bytes(out)

    def _request(self, line: str) -> bytes:
        """发送请求并返回响应行（已校验 OK/ERR）。"""
        self.connect()
        assert self._sock is not None
        self._sock.sendall(line.encode("utf-8") + b"\n")
        return self._read_response_header()

    def _read_response_header(self) -> bytes:
        head = self._read_line()
        if head.startswith(b"ERR "):
            raise MsfpError(head[4:].decode("utf-8", "replace"))
        if not head.startswith(b"OK "):
            raise MsfpError(f"非法响应行：{head[:80]!r}")
        return head[3:]

    # ------------------------------------------------------------------ 请求
    def ping(self) -> float:
        """返回 RTT 秒数。"""
        started = time.monotonic()
        payload = self._request("PING")
        if payload.strip() != b"0":
            raise MsfpError(f"PING 响应异常：{payload!r}")
        return time.monotonic() - started

    def size(self, path: str) -> int:
        payload = self._request(f"SIZE {path}")
        try:
            return int(payload.strip())
        except ValueError:
            raise MsfpError(f"SIZE 响应不是整数：{payload!r}") from None

    def get(self, path: str, start: int = 0, end: int = -1) -> bytes:
        """下载闭区间 ``[start, end]``；``end=-1`` 表示到文件末尾。"""
        self.connect()
        assert self._sock is not None
        self._sock.sendall(f"GET {start} {end} {path}\n".encode("utf-8"))
        payload = self._read_response_header()
        try:
            length = int(payload.strip())
        except ValueError:
            raise MsfpError(f"GET 响应头不是整数：{payload!r}") from None
        return self._read_exact(length)

    def send_raw(self, data: bytes) -> bytes:
        """直接发原始字节并读一行响应（用于测试非法请求）。"""
        self.connect()
        assert self._sock is not None
        self._sock.sendall(data)
        return self._read_line()

    # ------------------------------------------------------------------ 便捷
    def download_to(self, path: str, dest: Path, start: int = 0, end: int = -1) -> int:
        data = self.get(path, start, end)
        with open(dest, "r+b" if dest.exists() else "wb") as fp:
            fp.seek(start)
            fp.write(data)
        return len(data)


class DownloadReport:
    def __init__(self) -> None:
        self.size = 0
        self.chunks = 0
        self.threads = 0
        self.seconds = 0.0

    @property
    def speed_mbps(self) -> float:
        return self.size / 1024 / 1024 / self.seconds if self.seconds > 0 else 0.0


def download_multithreaded(
    host: str,
    port: int,
    path: str,
    dest: Path,
    size: int,
    threads: int = 8,
    chunk_size: int = 0,
    timeout: float = DEFAULT_TIMEOUT,
    progress: Optional[Callable[[int, int], None]] = None,
) -> DownloadReport:
    """每块一个独立连接并发下载，写入同一个文件的不同区间（客户端真实行为）。"""
    if size <= 0:
        raise ValueError("size 必须为正数")
    threads = max(1, min(int(threads), 64))
    if chunk_size <= 0:
        chunk_size = max(64 * 1024, size // (threads * 4))

    plan: List[Tuple[int, int]] = []
    start = 0
    while start < size:
        end = min(start + chunk_size - 1, size - 1)
        plan.append((start, end))
        start = end + 1

    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as fp:
        fp.truncate(size)

    report = DownloadReport()
    report.size = size
    report.chunks = len(plan)
    report.threads = threads
    errors: List[str] = []
    done_lock = threading.Lock()
    done_bytes = 0

    def worker(slices: List[Tuple[int, int]]) -> None:
        nonlocal done_bytes
        try:
            with MsfpClient(host, port, timeout=timeout) as client:
                for chunk_start, chunk_end in slices:
                    data = client.get(path, chunk_start, chunk_end)
                    expected = chunk_end - chunk_start + 1
                    if len(data) != expected:
                        errors.append(f"{chunk_start}-{chunk_end}: 长度 {len(data)} != {expected}")
                        return
                    with open(dest, "r+b") as fp:
                        fp.seek(chunk_start)
                        fp.write(data)
                    with done_lock:
                        done_bytes += len(data)
                        if progress is not None:
                            progress(done_bytes, size)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{slices[0][0]}-{slices[-1][1]}: {exc!r}")

    workers = []
    for index in range(threads):
        workers.append(threading.Thread(target=worker, args=(plan[index::threads],), daemon=True))
    started = time.monotonic()
    for worker_thread in workers:
        worker_thread.start()
    for worker_thread in workers:
        worker_thread.join(timeout=600)
    report.seconds = time.monotonic() - started
    if errors:
        raise MsfpError("; ".join(errors[:3]))
    return report


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fp:
        while True:
            chunk = fp.read(_READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
