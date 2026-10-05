"""AutoSync 测试入口：``python tests/run_tests.py``（仅需标准库）。

跑两个模块：
* ``test_mcdr_entry``  —— MCDR 插件入口（用 ``tests/mcdr_stub`` 桩，不需要真装 mcdreforged）
* ``test_autosync``    —— 独立服务端核心（148 项）

``sys.path`` 说明：仓库里有两份同名的 ``autosync`` 包
（``server/python/autosync`` = 独立版，``server/mcdr/autosync`` = MCDR 版）。
两个测试模块各自把自己需要的那份插到 ``sys.path`` 最前面：
``test_mcdr_entry`` 优先 ``server/mcdr``（要 ``autosync.entry``），
``test_autosync`` 优先 ``server/python``（要独立版）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
MCDR_DIR = str(REPO_ROOT / "server" / "mcdr")

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import test_mcdr_entry  # noqa: E402


def _switch_to_standalone_core() -> None:
    """把环境从「MCDR 版 autosync」切回「独立版 autosync」。

    ``test_mcdr_entry`` 会把 ``server/mcdr`` 插到 ``sys.path`` 最前面（那里有带
    ``entry`` 的 ``autosync`` 包）；``test_autosync`` 需要 ``server/python`` 那份，
    所以这里先把 MCDR 路径摘掉、清掉已导入的 ``autosync`` 模块，
    再由 ``test_autosync`` 自己把 ``server/python`` 插到最前面。
    """
    while MCDR_DIR in sys.path:
        sys.path.remove(MCDR_DIR)
    for name in list(sys.modules):
        if name == "autosync" or name.startswith("autosync."):
            del sys.modules[name]


_switch_to_standalone_core()
import test_autosync  # noqa: E402

if __name__ == "__main__":
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite(
        [
            loader.loadTestsFromModule(test_mcdr_entry),
            loader.loadTestsFromModule(test_autosync),
        ]
    )
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
