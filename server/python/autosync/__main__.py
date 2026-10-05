"""AutoSync 命令行入口（独立程序，纯标准库，不依赖 mcdreforged）。

用法::

    python3 -m autosync --base <目录> shell                # 交互式 REPL（推荐）：启动即拉起 MSFP，再进提示符
    python3 -m autosync --base <目录> serve                # 只跑 MSFP 服务（常驻，Ctrl+C 退出）
    python3 -m autosync --base <目录> build                # 只构建后退出
    python3 -m autosync --base <目录> status [--json]
    python3 -m autosync --base <目录> check [--port 0] [--target mods/xxx.jar]
    python3 -m autosync --base <目录> classify [--refresh] [--json]
    python3 -m autosync --base <目录> classify-apply [--json]
    python3 -m autosync --base <目录> deps [--json]
    python3 -m autosync --base <目录> deps-fix [--json]
    python3 -m autosync --base <目录> deps-fix 1a 3a [--json]
    python3 -m autosync --base <目录> deps-fix apply [--json]

配置文件默认取**当前工作目录**下的 ``config.json``（``--config`` 可指定别的路径）；
文件不存在时**自动生成一份默认配置**（含 ``tcp_port=8123`` 在内的全部配置项）再启动。
终端输出走 ANSI 颜色，``NO_COLOR`` 环境变量存在或输出被重定向时自动去色。
``check`` 不依赖任何第三方库，也用于 CI/验收。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional

from . import __version__, theme
from .builder import AutoSyncCore
from .classify import ClassifyService
from .config import DEFAULT_CONFIG_NAME, AutoSyncConfig, ensure_config_file, load_config_file
from .deps import DependencyService
from .deps_fix import DepsFixService
from .manifest import load_manifest
from .shell import AutoSyncShell, build_result_lines, resolve_base_dir, start_tcp_background
from .tcp_client import MsfpClient, MsfpError, download_multithreaded, sha256_file
from .tcp_server import PROTOCOL_NAME, AutoSyncTCPService


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m autosync",
        description="AutoSync 独立运行入口（程序版本 {}）".format(__version__),
    )
    parser.add_argument("--version", action="version", version="AutoSync {}".format(__version__))
    parser.add_argument(
        "action",
        choices=[
            "shell",
            "build",
            "status",
            "serve",
            "check",
            "classify",
            "classify-apply",
            "deps",
            "deps-fix",
            "deps-fix-apply",
        ],
    )
    parser.add_argument("target", nargs="*", default=[], help="check: 额外校验的相对路径；deps-fix: 编号（1a 2a）或 apply")
    parser.add_argument("--base", default="", help="dist_dir 的相对路径基准目录（默认取配置 base_dir，其次当前目录）")
    parser.add_argument("--data", default="./autosync-data", help="状态/缓存目录")
    parser.add_argument("--config", default="", help="JSON 配置文件路径（默认 ./config.json；不存在则自动生成）")
    parser.add_argument("--dist", default="", help="覆盖 dist_dir")
    parser.add_argument("--port", type=int, default=0, help="覆盖 tcp_port（check 默认 0=随机端口）")
    parser.add_argument("--connections", type=int, default=8, help="check: 并发连接数")
    parser.add_argument("--refresh", action="store_true", help="忽略 Modrinth 缓存强制重查")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    parser.add_argument("--quiet", action="store_true", help="只输出错误日志")
    return parser


def _force_utf8_stdio() -> None:
    """Windows 控制台/管道默认用 GBK，中文文件名日志会变乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _make_logger(quiet: bool) -> logging.Logger:
    logger = logging.getLogger("autosync.cli")
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", "%H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING if quiet else logging.INFO)
    return logger


def _say(text) -> None:
    """终端输出一行：``§`` 代码转 ANSI（``NO_COLOR`` / 非 TTY 时自动去色）。"""
    print(theme.to_ansi(text))


