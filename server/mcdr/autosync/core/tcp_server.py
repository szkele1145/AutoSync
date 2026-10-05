"""MSFP (AutoSync File Transfer Protocol) v1 —— 裸 TCP 文件分发服务。

协议见 ``PROTOCOL.md``。之所以不用 HTTP：服务器在阿里云中国大陆节点，未备案域名会被
备案拦截系统按 Host 头识别并返回 403（``Server: Beaver``），而裸 TCP 不受影响。

请求（UTF-8 行，``\\n`` 结尾，路径放行末因此可含空格/中文/方括号）::

    GET <start> <end> <path>\\n     # 闭区间偏移，end=-1 表示到文件末尾
    PING\\n                          # -> OK 0\\n
    SIZE <path>\\n                   # -> OK <size>\\n

响应::

    OK <length>\\n + <length> 字节原始数据
    ERR <message>\\n                # not found / forbidden / bad request / internal <detail>

其他要求：单连接 keep-alive（串行，不流水线）、支持并发连接（客户端每块一个连接）、
空闲 30 秒断开、必须做路径规范化拒绝逃逸（``..`` / 绝对路径 / 符号链接逃逸）。
"""

from __future__ import annotations

import re
import socket
import socketserver
import threading
import time
import traceback
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "PROTOCOL_NAME",
    "RequestError",
    "port_in_use",
    "resolve_candidates",
    "AutoSyncTCPHandler",
    "AutoSyncTCPServer",
    "AutoSyncTCPService",
]

