"""``mcdreforged.api.types`` 的桩：``PluginServerInterface`` / ``CommandSource``。

这两个桩也是测试夹具（fixture）：``FakeServer`` 记录注册过的命令树与日志，
``FakeSource`` 把 ``reply`` 收到的每一行存进 ``lines`` 供断言。
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - 简单转发
        try:
            self.records.append(record.getMessage())
        except Exception:  # noqa: BLE001
            self.records.append("<格式化失败>")


class FakeServer:
    """``PluginServerInterface`` 的最小实现，够入口加载与命令注册用。"""

    def __init__(self, root: Optional[Path] = None, working_directory: Optional[Path] = None) -> None:
        self.logger = logging.getLogger("mcdr_stub.{}".format(id(self)))
        self.logger.handlers.clear()
        self.handler = _ListHandler()
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.root = Path(root) if root is not None else Path(tempfile.mkdtemp(prefix="mcdr-stub-"))
        self.working_directory = Path(working_directory) if working_directory is not None else self.root / "server"
        self.commands: List[Any] = []
        self.help_messages: List[tuple] = []
        self.data_folder = self.root / "config" / "autosync"

    # -- PluginServerInterface -------------------------------------------
    def get_data_folder(self) -> str:
        self.data_folder.mkdir(parents=True, exist_ok=True)
        return str(self.data_folder)

    def get_mcdr_config(self) -> Dict[str, Any]:
        return {"working_directory": str(self.working_directory)}

    def register_command(self, node: Any) -> None:
        self.commands.append(node)

    def register_help_message(self, prefix: str, message: str) -> None:
        self.help_messages.append((prefix, message))

    def is_server_running(self) -> bool:
        return False

    def say(self, text: Any) -> None:  # pragma: no cover - 测试不广播
        self.logger.info("say: %s", text)

    def load_config_simple(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:  # pragma: no cover
        return {}

    # -- 断言辅助 ---------------------------------------------------------
    @property
    def log_lines(self) -> List[str]:
        return list(self.handler.records)

    def registered_paths(self) -> List[str]:
        """把注册过的命令树拍平成 ``!!autosync deps fix`` 这样的路径列表。"""
        paths: List[str] = []

        def walk(node: Any, prefix: str) -> None:
            current = (prefix + " " + node.name).strip()
            paths.append(current)
            for child in node.children:
                walk(child, current)

        for root in self.commands:
            walk(root, "")
        return paths

    def find_node(self, path: str) -> Any:
        """按 ``!!autosync deps fix`` 找节点，找不到返回 ``None``。"""
        wanted = " ".join(path.split())

        def walk(node: Any, prefix: str) -> Any:
            current = (prefix + " " + node.name).strip()
            if current == wanted:
                return node
            for child in node.children:
                found = walk(child, current)
                if found is not None:
                    return found
            return None

        for root in self.commands:
            found = walk(root, "")
            if found is not None:
                return found
        return None


class FakeSource:
    """``CommandSource`` 的最小实现：``reply`` 收集行，``has_permission`` 可控。"""

    def __init__(self, server: FakeServer, permission: int = 4) -> None:
        self._server = server
        self.permission = int(permission)
        self.lines: List[Any] = []

    # -- CommandSource ----------------------------------------------------
    def reply(self, text: Any) -> None:
        self.lines.append(text)

    def get_server(self) -> FakeServer:
        return self._server

    def get_permission_level(self) -> int:
        return self.permission

    def has_permission(self, level: Any) -> bool:
        try:
            return self.permission >= int(level)
        except (TypeError, ValueError):  # pragma: no cover - 真实 MCDR 传的是枚举
            return True

    def is_console(self) -> bool:
        return False

    def is_player(self) -> bool:
        return True

    # -- 断言辅助 ---------------------------------------------------------
    @property
    def text(self) -> str:
        """所有回复拼成一段纯文本（RText 取 ``to_plain_text``）。"""
        parts = []
        for line in self.lines:
            parts.append(line.to_plain_text() if hasattr(line, "to_plain_text") else str(line))
        return "\n".join(parts)

    @property
    def plain_lines(self) -> List[str]:
        return [line.to_plain_text() if hasattr(line, "to_plain_text") else str(line) for line in self.lines]


class CommandSource:  # pragma: no cover - 仅用于类型标注
    pass


class PluginServerInterface:  # pragma: no cover - 仅用于类型标注
    pass