def resolve_config_path(args: argparse.Namespace) -> Path:
    """``--config`` 指定的路径；没指定就是当前目录下的 ``config.json``。"""
    raw = str(args.config or "").strip() or DEFAULT_CONFIG_NAME
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (Path.cwd() / path)


def _make_config(args: argparse.Namespace, config_path: Path) -> AutoSyncConfig:
    cfg = load_config_file(config_path)
    if args.dist:
        cfg.dist_dir = args.dist
    if args.port:
        cfg.tcp_port = args.port
    cfg.normalize()
    return cfg


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    _force_utf8_stdio()
    logger = _make_logger(args.quiet)
    config_path = resolve_config_path(args)
    if ensure_config_file(config_path):
        logger.info("未找到配置文件，已生成默认配置：%s", config_path)
    cfg = _make_config(args, config_path)

    cwd = Path.cwd()
    if args.base:
        raw_base = Path(args.base).expanduser()
        base_dir = raw_base if raw_base.is_absolute() else cwd / raw_base
    else:
        base_dir = resolve_base_dir(cfg, cwd=cwd)
    base_dir = base_dir.resolve()
    data_dir = Path(args.data).expanduser()
    data_dir = data_dir if data_dir.is_absolute() else cwd / data_dir

    core = AutoSyncCore(cfg, data_dir=data_dir, base_dir=base_dir, logger=logger)

    if args.action == "shell":
        shell = AutoSyncShell(core=core, config_path=config_path, logger=logger)
        try:
            return shell.run(start=True)
        except KeyboardInterrupt:
            print()
            return 0
        finally:
            shell.shutdown()

    if args.action == "build":
        result = core.build(refresh_modrinth=args.refresh)
        if args.json:
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        else:
            for line in build_result_lines(result):
                _say(line)
        if result.errors and not args.json:
            for err in result.errors:
                print(f"  ! {err}", file=sys.stderr)
        return 0 if result.ok else 1

    if args.action == "status":
        info = core.status_dict()
        if args.json:
            print(json.dumps(info, ensure_ascii=False, indent=2))
        else:
            for line in core.status_lines():
                _say(line)
        return 0

    if args.action == "serve":
        if not start_tcp_background(core):
            print(f"MSFP 服务启动失败：{core.tcp_service().last_error}", file=sys.stderr)
            return 1
        try:
            core.start_watcher()
        except Exception as exc:  # noqa: BLE001
            logger.error("定时轮询启动异常：%r", exc)
        _say(
            theme.marked(
                theme.MARK_OK,
                "MSFP",
                "服务运行中：{}（协议 {}）· 目录 {}（Ctrl+C 退出）".format(
                    core.endpoint(), PROTOCOL_NAME, core.dist_dir
                ),
            )
        )
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print()
        finally:
            core.stop_watcher()
            core.stop_tcp()
        return 0

    if args.action == "check":
        return run_check(core, args)

    classify = ClassifyService(
        config=cfg,
        dist_dir=core.dist_dir,
        data_dir=core.data_dir,
        logger=logger,
        state_path=core.state_path,
    )

    if args.action == "deps":
        deps = DependencyService(config=cfg, dist_dir=core.dist_dir, data_dir=core.data_dir, logger=logger)
        report = deps.analyze()
        if args.json:
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        else:
            for line in deps.report_lines(report):
                _say(line)
            print(f"JSON 报告：{deps.report_path}")
        # 缺失前置属于「检查成功但发现问题」，退出码仍为 0（构建/搬运不受影响）；
        # 只有扫描流程本身失败（目录不存在等）才返回 1。
        return 0 if report.ok else 1

    if args.action in ("deps-fix", "deps-fix-apply"):
        # 三步式：deps-fix 只列编号清单；deps-fix <编号...> 只装选中的；deps-fix apply 全选推荐候选
        fix = DepsFixService(config=cfg, dist_dir=core.dist_dir, data_dir=core.data_dir, logger=logger)
        selections = [str(item) for item in (args.target or [])]
        download_all = args.action == "deps-fix-apply" or selections[:1] == ["apply"]
        if not download_all and not selections:
            plan = fix.plan()
            if args.json:
                print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
            else:
                for line in fix.plan_lines(plan):
                    _say(line)
                print(f"JSON 报告：{fix.report_path}")
            return 0 if plan.ok else 1
        outcome = fix.apply() if download_all else fix.apply_selection(selections)
        if args.json:
            print(json.dumps(outcome.to_dict(), ensure_ascii=False, indent=2))
        else:
            for line in fix.outcome_lines(outcome):
                _say(line)
            print(f"JSON 报告：{fix.report_path}")
        for err in outcome.errors:
            print(f"  ! {err}", file=sys.stderr)
        return 0 if outcome.ok else 1

    if args.action == "classify":
        report = classify.analyze(refresh=args.refresh)
        if args.json:
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        else:
            for line in classify.report_lines(report):
                _say(line)
            print(f"JSON 报告：{classify.report_path}")
        return 0 if report.ok else 1

    if args.action == "classify-apply":
        result = classify.apply()
        if args.json:
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        else:
            _say(theme.marked(
                theme.MARK_OK if result.ok else theme.MARK_BAD,
                "搬运结果",
                result.message,
            ))
            if result.backup_dir:
                _say(theme.kv("备份目录", str(result.backup_dir)))
            if result.report_path:
                print(f"JSON 报告：{result.report_path}")
        for err in result.errors:
            print(f"  ! {err}", file=sys.stderr)
        return 0 if result.ok else 1

    return 2