PROTOCOL_NAME = "MSFP/1"
MAX_LINE = 8192
IDLE_TIMEOUT = 30.0
READ_CHUNK = 256 * 1024
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class RequestError(Exception):
    """协议层错误，``message`` 会原样写进 ``ERR <message>``。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def port_in_use(host: str, port: int, timeout: float = 0.5) -> bool:
    """探测端口是否已被监听。

    为什么需要：Windows 的 ``SO_REUSEADDR`` 语义与 Linux 不同，允许**抢占**已监听端口。
    若别的进程已在 127.0.0.1:8123 监听，本服务绑定 0.0.0.0:8123 会"成功"，但 127.0.0.1
    的连接仍被先绑定的进程接走 —— 表现为"服务正常但内容不对"。所以先做一次连接探测。
    """
    if port <= 0:
        return False
    probe_hosts = ["127.0.0.1"]
    if host and host not in ("0.0.0.0", "::", "127.0.0.1", "localhost"):
        probe_hosts.append(host)
    for probe_host in probe_hosts:
        try:
            with socket.create_connection((probe_host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def resolve_candidates(
    dist_dir: Path,
    raw_path: str,
    path_prefix: str = "",
) -> List[Path]:
    """把协议路径解析为候选的真实路径（第一个是字面量，第二个可能做了百分号解码）。

    :raise RequestError: ``bad request`` / ``forbidden``
    """
    raw = raw_path
    if not raw:
        raise RequestError("bad request")
    if "\x00" in raw or "\\" in raw:
        raise RequestError("forbidden")

    prefix = (path_prefix or "").strip("/")
    if prefix and (raw == prefix or raw.startswith(prefix + "/")):
        # 兼容 HTTP 风格的 files/mods/x.jar 清单
        raw = raw[len(prefix) :].lstrip("/")

    if raw.startswith("/"):
        raise RequestError("forbidden")
    if _DRIVE_RE.match(raw):
        raise RequestError("forbidden")

    parts: List[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise RequestError("forbidden")
        if ":" in part:
            raise RequestError("forbidden")
        parts.append(part)
    if not parts:
        raise RequestError("forbidden")

    root = Path(dist_dir)
    try:
        root_resolved = root.resolve(strict=False)
    except OSError:
        raise RequestError("internal dist dir unavailable") from None

    candidates: List[Path] = [root.joinpath(*parts)]
    if "%" in raw:
        decoded = urllib.parse.unquote(raw)
        if decoded != raw:
            decoded_parts = [p for p in decoded.split("/") if p not in ("", ".")]
            if decoded_parts and ".." not in decoded_parts:
                candidates.append(root.joinpath(*decoded_parts))

    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=False)
        except OSError:
            continue
        # 双保险：规范化后必须仍在分发目录内（同时挡住符号链接/junction 逃逸）
        if resolved != root_resolved and not resolved.is_relative_to(root_resolved):
            raise RequestError("forbidden")
    return candidates


class AutoSyncTCPHandler(socketserver.StreamRequestHandler):
    """一个连接一个线程；串行处理 keep-alive 请求。"""

    disable_nagle_algorithm = True  # 小包（PING/SIZE）和一问一答模式必须关 Nagle
    rbufsize = -1
    wbufsize = 0  # wfile 直写 socket（SocketIO.write -> sendall）

    # ------------------------------------------------------------------ 生命周期
    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(getattr(self.server, "idle_timeout", IDLE_TIMEOUT))
        self._bump("connections")
        self._bump("active_connections")
        logger = getattr(self.server, "autosync_logger", None)
        if logger is not None:
            logger.debug("MSFP 连接建立：%s", self.client_address)

    def finish(self) -> None:
        self._bump("active_connections", -1)
        logger = getattr(self.server, "autosync_logger", None)
        if logger is not None:
            logger.debug("MSFP 连接关闭：%s", self.client_address)
        try:
            super().finish()
        except OSError:
            pass

    def handle(self) -> None:
        while True:
            try:
                raw = self.rfile.readline(MAX_LINE + 1)
            except (socket.timeout, TimeoutError):
                self._bump("idle_timeouts")
                return
            except OSError:
                return
            if not raw:
                return  # 客户端正常关闭
            if len(raw) > MAX_LINE or not raw.endswith(b"\n"):
                # 超长或半截请求：无法安全地继续解析这一行
                self._send_err("bad request")
                return
            line = raw[:-1]
            if line.endswith(b"\r"):
                line = line[:-1]
            if not line.strip():
                continue  # 容忍 keep-alive 之间的空行
            if not self._dispatch(line):
                return

    # ------------------------------------------------------------------ 分发
    def _dispatch(self, line: bytes) -> bool:
        """:return: False 表示处理完后关闭连接。"""
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            self._send_err("bad request")
            return False

        verb, _, rest = text.partition(" ")

        if verb == "PING":
            if rest.strip():
                self._send_err("bad request")
                return True
            self._bump("pings")
            self._ok_header(0)
            return True

        if verb == "SIZE":
            if not rest:
                self._send_err("bad request")
                return True
            try:
                target = self._find_file(rest)
                size = target.stat().st_size
            except RequestError as exc:
                self._send_err(exc.message)
                return True
            except OSError:
                self._send_err("internal stat failed")
                return True
            self._bump("sizes")
            self._ok_header(size)
            return True

        if verb == "GET":
            return self._handle_get(rest)

        # 兼容性要求：不是 GET/PING/SIZE 开头的连接 -> ERR bad request 并关闭
        self._send_err("bad request")
        return False

    def _handle_get(self, rest: str) -> bool:
        fields = rest.split(" ", 2)
        if len(fields) < 3:
            self._send_err("bad request")
            return True
        start_text, end_text, path = fields

        if not start_text.isdigit():
            self._send_err("bad request")
            return True
        if not (end_text == "-1" or end_text.isdigit()):
            self._send_err("bad request")
            return True
        start = int(start_text)
        end = int(end_text)

        try:
            target = self._find_file(path)
            size = target.stat().st_size
        except RequestError as exc:
            self._send_err(exc.message)
            return True
        except OSError:
            self._send_err("internal stat failed")
            return True

        # 空文件 / start 超出末尾：回 OK 0（不传数据），保持连接可继续
        if size == 0 or start >= size:
            self._bump("gets")
            self._ok_header(0)
            return True

        last = size - 1 if end == -1 else min(end, size - 1)
        if last < start:
            self._send_err("bad request")
            return True
        length = last - start + 1

        self._bump("gets")
        if start != 0 or length != size:
            self._bump("ranged_gets")
        self._ok_header(length)
        try:
            with open(target, "rb") as fp:
                fp.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fp.read(min(READ_CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
                    self._bump("bytes_sent", len(chunk))
        except OSError as exc:
            # 已经发出了 OK 头，无法再改成 ERR；只能断开让客户端重试
            logger = getattr(self.server, "autosync_logger", None)
            if logger is not None:
                logger.warning("MSFP 读取 %s 失败：%r，断开连接", target, exc)
            return False
        if remaining > 0:
            logger = getattr(self.server, "autosync_logger", None)
            if logger is not None:
                logger.warning(
                    "MSFP %s 只读到 %d/%d 字节（文件可能正在被替换），断开连接",
                    target,
                    length - remaining,
                    length,
                )
            return False
        return True

    # ------------------------------------------------------------------ 辅助
    def _find_file(self, raw_path: str) -> Path:
        candidates = resolve_candidates(
            Path(self.server.dist_dir),  # type: ignore[attr-defined]
            raw_path,
            getattr(self.server, "path_prefix", ""),
        )
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
        self._bump("errors_not_found")
        raise RequestError("not found")

    def _ok_header(self, length: int) -> None:
        self._send_line(f"OK {length}")

    def _send_line(self, text: str) -> None:
        self.wfile.write(text.encode("utf-8") + b"\n")

    def _send_err(self, message: str) -> None:
        key = message.split(" ", 1)[0]
        self._bump("errors_" + key.replace("-", "_"))
        self._send_line("ERR " + message)

    def _bump(self, key: str, amount: int = 1) -> None:
        bumper = getattr(self.server, "bump", None)
        if callable(bumper):
            bumper(key, amount)


class AutoSyncTCPServer(socketserver.ThreadingTCPServer):
    """多线程 TCP 服务器（客户端每块一个连接，必须并发）。"""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(
        self,
        address: Tuple[str, int],
        dist_dir: Path,
        logger: Any = None,
        path_prefix: str = "",
        idle_timeout: float = IDLE_TIMEOUT,
    ) -> None:
        self.dist_dir = Path(dist_dir)
        self.autosync_logger = logger
        self.path_prefix = path_prefix
        self.idle_timeout = float(idle_timeout)
        self.stats: Dict[str, Any] = {
            "connections": 0,
            "active_connections": 0,
            "pings": 0,
            "sizes": 0,
            "gets": 0,
            "ranged_gets": 0,
            "bytes_sent": 0,
            "idle_timeouts": 0,
            "errors_not_found": 0,
            "errors_forbidden": 0,
            "errors_bad": 0,
            "errors_internal": 0,
        }
        super().__init__(address, AutoSyncTCPHandler)

    def bump(self, key: str, amount: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + amount

    def handle_error(self, request: Any, client_address: Any) -> None:
        logger = getattr(self, "autosync_logger", None)
        if logger is not None:
            logger.debug("MSFP 连接异常 %s：\n%s", client_address, traceback.format_exc())


class AutoSyncTCPService:
    """在独立线程里 ``serve_forever()`` 的 MSFP 服务封装。"""

    def __init__(
        self,
        dist_dir: Path,
        host: str = "0.0.0.0",
        port: int = 8123,
        logger: Any = None,
        path_prefix: str = "",
        idle_timeout: float = IDLE_TIMEOUT,
    ) -> None:
        self.dist_dir = Path(dist_dir)
        self.host = host
        self.port = int(port)
        self.logger = logger
        self.path_prefix = path_prefix
        self.idle_timeout = float(idle_timeout)
        self._server: Optional[AutoSyncTCPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self.last_error: str = ""
        self.started_at: float = 0.0

    # ------------------------------------------------------------------ 属性
    @property
    def running(self) -> bool:
        return self._server is not None and self._thread is not None and self._thread.is_alive()

    @property
    def stats(self) -> Dict[str, Any]:
        if self._server is None:
            return {}
        return dict(self._server.stats)

    def endpoint(self) -> str:
        host = self.host if self.host not in ("0.0.0.0", "::") else "127.0.0.1"
        return f"{host}:{self.port}"

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> bool:
        with self._lock:
            if self.running:
                return True
            if port_in_use(self.host, self.port):
                self.last_error = f"端口 {self.port} 已被其他进程占用（{self.host}）"
                self._log(
                    "error",
                    f"MSFP 服务启动失败：{self.last_error}。请更换 tcp_port，或先停止占用该端口的进程。",
                )
                self._server = None
                self._thread = None
                return False
            try:
                server = AutoSyncTCPServer(
                    (self.host, self.port),
                    dist_dir=self.dist_dir,
                    logger=self.logger,
                    path_prefix=self.path_prefix,
                    idle_timeout=self.idle_timeout,
                )
            except OSError as exc:
                self.last_error = str(exc)
                self._log("error", f"MSFP 服务启动失败（{self.host}:{self.port}）：{exc}")
                self._server = None
                self._thread = None
                return False
            self._server = server
            self.port = int(server.server_address[1])  # port=0 时回填真实端口
            self.started_at = time.time()
            thread = threading.Thread(target=server.serve_forever, name="AutoSync-MSFP", daemon=True)
            self._thread = thread
            thread.start()
            self.last_error = ""
            self._log("info", f"MSFP 服务已启动：{self.endpoint()}（目录 {self.dist_dir}）")
            return True

    def stop(self) -> None:
        with self._lock:
            server, thread = self._server, self._thread
            self._server = None
            self._thread = None
            if server is not None:
                try:
                    server.shutdown()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    server.server_close()
                except Exception:  # noqa: BLE001
                    pass
            if thread is not None and thread.is_alive():
                thread.join(timeout=5.0)
            if server is not None:
                self._log("info", "MSFP 服务已停止")

    def restart(self) -> bool:
        self.stop()
        return self.start()

    def _log(self, level: str, message: str) -> None:
        if self.logger is None:
            return
        getattr(self.logger, level, self.logger.info)(message)
