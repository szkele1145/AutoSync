"""AutoSync MCDR 版入口的结构性测试（不需要真的安装 mcdreforged）。

入口模块是 ``autosync.entry``（MCDR 要求入口点在插件 id 同名的包内），
核心代码在 ``autosync.core``（与 server/python/autosync 同一份）。

覆盖点
------
1. 入口模块能导入（桩提供 ``mcdreforged.api``）；
2. ``on_load`` 能在 MCDR 上完成「读配置 -> 建核心 -> 注册命令」；
3. 命令树确实注册了 ``status`` / ``build`` / ``build refresh`` / ``reload`` /
   ``tcp start|stop|restart`` / ``classify`` / ``classify apply`` / ``deps`` /
   ``deps fix`` / ``deps fix <编号>`` / ``help``；
4. ``help`` 与 ``status`` 能产出非空输出，且帮助里的按钮是**真按钮**
   （``RText`` 带 ``clickEvent``，动作为 ``suggest_command``）；
5. 权限等级 2 的拦截生效（低权限玩家拿不到 ``build``）。

运行：``python tests/run_tests.py``（会连同 MCDR 用例一起跑），
或 ``python tests/test_mcdr_entry.py`` 单独跑。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
MCDR_DIR = REPO_ROOT / "server" / "mcdr"
PYTHON_DIR = REPO_ROOT / "server" / "python"

# 桩与 autosync 包都要能找到：HERE 提供 mcdr_stub，MCDR_DIR 提供带 entry 的 autosync 包。
# MCDR_DIR 必须排在 PYTHON_DIR 前面（两处都有 autosync 包）。
for candidate in (PYTHON_DIR, MCDR_DIR, HERE):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import mcdr_stub  # noqa: E402

mcdr_stub.install()

from mcdr_stub.types import FakeServer, FakeSource  # noqa: E402

from autosync import entry  # noqa: E402

EXPECTED_PATHS = (
    "!!autosync",
    "!!autosync help",
    "!!autosync status",
    "!!autosync build",
    "!!autosync build refresh",
    "!!autosync reload",
    "!!autosync tcp",
    "!!autosync tcp start",
    "!!autosync tcp stop",
    "!!autosync tcp restart",
    "!!autosync classify",
    "!!autosync classify apply",
    "!!autosync deps",
    "!!autosync deps fix",
    "!!autosync deps fix selection",
    "!!autosync deps fix apply",
    "!!as",
    "!!as status",
)


def make_fixture(permission: int = 4):
    """搭一套临时环境：临时 MCDR 根目录 + 分发目录 + 假命令源。"""
    root = Path(tempfile.mkdtemp(prefix="autosync-mcdr-test-"))
    server_root = root / "server"
    dist = server_root / "client-dist"
    (dist / "mods").mkdir(parents=True)
    (dist / "mods" / "demo.jar").write_bytes(b"PK\x03\x04demo")
    server = FakeServer(root=root, working_directory=server_root)
    source = FakeSource(server=server, permission=permission)
    return server, source, server_root, dist


class TestMcdrEntry(unittest.TestCase):
    def setUp(self) -> None:
        entry._core = None
        entry._config_path = None
        entry._data_dir = None
        self.server, self.source, self.server_root, self.dist = make_fixture()

    def tearDown(self) -> None:
        core = entry.get_core()
        if core is not None:
            try:
                core.stop_watcher()
                core.stop_tcp()
            except Exception:  # noqa: BLE001 - 测试清理不该失败
                pass
        entry._core = None

    # ------------------------------------------------------------------ 导入 / 加载
    def test_module_imports_without_real_mcdr(self) -> None:
        self.assertTrue(mcdr_stub.installed())
        self.assertEqual(entry.PREFIX, "!!autosync")
        self.assertEqual(entry.PLUGIN_ID, "autosync")
        self.assertEqual(entry.REQUIRE_HELPER_LEVEL, 2)

    def test_on_load_registers_every_command(self) -> None:
        entry.on_load(self.server, None)
        paths = set(self.server.registered_paths())
        for expected in EXPECTED_PATHS:
            self.assertIn(expected, paths, "缺少命令：{}".format(expected))
        self.assertTrue(self.server.help_messages, "应注册 !!autosync 帮助入口")

    def test_on_load_creates_config_and_core(self) -> None:
        entry.on_load(self.server, None)
        core = entry.get_core()
        self.assertIsNotNone(core, "on_load 后应存在核心对象")
        config_path = entry.get_config_path()
        self.assertIsNotNone(config_path)
        self.assertTrue(config_path.is_file(), "首次加载应生成 config.json：{}".format(config_path))
        # dist_dir 相对 server/ 解析
        self.assertEqual(Path(core.dist_dir), self.dist)
        # 数据目录落在插件配置目录下
        self.assertEqual(entry._data_dir, Path(self.server.get_data_folder()) / "autosync-data")

    def test_config_lands_in_plugin_data_folder(self) -> None:
        entry.on_load(self.server, None)
        self.assertEqual(entry.get_config_path(), Path(self.server.get_data_folder()) / "config.json")

    # ------------------------------------------------------------------ help
    def test_command_runs_help_without_arguments(self) -> None:
        entry.on_load(self.server, None)
        node = self.server.find_node("!!autosync")
        self.assertIsNotNone(node)
        node.handler(self.source)
        self.assertIn("AutoSync 帮助", self.source.text)
        self.assertIn("!!autosync build", self.source.text)

    def test_help_has_clickable_buttons(self) -> None:
        entry.on_load(self.server, None)
        self.source.lines.clear()
        entry._do_help(self.source)
        self.assertTrue(self.source.lines, "help 应有输出")
        clicks = []

        def collect(node) -> None:
            if getattr(node, "click", None):
                clicks.append(node.click)
            for child in getattr(node, "extra", []):
                collect(child)

        for line in self.source.lines:
            collect(line)
        self.assertTrue(clicks, "help 里的按钮应是带点击事件的 RText")
        actions = {action.name for action, _ in clicks}
        self.assertIn("suggest_command", actions)
        commands = {value for _, value in clicks}
        self.assertIn("!!autosync status", commands)
        for _, value in clicks:
            self.assertTrue(value.startswith("!!autosync ") or value == "!!autosync", value)

    def test_help_plain_text_has_no_control_chars(self) -> None:
        entry.on_load(self.server, None)
        entry._do_help(self.source)
        for line in self.source.plain_lines:
            self.assertNotIn("\x00", line)
            self.assertNotIn("\x01", line)
            self.assertNotIn("§", line, "回复里不应残留原始颜色代码：{!r}".format(line))

    # ------------------------------------------------------------------ status
    def test_status_produces_output(self) -> None:
        entry.on_load(self.server, None)
        entry.status_box(self.source)
        text = self.source.text
        self.assertIn("AutoSync", text)
        self.assertIn("分发目录", text)
        self.assertTrue(len(self.source.lines) >= 5, "status 应输出多行")

    def test_run_command_status_via_shell(self) -> None:
        entry.on_load(self.server, None)
        entry.run_command(self.source, "status")
        self.assertIn("AutoSync", self.source.text)

    def test_run_command_unknown_subcommand_reports_error(self) -> None:
        entry.on_load(self.server, None)
        entry.run_command(self.source, "not-a-command")
        self.assertIn("未知命令", self.source.text)

    # ------------------------------------------------------------------ 权限
    def test_build_denied_for_low_permission(self) -> None:
        entry.on_load(self.server, None)
        low = FakeSource(self.server, permission=0)
        entry._do_build(low, False)
        self.assertIn("权限不足", low.text)
        self.assertNotIn("开始构建", low.text)

    def test_classify_dry_run_allowed_for_low_permission(self) -> None:
        entry.on_load(self.server, None)
        low = FakeSource(self.server, permission=0)
        entry._do_classify(low, apply=False)
        # 干跑只读：不应被权限拦住（会真的跑一次分类，用空 mods 目录很快返回）
        self.assertNotIn("权限不足", low.text)

    def test_classify_apply_denied_for_low_permission(self) -> None:
        entry.on_load(self.server, None)
        low = FakeSource(self.server, permission=1)
        entry._do_classify(low, apply=True)
        self.assertIn("权限不足", low.text)

    # ------------------------------------------------------------------ deps fix 参数
    def test_deps_fix_rejects_garbage_selection(self) -> None:
        entry.on_load(self.server, None)
        entry._do_deps_fix(self.source, "rm -rf /")
        self.assertIn("编号格式不对", self.source.text)

    # ------------------------------------------------------------------ 生命周期
    def test_on_unload_is_safe_without_load(self) -> None:
        entry._core = None
        entry.on_unload(self.server)  # 不应抛异常

    def test_on_server_startup_is_safe(self) -> None:
        entry.on_load(self.server, None)
        entry.on_server_startup(self.server)  # 不应抛异常


if __name__ == "__main__":
    unittest.main(verbosity=2)
