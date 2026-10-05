"""用**真实 mcdreforged** 校验 AutoSync MCDR 入口（有真用真，没真用桩）。

做什么
------
1. 导入 ``autosync.entry``（走真实 ``mcdreforged.api``）；
2. 构造真实的 ``PluginServerInterface``（只需要一个最小的 ``AbstractPlugin`` 桩，
   不依赖 MCDR 服务端进程），调用 ``entry.on_load`` 完整跑一遍加载流程；
3. 检查命令树确实注册了全部子命令，并**真实执行** ``!!autosync``（= help）与
   ``!!autosync status``，断言产出的 ``RText`` 能被序列化成合法 JSON
   （即游戏内 tellraw 不会报错）；
4. 校验帮助里的每个按钮都带 ``suggest_command`` 点击事件与悬停说明。

用法::

    # 有真实 mcdreforged 的环境（推荐）
    python tools/verify-mcdr-entry.py

    # 只想在没装 MCDR 的机器上跑结构性检查
    python tools/verify-mcdr-entry.py --stub

退出码：0 = 全部通过；1 = 有失败项；2 = 需要真实 mcdreforged 但没有（未加 ``--stub``）。
"""

from __future__ import annotations

import argparse
import logging
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, List

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
MCDR_DIR = REPO_ROOT / "server" / "mcdr"
PYTHON_DIR = REPO_ROOT / "server" / "python"
TESTS_DIR = REPO_ROOT / "tests"