# --------------------------------------------------------------------------- check
def run_check(core: AutoSyncCore, args: argparse.Namespace) -> int:
    """起一个 MSFP 服务，用真实客户端跑一遍协议自检（PING / SIZE / GET / 分块 / 并发 / 错误码）。"""
    failures: List[str] = []

    def check(condition: bool, label: str, detail: str = "") -> None:
        print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f" -> {detail}" if detail else ""))
        if not condition:
            failures.append(label)

    dist_dir = core.dist_dir
    manifest_file = core.manifest_path
    if not manifest_file.is_file():
        print(f"清单不存在：{manifest_file}，请先 build", file=sys.stderr)
        return 1
    manifest = load_manifest(manifest_file) or {}
    entries = manifest.get("files") or []

    service = AutoSyncTCPService(
        dist_dir=dist_dir,
        host="127.0.0.1",
        port=int(args.port or 0),
        logger=core.logger,
        path_prefix=core.config.hosted_url_prefix,
        idle_timeout=core.config.tcp_idle_timeout_seconds,
    )
    if not service.start():
        print(f"服务启动失败：{service.last_error}", file=sys.stderr)
        return 1
    host, port = "127.0.0.1", service.port
    print(f"== MSFP 自检：{host}:{port}（协议 {PROTOCOL_NAME}，目录 {dist_dir}）")
    try:
        with MsfpClient(host, port) as client:
            # 1. PING
            rtt = client.ping()
            check(True, "PING -> OK 0", f"RTT {rtt * 1000:.2f} ms")

            # 2. 清单可读且是合法 JSON
            manifest_remote = client.get(core.config.manifest_name)
            parsed = json.loads(manifest_remote.decode("utf-8"))
            check(parsed.get("version") == manifest.get("version"), "GET manifest.json 与本地一致", str(parsed.get("version")))
            check(client.size(core.config.manifest_name) == len(manifest_remote), "SIZE manifest.json 正确")

            # 3. speedtest.bin（SIZE + 整文件 + 首块）
            speedtest_name = core.config.speedtest_name
            if core.config.speedtest_enabled:
                size = client.size(speedtest_name)
                check(size == core.config.speedtest_size, f"SIZE {speedtest_name} = {core.config.speedtest_size}", str(size))
                first_block = client.get(speedtest_name, 0, 1023)
                on_disk = (dist_dir / speedtest_name)
                expected = on_disk.read_bytes()[:1024] if on_disk.is_file() else b""
                check(first_block == expected, "GET speedtest.bin 0-1023 字节与磁盘一致", f"{len(first_block)} 字节")

            # 4. 挑中文/方括号文件名，走 SIZE + 分块 GET，逐字节核对
            unicode_entry = next(
                (e for e in entries if any(ord(c) > 127 for c in e["path"]) and "[" in e["path"]),
                None,
            ) or next((e for e in entries if any(ord(c) > 127 for c in e["path"])), None)
            if unicode_entry is None:
                check(False, "清单里应有中文/特殊字符文件名")
            else:
                path = unicode_entry["path"]
                local = dist_dir / path
                remote_size = client.size(path)
                check(remote_size == local.stat().st_size, f"SIZE 中文/方括号文件 {path}", str(remote_size))
                start, end = 1024, 4096
                got = client.get(path, start, end)
                check(
                    got == local.read_bytes()[start : end + 1],
                    f"GET {path} [{start}-{end}] 字节区间正确",
                    f"{len(got)} 字节",
                )
                check(
                    unicode_entry["urls"][0] == path,
                    "清单里的自托管地址就是分发目录相对路径（MSFP 客户端可直接当 path 用）",
                    unicode_entry["urls"][0],
                )

            # 5. 错误码
            try:
                client.size("no/such/file.jar")
                check(False, "不存在的文件应报 ERR not found")
            except MsfpError as exc:
                check(str(exc) == "not found", "不存在的文件 -> ERR not found", str(exc))
            for bad in ("../secret.txt", "/etc/passwd", "C:/Windows/win.ini", "mods/../../secret"):
                try:
                    client.size(bad)
                    check(False, f"{bad} 应被拒绝")
                except MsfpError as exc:
                    check(str(exc) == "forbidden", f"逃逸路径被拒绝：{bad}", str(exc))
            raw = client.send_raw(b"HELLO\r\n")  # stream 已被 send_raw 覆盖为普通行
            check(raw.startswith(b"ERR bad request"), "未知动词 -> ERR bad request", repr(raw))

        # 6. 并发连接分块下载 speedtest.bin，整文件 sha256 校验
        speedtest_name = core.config.speedtest_name
        if core.config.speedtest_enabled:
            size = (dist_dir / speedtest_name).stat().st_size
            with tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / "speedtest-downloaded.bin"
                report = download_multithreaded(host, port, speedtest_name, dest, size, threads=max(2, args.connections))
                check(
                    sha256_file(dest) == sha256_file(dist_dir / speedtest_name),
                    f"{args.connections} 并发连接分块下载 {speedtest_name} 校验一致",
                    f"{report.chunks} 块 / {report.size / 1024:.0f} KB / {report.seconds:.2f}s = {report.speed_mbps:.1f} MB/s",
                )

        # 7. keep-alive：一个连接连续多个请求
        with MsfpClient(host, port) as client:
            ok = True
            for _ in range(5):
                ok = ok and client.ping() >= 0
            size_a = client.size(core.config.manifest_name)
            size_b = client.size(core.config.manifest_name)
            check(ok and size_a == size_b, "单连接 keep-alive 连续 7 个请求均正常", f"size={size_a}")

        stats = service.stats
        print(
            "  统计：连接 {}（当前 {}），PING {}，SIZE {}，GET {}（分块 {}），发送 {} 字节".format(
                stats.get("connections"), stats.get("active_connections"), stats.get("pings"),
                stats.get("sizes"), stats.get("gets"), stats.get("ranged_gets"), stats.get("bytes_sent"),
            )
        )
    finally:
        service.stop()

    print()
    if failures:
        print(f"结果：{len(failures)} 项失败 -> {failures}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
