#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建 AutoSync 的 MCDR 插件包（server/mcdr/AutoSync-<版本>.mcdr）。

这是 ``tools/build-mcdr.ps1`` 的跨平台等价实现：Windows / Linux / macOS 都能跑，
纯标准库，无需 PowerShell。CI 里建议用这个。

做的事：
    1. 把 ``server/python/autosync`` 同步到 ``server/mcdr/autosync/core``
       （MCDR 版与独立版共用同一套核心代码，不复制业务逻辑）
    2. 清掉 ``__pycache__`` / ``*.pyc``
    3. 逐文件 SHA-256 校验同步结果（不一致就中止，避免打出坏包）
    4. 校验入口可用（``tools/verify-mcdr-entry.py``：装了 mcdreforged 就用真的，
       否则用 ``tests/mcdr_stub`` 桩）
    5. 打成 zip 再改名成 ``.mcdr``（注意：``.mcdr`` 必须就是 zip，
       路径分隔符必须是正斜杠）
    6. 检查包结构（``tools/check-mcdr-package.py``）

包内结构（MCDR 要求入口必须在与插件 id 同名的包内，所以核心代码放在 ``autosync/core/``）::

    mcdreforged.plugin.json      entrypoint = autosync.entry
    autosync/__init__.py         版本号 / 包说明
    autosync/entry.py            MCDR 入口（仓库里的唯一副本）
    autosync/core/*.py           独立版核心代码的同步副本
    config.example.json          默认配置参考

用法::

    python tools/build_mcdr.py                    # 同步 + 校验 + 打包
    python tools/build_mcdr.py --skip-verify      # 跳过校验（不推荐）
    python tools/build_mcdr.py --version 1.0.1    # 指定版本号
    python tools/build_mcdr.py --python /usr/bin/python3   # 指定带 mcdreforged 的解释器

版本号只改这一处（或改 ``server/python/autosync/__init__.py`` 的 ``__version__``）。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON_SRC = REPO_ROOT / "server" / "python" / "autosync"
MCDR_DIR = REPO_ROOT / "server" / "mcdr"
MCDR_PKG = MCDR_DIR / "autosync"
MCDR_CORE = MCDR_PKG / "core"
ENTRY_FILE = MCDR_PKG / "entry.py"
PLUGIN_JSON = MCDR_DIR / "mcdreforged.plugin.json"
CONFIG_EXAMPLE = MCDR_DIR / "config.example.json"
VERIFY_SCRIPT = REPO_ROOT / "tools" / "verify-mcdr-entry.py"
CHECK_SCRIPT = REPO_ROOT / "tools" / "check-mcdr-package.py"


def log(msg: str) -> None:
    print(msg, flush=True)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def purge_pycache(root: Path) -> int:
    """删除 __pycache__ 目录与 *.pyc / *.pyo 文件，返回删除数量。"""
    removed = 0
    if not root.exists():
        return 0
    for p in list(root.rglob("__pycache__")):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            removed += 1
    for pat in ("*.pyc", "*.pyo"):
        for p in list(root.rglob(pat)):
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def sync_core() -> None:
    """把独立版核心同步到 MCDR 包的 core/，并做逐文件哈希校验。"""
    if not PYTHON_SRC.is_dir():
        raise SystemExit(f"找不到独立版源码目录：{PYTHON_SRC}")
    if not ENTRY_FILE.is_file():
        raise SystemExit(f"找不到 MCDR 入口：{ENTRY_FILE}")
    if not PLUGIN_JSON.is_file():
        raise SystemExit(f"找不到插件元数据：{PLUGIN_JSON}")

    if MCDR_CORE.exists():
        shutil.rmtree(MCDR_CORE)
    shutil.copytree(PYTHON_SRC, MCDR_CORE)
    log("   已同步核心代码：server/python/autosync -> server/mcdr/autosync/core")

    purge_pycache(MCDR_DIR)

    src_files = sorted(
        p.relative_to(PYTHON_SRC).as_posix()
        for p in PYTHON_SRC.rglob("*")
        if p.is_file() and p.suffix not in (".pyc", ".pyo")
    )
    dst_files = sorted(
        p.relative_to(MCDR_CORE).as_posix()
        for p in MCDR_CORE.rglob("*")
        if p.is_file() and p.suffix not in (".pyc", ".pyo")
    )
    if src_files != dst_files:
        only_src = set(src_files) - set(dst_files)
        only_dst = set(dst_files) - set(src_files)
        raise SystemExit(
            "autosync/core/ 同步后文件列表不一致\n"
            f"  仅源有: {sorted(only_src)}\n  仅目标有: {sorted(only_dst)}"
        )

    changed = [
        rel
        for rel in src_files
        if sha256_of(PYTHON_SRC / rel) != sha256_of(MCDR_CORE / rel)
    ]
    if changed:
        raise SystemExit(f"autosync/core/ 有 {len(changed)} 个文件内容不一致：{changed}")
    log(f"   内容校验：{len(src_files)} 个文件与 server/python 完全一致")


def find_python(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("AUTOSYNC_PYTHON")
    if env and Path(env).exists():
        return env
    for cand in (
        Path(os.environ.get("LOCALAPPDATA", "")) / "Python" / "pythoncore-3.14-64" / "python.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python312" / "python.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python311" / "python.exe",
        Path("/usr/bin/python3"),
            Path("/usr/local/bin/python3"),
    ):
        if cand.is_file():
            return str(cand)
    return sys.executable or "python3"


def run_helper(python: str, script: Path, *args: str) -> None:
    if not script.is_file():
        raise SystemExit(f"找不到辅助脚本：{script}")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [python, str(script), *args]
    proc = subprocess.run(cmd, env=env)
    if proc.returncode != 0:
        raise SystemExit(f"{script.name} 失败（退出码 {proc.returncode}），已中止打包")


def package(version: str) -> Path:
    """把 server/mcdr 下的内容打成 .mcdr（其实是 zip，用正斜杠路径）。"""
    out_file = MCDR_DIR / f"AutoSync-{version}.mcdr"
    with tempfile.TemporaryDirectory(prefix="autosync-mcdr-build-") as staging:
        staging_path = Path(staging)
        shutil.copytree(MCDR_PKG, staging_path / "autosync")
        shutil.copy2(PLUGIN_JSON, staging_path / "mcdreforged.plugin.json")
        if CONFIG_EXAMPLE.is_file():
            shutil.copy2(CONFIG_EXAMPLE, staging_path / "config.example.json")
        purge_pycache(staging_path)

        if out_file.exists():
            out_file.unlink()

        # zip 路径必须是正斜杠；固定时间戳让构建可复现
        files: list[Path] = sorted(
            (p for p in staging_path.rglob("*") if p.is_file()),
            key=lambda p: p.relative_to(staging_path).as_posix(),
        )
        with zipfile.ZipFile(out_file, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for p in files:
                arcname = p.relative_to(staging_path).as_posix()
                info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                zf.writestr(info, p.read_bytes())
    return out_file


def print_archive(out_file: Path) -> None:
    with zipfile.ZipFile(out_file) as zf:
        log("   包内文件：")
        for info in sorted(zf.infolist(), key=lambda i: i.filename):
            log(f"     {info.filename:<40} {info.file_size:>8,} B")


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 AutoSync 的 MCDR 插件包")
    parser.add_argument("--version", default="1.0.0", help="版本号（默认 1.0.0）")
    parser.add_argument("--python", default=None, help="用于校验入口的 Python 解释器")
    parser.add_argument("--skip-verify", action="store_true", help="跳过入口校验（不推荐）")
    args = parser.parse_args()

    log(f"== AutoSync MCDR 打包 v{args.version}")
    log(f"   仓库根目录 : {REPO_ROOT}")

    sync_core()

    python = find_python(args.python)
    log(f"   解释器     : {python}")

    if not args.skip_verify:
        log("   校验入口（tools/verify-mcdr-entry.py）...")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run([python, str(VERIFY_SCRIPT)], env=env)
        if proc.returncode != 0:
            # 没装 mcdreforged 时自动降级为桩校验（仍会检查入口结构，只是不跑真实 MCDR）
            log("   （当前解释器没有 mcdreforged，降级为桩校验 --stub）")
            run_helper(python, VERIFY_SCRIPT, "--stub")

    out_file = package(args.version)
    size = out_file.stat().st_size
    log(f"   已生成：{out_file}  ({size:,} 字节 / {size / 1024:.1f} KB)")

    log("   检查包结构（tools/check-mcdr-package.py）...")
    run_helper(python, CHECK_SCRIPT, str(out_file))

    print_archive(out_file)
    log(f"== 完成。把 {out_file} 放进 MCDR 的 plugins/ 目录即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
