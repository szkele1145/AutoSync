"""检查 .mcdr 包（本质是 zip）的结构是否正确。

检查项：
1. zip 能被解析、CRC 全通过；
2. 包内路径使用 **正斜杠**（zip 规范；Windows 的反斜杠会让部分解压器建出怪目录）；
3. 必需文件齐全：``mcdreforged.plugin.json`` / ``autosync_mcdr/entry.py`` /
   ``autosync/__init__.py`` 及全部核心模块；
4. ``mcdreforged.plugin.json`` 的 ``id`` / ``version`` / ``entrypoint`` 与实际包结构一致；
5. 没有 ``__pycache__`` / ``*.pyc``。

用法::

    python tools/check-mcdr-package.py                       # 默认检查 server/mcdr/AutoSync-1.0.0.mcdr
    python tools/check-mcdr-package.py path/to/xxx.mcdr
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from typing import List

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
DEFAULT_PACKAGE = REPO_ROOT / "server" / "mcdr" / "AutoSync-1.0.0.mcdr"
#: 独立版与 MCDR 版共用的核心模块（少一个都说明同步漏了）
CORE_MODULES = (
    "__init__.py",
    "__main__.py",
    "builder.py",
    "classify.py",
    "config.py",
    "curseforge.py",
    "deps.py",
    "deps_fix.py",
    "manifest.py",
    "modrinth.py",
    "scanner.py",
    "shell.py",
    "speedtest.py",
    "tcp_client.py",
    "tcp_server.py",
    "theme.py",
)

FAILURES: List[str] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    print("  [{}] {}{}".format("PASS" if condition else "FAIL", label, " -> {}".format(detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


def main(argv: List[str]) -> int:
    package = Path(argv[1]) if len(argv) > 1 else DEFAULT_PACKAGE
    print("== 检查 MCDR 包：{}".format(package))
    if not package.is_file():
        print("!! 文件不存在：{}".format(package))
        return 1
    check(True, "文件存在", "{} 字节".format(package.stat().st_size))

    with zipfile.ZipFile(package) as archive:
        names = archive.namelist()
        check(archive.testzip() is None, "zip CRC 全部通过")
        check(not any("\\" in name for name in names),
              "包内路径使用正斜杠", "反斜杠条目 {} 个".format(sum(1 for n in names if "\\" in n)))
        check(not any("__pycache__" in name or name.endswith((".pyc", ".pyo")) for name in names),
              "不含 __pycache__ / .pyc")

        required = ["mcdreforged.plugin.json", "autosync/__init__.py", "autosync/entry.py"]
        required += ["autosync/core/{}".format(name) for name in CORE_MODULES]
        for name in required:
            check(name in names, "包含 {}".format(name))

        metadata = json.loads(archive.read("mcdreforged.plugin.json").decode("utf-8"))
        check(metadata.get("id") == "autosync", "plugin.json id = autosync", str(metadata.get("id")))
        check(metadata.get("version") == "1.0.0", "plugin.json version = 1.0.0", str(metadata.get("version")))
        check(metadata.get("entrypoint") == "autosync.entry",
              "plugin.json entrypoint = autosync.entry", str(metadata.get("entrypoint")))
        check(metadata.get("name") == "AutoSync", "plugin.json name = AutoSync", str(metadata.get("name")))

        # 入口模块能编译（语法正确）
        entry_source = archive.read("autosync/entry.py").decode("utf-8")
        try:
            compile(entry_source, "autosync/entry.py", "exec")
            check(True, "autosync/entry.py 语法正确")
        except SyntaxError as exc:  # pragma: no cover
            check(False, "autosync/entry.py 语法正确", repr(exc))

        core_init = archive.read("autosync/core/__init__.py").decode("utf-8")
        check('__version__ = "1.0.0"' in core_init, "autosync/core/__init__.py 版本号 = 1.0.0")

    print()
    if FAILURES:
        print("结果：{} 项失败 -> {}".format(len(FAILURES), FAILURES))
        return 1
    print("结果：全部通过（{} 个条目）".format(len(names)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