# ``server/mcdr`` 必须排在 ``server/python`` 前面：
# 两处都有名为 ``autosync`` 的包，这里要的是带 ``entry`` 的 MCDR 版。
for candidate in (str(TESTS_DIR), str(PYTHON_DIR), str(MCDR_DIR)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

FAILURES: List[str] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    print("  [{}] {}{}".format("PASS" if condition else "FAIL", label, " -> {}".format(detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


# --------------------------------------------------------------------------- 环境
def load_real_mcdr() -> bool:
    """尝试使用真实 mcdreforged；返回是否拿到真实库。"""
    try:
        import mcdreforged  # noqa: F401
    except ImportError:
        return False
    return not getattr(sys.modules["mcdreforged"], "__mcdr_stub__", False)


def make_real_server_interface(root: Path):
    """构造真实的 ``PluginServerInterface``：只需要最小 AbstractPlugin 桩。"""
    from mcdreforged.plugin.si.plugin_server_interface import PluginServerInterface

    class MinimalPlugin:
        """满足入口用到的 ``get_id`` / ``register_command`` / ``register_help_message``。"""

        def __init__(self) -> None:
            self.commands: List[Any] = []
            self.help_messages: List[tuple] = []

        def get_id(self) -> str:
            return "autosync"

        def get_name(self) -> str:
            return "AutoSync"

        def register_command(self, root_node: Any, allow_duplicates: bool = False) -> None:
            self.commands.append(root_node)

        def register_help_message(self, help_message: Any) -> None:
            # 真实 MCDR 传进来的是 HelpMessage 对象
            prefix = getattr(help_message, "prefix", None) or getattr(help_message, "literal", "")
            self.help_messages.append((prefix, help_message))

        def __str__(self) -> str:  # pragma: no cover - 调试用
            return "AutoSync-StubPlugin"

    class MinimalMcdrServer:
        """入口不碰 MCDR 服务端本体，给一个空壳即可。"""

        def __getattr__(self, item: str) -> Any:  # pragma: no cover - 防御性
            raise AttributeError(item)

    plugin = MinimalPlugin()
    interface = PluginServerInterface.__new__(PluginServerInterface)
    interface._BasicServerInterface__mcdr_server = MinimalMcdrServer()  # type: ignore[attr-defined]
    interface._PluginServerInterface__plugin = plugin  # type: ignore[attr-defined]
    interface._PluginServerInterface__logger_for_plugin = logging.getLogger("autosync.verify")  # type: ignore[attr-defined]
    # 数据目录：真实实现在 config/<plugin_id>/ 下创建，这里换成临时目录，避免污染 cwd
    interface.get_data_folder = lambda: _ensure_dir(root / "config" / "autosync")  # type: ignore[method-assign]
    interface.get_mcdr_config = lambda: {"working_directory": str(root / "server")}  # type: ignore[method-assign]
    interface.registered_paths = lambda: _command_paths(plugin.commands)  # type: ignore[attr-defined]
    interface.help_messages = plugin.help_messages  # type: ignore[attr-defined]
    return interface


def _command_paths(roots: List[Any]) -> List[str]:
    """把命令树拍平成 ``!!autosync deps fix`` 这样的路径列表。

    真实 MCDR 的 ``Literal`` 把名字放在 ``literals`` 集合里，
    参数节点（``GreedyText``）用 ``get_name()``；两种都兼容。
    """
    paths: List[str] = []

    def node_name(node: Any) -> str:
        literals = getattr(node, "literals", None)
        if literals:
            return sorted(literals)[0]
        getter = getattr(node, "get_name", None)
        if callable(getter):
            return str(getter())
        return str(getattr(node, "name", "?"))

    def walk(node: Any, prefix: str) -> None:
        current = (prefix + " " + node_name(node)).strip()
        paths.append(current)
        for child in node.get_children():
            walk(child, current)

    for root in roots:
        walk(root, "")
    return paths


def _ensure_dir(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def make_stub_server_interface(root: Path):
    """没有真实 MCDR 时退回 ``tests/mcdr_stub``。"""
    import mcdr_stub

    mcdr_stub.install()
    from mcdr_stub.types import FakeServer

    server = FakeServer(root=root, working_directory=root / "server")
    (root / "server" / "client-dist" / "mods").mkdir(parents=True, exist_ok=True)
    return server


# --------------------------------------------------------------------------- 校验
def collect_clicks(payload: Any, found: List[dict]) -> None:
    """从 ``to_json_object()`` 的结果里递归收集所有 ``clickEvent``。

    用序列化结果而不是对象内部字段，是因为真实 MCDR 把点击事件放在私有属性里
    （``_RText__click_event``），而 JSON 形状是跨版本稳定的：游戏内 tellraw 就是吃这棵树。
    """
    if isinstance(payload, dict):
        click = payload.get("clickEvent")
        if isinstance(click, dict):
            found.append(
                {
                    "action": str(click.get("action", "")),
                    "value": str(click.get("value", "")),
                    "hover": payload.get("hoverEvent"),
                    "text": str(payload.get("text", "")),
                }
            )
        for key in ("extra", "with", "hoverEvent"):
            collect_clicks(payload.get(key), found)
    elif isinstance(payload, list):
        for item in payload:
            collect_clicks(item, found)


def plain_text_of(node: Any) -> str:
    """取一行的纯文本（真实 RText / 桩 / 裸字符串都能处理）。"""
    if hasattr(node, "to_plain_text"):
        return node.to_plain_text()
    return str(node)


def json_of(node: Any) -> Any:
    if hasattr(node, "to_json_object"):
        return node.to_json_object()
    return str(node)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用真实 mcdreforged（或 tests/mcdr_stub）校验 AutoSync 入口")
    parser.add_argument("--stub", action="store_true", help="强制使用 tests/mcdr_stub，而不是真实 mcdreforged")
    args = parser.parse_args(argv)

    real = False if args.stub else load_real_mcdr()
    if not real and not args.stub:
        print("!! 当前 Python 环境没有安装 mcdreforged。")
        print("   请先 pip install mcdreforged，或加 --stub 只做结构性检查。")
        return 2

    if args.stub:
        print("== AutoSync MCDR 入口校验（桩模式，未使用真实 mcdreforged）")
    else:
        print("== AutoSync MCDR 入口校验（真实 mcdreforged / Python {}）".format(sys.version.split()[0]))

    root = Path(tempfile.mkdtemp(prefix="autosync-verify-"))
    (root / "server" / "client-dist" / "mods").mkdir(parents=True, exist_ok=True)
    (root / "server" / "client-dist" / "mods" / "demo.jar").write_bytes(b"PK\x03\x04demo")

    if real:
        server = make_real_server_interface(root)
    else:
        server = make_stub_server_interface(root)

    from autosync import entry

    # 1. on_load 全流程
    entry._core = None
    entry.on_load(server, None)
    check(entry.get_core() is not None, "on_load 建立核心对象")
    check(entry.get_config_path() is not None and Path(entry.get_config_path()).is_file(),
          "on_load 生成/读取 config.json", str(entry.get_config_path()))

    # 2. 命令树
    paths = set(server.registered_paths())
    expected = [
        "!!autosync status",
        "!!autosync build",
        "!!autosync build refresh",
        "!!autosync reload",
        "!!autosync tcp start",
        "!!autosync tcp stop",
        "!!autosync tcp restart",
        "!!autosync classify",
        "!!autosync classify apply",
        "!!autosync deps",
        "!!autosync deps fix",
        "!!autosync deps fix apply",
        "!!autosync help",
    ]
    for path in expected:
        check(path in paths, "命令已注册：{}".format(path))

    # 3. 真实执行 help / status，并序列化 RText
    source = _make_source(server, real)
    check(entry.run_command(source, "help") is True, "执行 !!autosync help")
    check(bool(source.lines), "help 有输出", "{} 行".format(len(source.lines)))
    help_text = "\n".join(plain_text_of(line) for line in source.lines)
    check("AutoSync 帮助" in help_text, "help 标题正确")
    check("!!autosync build refresh" in help_text, "help 列出 build refresh")
    check("!!autosync deps fix apply" in help_text, "help 列出 deps fix apply")

    clicks: List[dict] = []
    for line in source.lines:
        collect_clicks(json_of(line), clicks)
    check(bool(clicks), "help 里有可点击按钮", "{} 个".format(len(clicks)))
    actions = {item["action"] for item in clicks}
    check("suggest_command" in actions, "按钮动作为 suggest_command", str(sorted(actions)))
    commands = {item["value"] for item in clicks}
    check("!!autosync status" in commands, "按钮命令指向 !!autosync status")
    check(all(cmd.startswith("!!autosync") for cmd in commands), "按钮命令都是 !!autosync 前缀",
          "{} 个命令".format(len(commands)))
    check(all(item["hover"] is not None for item in clicks), "每个按钮都带悬停说明")
    check(all("点击后按回车执行" in json.dumps(item["hover"], ensure_ascii=False) for item in clicks),
          "悬停文案里提醒了「点击后按回车执行」")

    # 4. RText 序列化成合法 JSON（游戏内 tellraw 不会炸）
    serialized = [json_of(line) for line in source.lines]
    try:
        payload = json.dumps(serialized, ensure_ascii=False)
        check(True, "help 的 RText 可序列化为 JSON", "{} 字符".format(len(payload)))
    except (TypeError, ValueError) as exc:  # pragma: no cover
        check(False, "help 的 RText 可序列化为 JSON", repr(exc))

    source.lines.clear()
    entry.run_command(source, "status")
    check(bool(source.lines), "执行 !!autosync status 有输出", "{} 行".format(len(source.lines)))
    status_text = "\n".join(plain_text_of(line) for line in source.lines)
    check("AutoSync" in status_text, "status 含 AutoSync 标识")
    check("分发目录" in status_text, "status 含分发目录")
    for line in source.lines:
        json_of(line)  # 不抛异常即通过
    status_clicks: List[dict] = []
    for line in source.lines:
        collect_clicks(json_of(line), status_clicks)
    check(bool(status_clicks), "status 也带快捷按钮", "{} 个".format(len(status_clicks)))

    # 5. 权限拦截（只有真实 MCDR 的权限枚举才值得测；桩也顺便测一遍）
    low = _make_source(server, real, permission=0)
    entry._do_build(low, False)
    check("权限不足" in low.text, "低权限执行 build 被拦截")

    low2 = _make_source(server, real, permission=0)
    entry._do_classify(low2, False)
    check("权限不足" not in low2.text, "低权限仍可执行只读 classify")

    # 清理
    core = entry.get_core()
    if core is not None:
        try:
            core.stop_watcher()
            core.stop_tcp()
        except Exception:  # noqa: BLE001
            pass

    print()
    if FAILURES:
        print("结果：{} 项失败 -> {}".format(len(FAILURES), FAILURES))
        return 1
    print("结果：全部通过（{}{}）".format(
        "真实 mcdreforged" if real else "桩",
        "，{} 个可点按钮".format(len(clicks)) if clicks else "",
    ))
    return 0


def _make_source(server: Any, real: bool, permission: int = 4):
    """构造命令源。

    真实 MCDR 的 ``PlayerCommandSource`` 需要一套完整的连接/玩家对象，太重，
    这里用一个只实现入口用到的那几个方法的轻量替身（``reply`` / ``get_server`` /
    ``has_permission`` / ``get_permission_level``）——入口只依赖这几个方法。
    """
    if real:
        return AdapterSource(server, permission)
    from mcdr_stub.types import FakeSource

    return FakeSource(server, permission=permission)


class AdapterSource:
    """真实 MCDR 环境下的命令源替身：把 ``reply`` 收到的内容存起来供断言。"""

    def __init__(self, server: Any, permission: int = 4) -> None:
        self._server = server
        self.permission = int(permission)
        self.lines: List[Any] = []

    def reply(self, text: Any) -> None:
        self.lines.append(text)

    def get_server(self) -> Any:
        return self._server

    def get_permission_level(self) -> int:
        return self.permission

    def has_permission(self, level: Any) -> bool:
        try:
            return self.permission >= int(level)
        except (TypeError, ValueError):  # pragma: no cover - 真实枚举
            return self.permission >= 2

    def is_console(self) -> bool:
        return False

    def is_player(self) -> bool:
        return True

    def get_preference(self, key: str) -> Any:  # pragma: no cover - 入口不用
        return None

    @property
    def text(self) -> str:
        parts = []
        for line in self.lines:
            parts.append(line.to_plain_text() if hasattr(line, "to_plain_text") else str(line))
        return "\n".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())
