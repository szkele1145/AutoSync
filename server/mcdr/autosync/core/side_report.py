"""ModSideDetector 上报接收（MSFP ``REPORT`` 命令，纯标准库）。

背景
----
配套工具 ModSideDetector 会生成 ``side-report.json``。以前只能人工把文件拷到
``<dist_dir>/mods/side-report.json``，服务器在阿里云、工具在本机时很不方便。
这里在**现有 MSFP 协议**上加一条 ``REPORT`` 命令，让工具通过网络把报告推过来：

* **不新开端口、不引入 HTTP**：复用 ``tcp_host`` / ``tcp_port`` 与同一个 accept
  循环。（阿里云大陆节点会按 Host 头拦截 HTTP，只有裸 TCP 才通；而且协议文档已
  约定「第一字节不是 GET/PING/SIZE 开头的连接 -> ERR bad request」，``REPORT``
  以 ``R`` 开头，天然与老客户端不冲突。）
* 默认**关闭**；即使打开，``side_report_token`` 为空时也一律回 ``ERR disabled``，
  绝不允许「没配令牌就能往磁盘写文件」。
* 收到报告**不自动触发 classify**，只在日志里提示「下次 classify 将使用新报告」。

报文格式（详见 ``docs/MSFP协议.md``）::

    REPORT <token> <length>\\n
    <length 字节的 UTF-8 JSON（side-report.json 全文）>

    -> OK <count>\\n              count = 报告里 mods 数组的条目数
    -> ERR unauthorized\\n        令牌不匹配（hmac.compare_digest 比较）
    -> ERR disabled\\n            功能关闭 / 未配置令牌
    -> ERR too large\\n           length 超过上限（**读数据之前**就拒绝）
    -> ERR bad request\\n         格式错误 / JSON 不合法 / 顶层不是对象 / mods 不是数组
    -> ERR internal <detail>\\n   落盘失败
"""

from __future__ import annotations

import datetime
import hmac
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict

__all__ = [
    "MAX_BODY_BYTES",
    "token_matches",
    "resolve_side_report_path",
    "SideReportReceiver",
]

#: 报告 JSON 的字节上限（32 MB）。超限在**读数据之前**就回 ``ERR too large``。
MAX_BODY_BYTES = 32 * 1024 * 1024


def token_matches(provided: str, expected: str) -> bool:
    """恒定时间比较令牌。

    用 :func:`hmac.compare_digest` 而不是 ``==``：前者耗时与「前多少字节相同」
    无关，避免通过响应时间逐字节猜令牌（时序侧信道）。
    """
    if not provided or not expected:
        return False
    try:
        return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
    except (UnicodeError, TypeError):  # 极端输入（代理对等）一律视为不匹配
        return False


def resolve_side_report_path(config: Any, data_dir: Path) -> Path:
    """报告落盘路径。

    * ``side_report_path`` 留空 -> ``<data_dir>/side-report.json``
    * 相对路径 -> 相对 ``data_dir`` 解析
    * 绝对路径 -> 直接使用
    """
    raw = str(getattr(config, "side_report_path", "") or "").strip()
    base = Path(data_dir)
    if not raw:
        return base / "side-report.json"
    path = Path(os.path.expanduser(raw))
    return path if path.is_absolute() else (base / path)


def _now_iso() -> str:
    """本地时区的 ISO 时间字符串（秒级），用于「收到时间」。"""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


class SideReportReceiver:
    """上报接收器：开关/令牌判定、原子落盘、最近一次接收的状态。

    一个 AutoSync 实例一个；``config`` 变了由 :class:`autosync.builder.AutoSyncCore`
    重新构造，所以这里不做热更新。
    """

    def __init__(self, config: Any, data_dir: Path, logger: Any = None) -> None:
        self.config = config
        self.data_dir = Path(data_dir)
        self.logger = logger
        self.path = resolve_side_report_path(config, self.data_dir)
        #: 请求体上限（测试可以调小，避免真的构造 32MB 数据）
        self.max_body_bytes = MAX_BODY_BYTES
        self._lock = threading.Lock()
        #: 最近一次成功接收的状态（重启后为空；报告本身在磁盘上）
        self.received_at = ""
        self.mods = 0
        self.generated = ""

    # ---------------------------------------------------------------- 配置
    @property
    def enabled(self) -> bool:
        """``side_report_enabled`` 开关（默认 false）。"""
        return bool(getattr(self.config, "side_report_enabled", False))

    @property
    def token(self) -> str:
        """共享令牌（去掉首尾空白；内部不允许含空格）。"""
        return str(getattr(self.config, "side_report_token", "") or "").strip()

    @property
    def accepting(self) -> bool:
        """是否可以接收上报：开关打开**且**配了非空令牌。"""
        return self.enabled and bool(self.token)

    # ---------------------------------------------------------------- 落盘
    def save(self, payload: Dict[str, Any], source: str = "") -> Path:
        """原子写入报告并更新内存状态。

        「先写 ``.tmp`` 再 ``os.replace``」保证读者（classify）要么看到完整的旧文件、
        要么看到完整的新文件，绝不会读到写了一半的 JSON。
        """
        target = self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as fp:
                fp.write(text)
            os.replace(tmp, target)
        except OSError:
            # 失败时清掉半截临时文件（尽力而为，清不掉也不影响正确性）
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise
        mods = payload.get("mods")
        with self._lock:
            self.received_at = _now_iso()
            self.mods = len(mods) if isinstance(mods, list) else 0
            self.generated = str(payload.get("generated") or "")
        return target

    # ---------------------------------------------------------------- 日志
    def log(self, level: str, message: str) -> None:
        """按级别写日志；``logger`` 缺失时静默（测试里可以传 ``None``）。"""
        if self.logger is None:
            return
        method = getattr(self.logger, level, None) or getattr(self.logger, "info", None)
        if callable(method):
            method(message)

    # ---------------------------------------------------------------- 展示
    def status(self) -> Dict[str, Any]:
        """当前状态快照（给日志/排查用；不参与协议响应）。"""
        return {
            "enabled": self.enabled,
            "accepting": self.accepting,
            "has_token": bool(self.token),
            "path": str(self.path),
            "received_at": self.received_at,
            "mods": self.mods,
            "generated": self.generated,
            "max_body_bytes": self.max_body_bytes,
        }
