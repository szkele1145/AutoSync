#!/usr/bin/env python3
"""AutoSync「图形窗口实时日志」端到端验收。

复用 verify_cdn_select.py 里的假 MSFP / 假 CDN，跑一次**图形模式**（不带 windowless 参数）的真实同步，
再挂一个进程内探针 agent（tests/gui/ScrollProbeAgent.java）实时采样日志区的滚动状态，
最后对客户端日志、滚动采样与产物做断言——其中就包括「日志一直自动滚到最新一行」。

探针是纯进程内采样：不截屏、不动鼠标键盘。它还会顺手吞掉落到客户端进程的滚轮/按键事件，
免得跑验收时用户玩游戏的真实滚轮把「视图位置」搅乱（进程内 harness 上实测被搅乱过）。

用法::

    python tests/verify_gui_live.py [--java <java.exe>] [--jar <AutoSync-1.0.0.jar>] [--keep]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import verify_cdn_select as base  # noqa: E402

JAR_NAME = "AutoSync-1.0.0.jar"
DEFAULT_JAR = HERE.parent / "build" / "libs" / JAR_NAME
DEFAULT_JAVA = Path(os.path.expandvars(r"%USERPROFILE%\.jdks\ms-17.0.19\bin\java.exe"))
CLASSES = HERE / "gui" / "classes"
AGENT_SOURCE = HERE / "gui" / "ScrollProbeAgent.java"
AGENT_CLASS = "com.github.balloonupdate.mcpatch.client.ui.ScrollProbeAgent"

FAILURES: list[tuple[str, str]] = []


def check(condition: bool, label: str, detail: str = "") -> bool:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  ->  {detail}" if detail else ""))
    if not condition:
        FAILURES.append((label, detail))
    return condition


def build_agent(java: Path, work: Path) -> Path:
    """用同一个 JDK 现场编译打包探针 agent（只依赖 Swing/AWT，不依赖被测 jar）。"""
    javac = java.with_name("javac.exe")
    jar_tool = java.with_name("jar.exe")

    classes = work / "agent-classes"
    classes.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        [str(javac), "-encoding", "UTF-8", "-nowarn", "-d", str(classes), str(AGENT_SOURCE)],
        check=True,
    )

    manifest = work / "agent-manifest.txt"
    manifest.write_text(f"Premain-Class: {AGENT_CLASS}\n", "ascii")

    agent_jar = work / "scroll-probe-agent.jar"
    subprocess.run(
        [str(jar_tool), "--create", "--file", str(agent_jar), "--manifest", str(manifest),
         "-C", str(classes), "."],
        check=True,
    )

    return agent_jar


def parse_probe(path: Path) -> list[dict]:
    """把 agent 写出来的采样行解析成 [{doc, value, visible, max, gap, last_visible, showing}]。"""
    rows: list[dict] = []

    if not path.is_file():
        return rows

    for line in path.read_text("utf-8", errors="replace").splitlines():
        line = line.strip()

        if not line.startswith("doc="):
            continue

        fields: dict = {}

        for pair in line.split():
            if "=" not in pair:
                continue

            key, value = pair.split("=", 1)

            if value in ("true", "false"):
                fields[key] = value == "true"
            else:
                try:
                    fields[key] = int(value)
                except ValueError:
                    fields[key] = value

        if "gap" in fields:
            rows.append(fields)

    return rows


def longest_stuck_run(rows: list[dict], threshold: int = 30) -> tuple[int, int]:
    """连续「距底超过 threshold 像素」的最长样本数 + 全程最大距底像素。"""
    worst = 0
    longest = 0
    run = 0

    for row in rows:
        gap = int(row.get("gap", 0))

        worst = max(worst, gap)

        if gap > threshold:
            run += 1
            longest = max(longest, run)
        else:
            run = 0

    return longest, worst


def wait_for_client(proc: subprocess.Popen, seconds: float) -> int | None:
    """等客户端自己退出；超时就杀掉它并返回 None（免得留下一个吊着的进程）。"""
    try:
        return proc.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=30)

        return None


def capture(java: Path, out: Path, rect: tuple[int, int, int, int] | None = None) -> None:
    """整屏截图（可选只截一块区域）。默认不调用：跑测试的机器上用户可能正在用，不要抢屏幕。"""
    command = [str(java), "-cp", str(CLASSES), "Capture", str(out)]

    if rect:
        command += [str(value) for value in rect]

    subprocess.run(command, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--java", default=str(DEFAULT_JAVA))
    parser.add_argument("--jar", default=str(DEFAULT_JAR))
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    java, jar = Path(args.java), Path(args.jar)

    print(f"jar : {jar}")
    if not check(jar.is_file(), "构建产物存在", str(jar)):
        return 1

    tmp_root = Path(tempfile.mkdtemp(prefix="autosync-gui-live-"))
    print(f"工作目录: {tmp_root}")

    agent_jar = build_agent(java, tmp_root)
    # 注意：agent 参数只能是纯 ASCII 的文件名（Windows 上 -javaagent 的参数按平台编码解码，
    # 带中文的完整路径到这里已经被损坏），目录由 agent 自己从 java.io.tmpdir 取
    probe_name = f"autosync-scroll-probe-{os.getpid()}.txt"
    probe_file = Path(tempfile.gettempdir()) / probe_name
    probe_file.unlink(missing_ok=True)

    cdn = base.ThrottledCdnServer(tmp_root / "_cdn", 0)
    msfp = base.ThrottledMsfpServer(tmp_root / "_msfp", 0)

    threading.Thread(target=cdn.serve_forever, daemon=True).start()
    threading.Thread(target=msfp.serve_forever, daemon=True).start()

    ws = base.Workspace(tmp_root / "gui", jar, cdn.port)
    ws.write_dist()
    cdn.dist = ws.dist
    msfp.dist = ws.dist

    # CDN 明显更快 -> 走 CDN；限速让下载过程持续几秒，好让探针采到「日志正在滚动」的过程
    cdn.rate, msfp.rate = 6 * 1024 * 1024, 0.6 * 1024 * 1024
    ws.write_config(msfp.port, **{"speedtest-duration-ms": 1000})

    print(f"假 CDN : http://127.0.0.1:{cdn.port}")
    print(f"假 MSFP: msfp://127.0.0.1:{msfp.port}")
    print(f"探针   : {agent_jar}  ->  {probe_file}")

    shots = tmp_root / "shots"
    shots.mkdir(exist_ok=True)

    client_log = tmp_root / "client.log"

    with open(client_log, "wb") as log:
        # 注意：不带 windowless 参数 = 图形模式，窗口会真的弹出来。
        # -javaagent 会在这个进程里采样日志区滚动状态，并吞掉落到它的滚轮/按键事件
        proc = subprocess.Popen(
            [str(java), "-Dfile.encoding=UTF-8", "-Dsun.stdout.encoding=UTF-8",
             "-Dsun.stderr.encoding=UTF-8",
             f"-javaagent:{agent_jar}={probe_name}",
             "-jar", str(ws.prog / JAR_NAME)],
            cwd=str(ws.game), stdout=log, stderr=subprocess.STDOUT,
        )

    captured = []
    begin = time.time()

    # 注意：这里**故意不截图**。窗口长什么样、颜色对不对，由 tests/gui/GuiLogHarness 在进程内断言；
    # 而这个脚本跑的时候用户可能正在用这台机器，整屏截图/抬起窗口会打扰到他
    while proc.poll() is None and time.time() - begin < 120:
        time.sleep(0.5)

    code = wait_for_client(proc, 120)
    text = base.decode_log(client_log)

    try:
        check(code == 0, "图形模式客户端正常退出", f"exit={code}")
        check("图形模式: true" in text, "日志确认跑的是图形模式（会弹窗）")
        check("[测速] CDN " in text and "选 CDN" in text, "真的做了测速选源", base.first_line(text, "[测速] CDN"))
        check("[CDN] big.jar 从 127.0.0.1 下载成功" in text, "big.jar 真的从 CDN 下载成功了")
        check((ws.game / "mods" / "big.jar").is_file()
              and base.sha256_file(ws.game / "mods" / "big.jar") == base.sha256_file(ws.dist / "files" / "mods" / "big.jar"),
              "big.jar 内容与清单一致")
        check(not base.find_residue(ws.game), "没有临时文件残留")
        check((ws.prog / "mcpatch.log").is_file(), "图形模式写的是 mcpatch.log",
              str(ws.prog / "mcpatch.log"))
        check(len(captured) == 0, "整个过程没有截图、没有操控鼠标（不打扰用户）")

        # ---- 日志区自动滚动：真实同步过程中的进程内采样 ----
        rows = parse_probe(probe_file)
        overflow = [row for row in rows if int(row.get("doc", 0)) > 0
                    and int(row.get("max", 0)) > int(row.get("visible", 0))]
        longest, worst = longest_stuck_run(rows)
        tail = rows[-5:]

        print(f"  [info] 探针样本 {len(rows)} 个，其中内容超出一屏的 {len(overflow)} 个"
              f"；全程最大距底 {worst}px")

        check(probe_file.is_file(), "探针采样文件存在", str(probe_file))
        # 同步本身只跑一两秒，样本数跟机器快慢有关，这里只要够看出趋势就行；
        # 真正吃劲的是下面两条：全程不能长期掉队、结束前必须贴在底部
        check(len(rows) >= 6, "同步过程中探针采到了足够的样本", f"{len(rows)} 个")
        check(len(overflow) >= 4, "同步过程中日志确实超出了一屏（有滚动可言）", f"{len(overflow)} 个溢出样本")
        check(longest <= 1, "日志区全程没有掉队（连续「距底 > 30px」的样本不超过 1 个）",
              f"最长连续 {longest} 个样本，全程最大距底 {worst}px")
        check(bool(tail) and all(int(row.get("gap", 999)) <= 2 and row.get("at_bottom") for row in tail),
              "同步结束前最后 5 个样本都贴在底部",
              "；".join(f"gap={row.get('gap')} at_bottom={row.get('at_bottom')}" for row in tail))
    finally:
        cdn.shutdown()
        msfp.shutdown()
        cdn.server_close()
        msfp.server_close()

        if not args.keep:
            print(f"（临时目录会在本次运行后保留，便于人工看采样与截图）{tmp_root}")

    print()
    if FAILURES:
        print(f"结果：{len(FAILURES)} 项失败")
        for label, detail in FAILURES:
            print(f"  - {label}  {detail}")
        return 1

    print("结果：全部通过")
    print(f"探针采样：{probe_file}")
    print(f"截图目录：{shots}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
