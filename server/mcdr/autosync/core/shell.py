"""AutoSync 交互式 shell（独立运行版，**不依赖 mcdreforged**）。

提示符 ``AutoSync> ``，指令**不带前缀**（``status`` / ``build`` / ``deps fix 1a 2a`` /
``classify`` / ``tcp restart``），回复就是 ``print()``；``help`` / ``exit`` / ``quit``
是内置命令。

要点
----
* 启动即拉起 MSFP 服务（在后台线程启动，accept 循环本身也是守护线程），主线程只处理输入。
* 耗时命令（build / classify / deps / deps fix）在**标准库 threading.Thread** 里跑，
  跑完再回到提示符——这样既满足「不阻塞服务」，输出顺序也稳定（不会和提示符交错）。
* 所有输出都过 :func:`autosync.theme.to_ansi`：终端是彩色 + UTF-8 符号；
  ``NO_COLOR`` 或重定向时自动去色。
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, TextIO

from . import __version__, theme
from .builder import BuildResult, AutoSyncCore
from .classify import ClassifyService
from .config import AutoSyncConfig, load_config_file
from .deps import DependencyService
from .deps_fix import DepsFixService
from .tcp_server import PROTOCOL_NAME

__all__ = [
    "PROMPT",
    "HELP_GROUPS",
    "AutoSyncShell",
    "help_lines",
    "build_result_lines",
    "resolve_base_dir",
    "start_tcp_background",
    "start_services",
]

#: REPL 提示符
PROMPT = "AutoSync> "

#: 帮助内容：(分组, ((命令, 说明), ...))；命令为空串表示纯说明行。
#: 顺序与用法对测试/README 都是稳定契约，改这里记得同步测试。
HELP_GROUPS: Sequence[Any] = (
    (
        "基础",
        (
            ("status", "查看文件数 / 清单版本 / MSFP 端口与连接统计"),
            ("build", "重建客户端清单与测速文件"),
            ("build refresh", "忽略 Modrinth 缓存强制重查后重建"),
            ("reload", "重载 config.json 并重启 MSFP 服务与定时轮询"),
            ("tcp start|stop|restart", "控制 MSFP（裸 TCP）分发服务（http 是同一命令的旧别名）"),
        ),
    ),
    (
        "模组分类",
        (
            ("classify", "干跑：分析纯客户端/双端/纯服务端/待定（不改动任何文件）"),
            ("classify apply", "按上次 classify 结果搬运（双端复制；纯服务端默认也只复制）"),
        ),
    ),
    (
        "依赖检查",
        (
            ("deps", "只读扫描 jar 元数据（含 JiJ 嵌套层），列出真缺失前置 / JiJ 已提供 / 无法解析"),
        ),
    ),
    (
        "缺失前置下载",
        (
            ("deps fix", "列出缺失前置编号清单与可选版本（a/b/c），不下载任何文件"),
            ("deps fix 1a 2a", "只下载编号 1a、2a 这两项（不填编号则什么都不下载）"),
            ("deps fix apply", "全选推荐候选（每项取 a；只有预发布版时取 b/c）并下载到 <dist_dir>/mods/"),
            ("", "下载源：Modrinth 优先，失败兜底 CurseForge（官方 API / 免 key 的 CFWidget）"),
            ("", "哈希校验；同名不同内容绝不覆盖；清单缓存 30 分钟，过期请重新运行 deps fix"),
        ),
    ),
    (
        "其它",
        (
            ("help", "显示本帮助"),
            ("exit | quit", "退出 shell（MSFP 服务会一起停掉）"),
        ),
    ),
)

#: 帮助里只受开关控制的分组
_DEPS_FIX_GROUP = "缺失前置下载"
_CLASSIFY_GROUP = "模组分类"
#: 帮助里命令列的宽度（按显示宽度对齐，中文算 2）
_HELP_COMMAND_WIDTH = 24

_USAGE = "用法：直接输入子命令（不带前缀），例如 status / build / deps fix 1a 2a"


def help_lines(classify_enabled: bool = True, deps_fix_enabled: bool = True) -> List[str]:
    """``help`` 的帮助正文（**逐行**返回，调用方一行一次 print）。"""
    lines: List[str] = [theme.hint(_USAGE)]
    for group, rows in HELP_GROUPS:
        if group == _CLASSIFY_GROUP and not classify_enabled:
            continue
        if group == _DEPS_FIX_GROUP and not deps_fix_enabled:
            continue
        lines.append(theme.group(group))
        for command, note in rows:
            if command:
                pad = " " * max(2, _HELP_COMMAND_WIDTH - theme.display_width(command))
                lines.append("  §f{cmd}{pad}§7{note}".format(cmd=command, pad=pad, note=note))
            else:
                lines.append("  §7  └ {note}".format(note=note))
    return theme.box("AutoSync 帮助", lines)


def _hit_rate_color(hit_rate: float) -> str:
    """命中率配色：≥90% 绿、≥50% 黄、其余红。"""
    if hit_rate >= 90:
        return theme.C_OK
    if hit_rate >= 50:
        return theme.C_WARN
    return theme.C_BAD


def build_result_lines(result: BuildResult) -> List[str]:
    """``build`` 的完成汇总（QBM 风格框，**逐行**返回）。"""
    if result.busy:
        return [theme.marked(theme.MARK_WARN, "构建", "已有构建任务在运行，请稍后再试")]
    if not result.ok:
        return theme.box(
            "AutoSync 构建失败",
            [
                theme.marked(theme.MARK_BAD, "失败原因", result.message or "未知错误"),
                theme.kv("分发目录", str(result.dist_dir)),
                theme.kv("排查建议", "看控制台日志里的异常堆栈；修好后重新执行 build"),
            ],
        )

    rate_color = _hit_rate_color(result.modrinth_hit_rate)
    body = [
        theme.marked(
            theme.MARK_OK,
            "文件总数",
            "{count} 个 §7/ §f{size} §7（jar §f{jar}§7 个）".format(
                count=result.file_count,
                size=theme.human_size(result.total_size),
                jar=result.jar_count,
            ),
        ),
        theme.marked(
            theme.MARK_OK,
            "清单版本",
            "{version} §7（{changed}）".format(
                version=result.version or "(未知)",
                changed="内容有变化" if result.changed else "内容未变化，版本号保持",
            ),
        ),
        theme.kv(
            "Modrinth",
            "{color}{hits} 命中{reset} §7/ §f{self_hosted} 自托管 §7（命中率 {color}{rate:.1f}%{reset}§7）".format(
                color=rate_color,
                hits=result.modrinth_hits,
                reset=theme.C_RESET,
                self_hosted=result.self_hosted_only,
                rate=result.modrinth_hit_rate,
            ),
        ),
        theme.kv(
            "清单大小",
            "{size} §7· 待删除 §f{deletes} §7条".format(
                size=theme.human_size(result.manifest_size), deletes=len(result.deletes)
            ),
        ),
        theme.kv(
            "构建耗时",
            "总计 §f{total:.2f}s §7（哈希 §f{hashed:.2f}s §7· 网络 §f{network:.2f}s§7）".format(
                total=result.elapsed, hashed=result.hash_seconds, network=result.modrinth_seconds
            ),
        ),
    ]
    speedtest = result.speedtest or {}
    if speedtest.get("name"):
        body.append(
            theme.kv(
                "测速文件",
                "{color}{name}{reset} §7（{size} · {state}）".format(
                    color=theme.C_OK,
                    name=speedtest.get("name"),
                    reset=theme.C_RESET,
                    size=theme.human_size(speedtest.get("size") or 0),
                    state=theme.ok("本次新生成") if speedtest.get("written") else theme.hint("已存在，原样复用"),
                ),
            )
        )
    if result.deps_checked:
        if result.deps_missing:
            missing = "、".join(result.deps_missing_ids[:5]) or "(见报告)"
            body.append(
                theme.marked(
                    theme.MARK_WARN,
                    "依赖检查",
                    "{warn}发现 {count} 个真缺失前置：{ids}{reset} §7（客户端会崩，详见 deps）".format(
                        warn=theme.C_WARN,
                        count=theme.color_number(result.deps_missing, zero_is_ok=True),
                        ids=missing,
                        reset=theme.C_RESET,
                    ),
                )
            )
        else:
            body.append(theme.marked(theme.MARK_OK, "依赖检查", theme.ok("未发现真缺失前置")))
    if result.errors:
        body.append(
            theme.marked(
                theme.MARK_WARN,
                "告警",
                "本轮有 {count} 条 Modrinth 请求错误（未命中的文件降级为服务端托管）".format(
                    count=theme.color_number(len(result.errors), zero_is_ok=True)
                ),
            )
        )
    return theme.box("AutoSync 构建完成", body)


# --------------------------------------------------------------------------- 路径 / 服务
def resolve_base_dir(config: AutoSyncConfig, cwd: Optional[Path] = None) -> Path:
    """解释 ``dist_dir`` 相对路径的基准目录。

    * ``base_dir`` 为空（默认）或为旧值 ``"mcdr_root"`` -> 当前工作目录
      （即 ``config.json`` 所在目录；systemd 里由 ``WorkingDirectory`` 决定）；
    * 其它值：绝对路径直接用，相对路径按当前工作目录解析。
    """
    root = Path(cwd) if cwd is not None else Path.cwd()
    raw = str(config.base_dir or "").strip()
    if not raw or raw == "mcdr_root":
        return root
    path = Path(os.path.expanduser(raw))
    return path if path.is_absolute() else root / path


def start_tcp_background(core: AutoSyncCore, timeout: float = 30.0) -> bool:
    """在**后台线程**里启动 MSFP 服务，返回是否启动成功。

    ``AutoSyncTCPService.start()`` 本身内部就有 accept 线程、调用即返回；
    这里再包一层线程是为了让「启动」这件事完全不占主线程（端口占用探测等可能稍慢），
    主线程只等一个短超时拿结果。
    """
    box: Dict[str, Any] = {}

    def worker() -> None:
        try:
            box["ok"] = core.start_tcp()
        except Exception as exc:  # noqa: BLE001 - 启动异常不该炸掉 shell
            box["error"] = repr(exc)

    thread = threading.Thread(target=worker, name="AutoSync-MSFP-Start", daemon=True)
    thread.start()
    thread.join(timeout)
    if "error" in box:
        core.logger.error("AutoSync MSFP 启动异常：%s", box["error"])
    return bool(box.get("ok"))


def start_services(core: AutoSyncCore) -> bool:
    """启动 MSFP 服务 + 定时轮询，返回 MSFP 是否在跑。"""
    ok = start_tcp_background(core)
    try:
        core.start_watcher()
    except Exception as exc:  # noqa: BLE001
        core.logger.error("AutoSync 定时轮询启动异常：%r", exc)
    return ok


# --------------------------------------------------------------------------- shell
class AutoSyncShell:
    """交互式 REPL：解析一行指令 -> 调核心服务 -> 逐行打印。

    ``out`` 是输出回调（默认 ``print``），测试里可以换成列表的 ``append``；
    ``color=None`` 时按 stdout 是否 TTY 自动决定上色（``NO_COLOR`` 优先）。
    """

    def __init__(
        self,
        core: AutoSyncCore,
        config_path: Optional[Path] = None,
        out: Optional[Callable[[str], None]] = None,
        logger: Optional[logging.Logger] = None,
        color: Optional[bool] = None,
        stream: Optional[TextIO] = None,
    ) -> None:
        self.core = core
        self.config_path = Path(config_path) if config_path is not None else None
        self.logger = logger or core.logger
        self.out = out or print
        self.color = color
        self.stream = stream

    # ------------------------------------------------------------------ 输出
    def emit(self, text: Any) -> None:
        """输出一行（含 ``§`` 代码时转 ANSI；去色模式下退化为纯文本）。"""
        self.out(theme.to_ansi(text, color=self.color, stream=self.stream))

    def emit_lines(self, lines: Any) -> None:
        """**逐行**输出：多行字符串会被拆开，空行原样保留。"""
        for line in split_lines(lines):
            self.emit(line)

    # ------------------------------------------------------------------ 服务
    def start(self) -> bool:
        """启动 MSFP 服务 + 定时轮询，并打印启动横幅。"""
        ok = start_services(self.core)
        self.emit(
            theme.marked(
                theme.MARK_OK if ok else theme.MARK_BAD,
                "MSFP",
                "服务{state}：{endpoint}（协议 {proto}）· 分发目录 {dist}".format(
                    state="运行中" if ok else "启动失败",
                    endpoint=self.core.endpoint(),
                    proto=PROTOCOL_NAME,
                    dist=self.core.dist_dir,
                ),
            )
        )
        if not ok:
            self.emit(
                theme.marked(
                    theme.MARK_BAD,
                    "原因",
                    "{}:{} —— {}".format(
                        self.core.config.tcp_host,
                        self.core.config.tcp_port,
                        self.core.tcp_service().last_error,
                    ),
                )
            )
        return ok

    def shutdown(self) -> None:
        """停服务 + 停轮询（退出 shell 时调用）。"""
        try:
            self.core.stop_watcher()
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("AutoSync 停止轮询异常：%r", exc)
        try:
            self.core.stop_tcp()
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("AutoSync 停止 MSFP 服务异常：%r", exc)

    # ------------------------------------------------------------------ 工厂（测试可替换）
    def make_deps_service(self, core: AutoSyncCore) -> DependencyService:
        return DependencyService(
            config=core.config, dist_dir=core.dist_dir, data_dir=core.data_dir, logger=core.logger
        )

    def make_deps_fix_service(self, core: AutoSyncCore) -> DepsFixService:
        return DepsFixService(
            config=core.config,
            dist_dir=core.dist_dir,
            data_dir=core.data_dir,
            logger=core.logger,
            deps_service=self.make_deps_service(core),
        )

    def make_classify_service(self, core: AutoSyncCore) -> ClassifyService:
        return ClassifyService(
            config=core.config,
            dist_dir=core.dist_dir,
            data_dir=core.data_dir,
            logger=core.logger,
            state_path=core.state_path,
        )

    # ------------------------------------------------------------------ 分发
    def execute(self, line: Any) -> bool:
        """执行一行指令；返回 ``False`` 表示该退出 shell。"""
        tokens = str(line or "").strip().split()
        if not tokens:
            self.show_help()
            return True
        command = tokens[0].lower()
        rest = [token.lower() for token in tokens[1:]]

        if command in ("help", "?", "h"):
            self.show_help()
        elif command in ("exit", "quit", "q"):
            self.emit(theme.hint("已退出 AutoSync shell。"))
            return False
        elif command in ("status", "st"):
            self.show_status()
        elif command == "build":
            self.do_build(refresh=rest[:1] in (["refresh"], ["force"]))
        elif command == "reload":
            self.do_reload()
        elif command in ("tcp", "http"):
            self.do_tcp(rest[0] if rest else "")
        elif command == "classify":
            self.do_classify(apply=rest[:1] == ["apply"])
        elif command == "deps":
            if rest[:1] == ["fix"]:
                self.do_deps_fix(rest[1:])
            else:
                self.do_deps()
        else:
            self.emit(theme.bad("未知命令：{}".format(tokens[0])))
            self.emit(theme.hint(_USAGE))
            self.emit(theme.hint("输入 help 查看全部命令"))
        return True

    def run(self, start: bool = True) -> int:
        """REPL 主循环：启动服务 -> 读输入 -> 分发，直到 ``exit`` / ``quit`` / EOF。"""
        self.emit(theme.title("AutoSync shell v{}".format(__version__)))
        self.emit(theme.kv("配置文件", str(self.config_path) if self.config_path else "(内置默认值)"))
        self.emit(theme.kv("基准目录", str(self.core.base_dir)))
        if start:
            self.start()
        self.emit(theme.hint("输入 help 查看命令；exit / quit 退出。"))
        while True:
            try:
                line = input(PROMPT)
            except EOFError:
                self.emit("")
                self.emit(theme.hint("已退出 AutoSync shell。"))
                return 0
            except KeyboardInterrupt:
                self.emit("")
                self.emit(theme.hint("收到 Ctrl+C；再按一次 Ctrl+C 或输入 exit / quit 退出。"))
                continue
            if not self.execute(line):
                return 0

    # ------------------------------------------------------------------ 命令实现
    def show_help(self) -> None:
        self.emit_lines(help_lines(self.core.config.classify_enabled, self.core.config.deps_fix_enabled))

    def show_status(self) -> None:
        lines = list(self.core.status_lines())
        lines.append(
            theme.kv("配置文件", str(self.config_path) if self.config_path else "(内置默认值)")
        )
        lines.append(theme.kv("更多命令", "§fhelp §7（列出全部命令与用法）"))
        self.emit_lines(lines)

    def do_build(self, refresh: bool = False) -> None:
        self.emit(
            theme.marked(
                theme.MARK_INFO,
                "构建",
                "开始构建（{}），详见下方输出".format("强制刷新 Modrinth 缓存" if refresh else "使用缓存"),
            )
        )

        def work() -> None:
            result = self.core.build(refresh_modrinth=refresh)
            self.emit_lines(build_result_lines(result))

        self._run_in_thread("AutoSync-Builder", work)

    def do_reload(self) -> None:
        if self.config_path is None:
            self.emit(theme.bad("没有配置文件路径，无法重载（启动时请用 --config 指定）"))
            return
        try:
            new_config = load_config_file(self.config_path)
        except Exception as exc:  # noqa: BLE001 - 配置写坏了不该炸掉 shell
            self.emit(theme.bad("配置重载失败：{!r}".format(exc)))
            return
        self.core.config = new_config
        self.core.restart_tcp()
        self.core.start_watcher()
        self.emit(
            theme.marked(
                theme.MARK_OK,
                "配置已重载",
                "分发目录 {} §7· MSFP {} §7· 轮询 {}s".format(
                    self.core.dist_dir, self.core.endpoint(), new_config.poll_interval_seconds
                ),
            )
        )

    def do_tcp(self, verb: str) -> None:
        if verb == "start":
            if start_tcp_background(self.core):
                self.emit(
                    theme.marked(
                        theme.MARK_OK, "MSFP", "服务已启动：{}（协议 {}）".format(self.core.endpoint(), PROTOCOL_NAME)
                    )
                )
            else:
                self.emit(
                    theme.marked(
                        theme.MARK_BAD,
                        "MSFP",
                        "服务启动失败（{}:{}）：{}".format(
                            self.core.config.tcp_host,
                            self.core.config.tcp_port,
                            self.core.tcp_service().last_error,
                        ),
                    )
                )
        elif verb == "stop":
            self.core.stop_tcp()
            self.emit(theme.marked(theme.MARK_OK, "MSFP", "服务已停止"))
        elif verb == "restart":
            if self.core.restart_tcp():
                self.emit(theme.marked(theme.MARK_OK, "MSFP", "服务已重启：{}".format(self.core.endpoint())))
            else:
                self.emit(theme.marked(theme.MARK_BAD, "MSFP", "服务重启失败，详见日志"))
        else:
            self.emit(theme.hint("用法：tcp start|stop|restart"))

    def do_classify(self, apply: bool = False) -> None:
        if not self.core.config.classify_enabled:
            self.emit(theme.warn("分类功能已禁用（classify_enabled=false）；改好配置后执行 reload 生效"))
            return
        service = self.make_classify_service(self.core)
        if not apply:
            self.emit(theme.marked(theme.MARK_INFO, "分类", "开始分类分析（dry-run，不会改动任何文件）"))

            def work() -> None:
                report = service.analyze()
                lines = list(service.report_lines(report))
                lines.append(theme.kv("JSON 报告", str(service.report_path)))
                if report.ok:
                    lines.append(theme.marked(theme.MARK_WARN, "下一步", "确认无误后执行 §fclassify apply"))
                self.emit_lines(lines)

            self._run_in_thread("AutoSync-Classify", work)
            return

        self.emit(theme.marked(theme.MARK_INFO, "分类", "开始按上次结果搬运（复制/移动）"))

        def work_apply() -> None:
            result = service.apply()
            self.emit(
                theme.marked(
                    theme.MARK_OK if result.ok else theme.MARK_BAD,
                    "搬运结果",
                    theme.ok(result.message) if result.ok else theme.bad(result.message),
                )
            )
            if result.conflicts:
                self.emit(
                    theme.marked(
                        theme.MARK_WARN,
                        "同名冲突",
                        "有 {} 个未覆盖，请查看报告：{}".format(result.conflicts, result.report_path),
                    )
                )
            if result.backup_dir:
                self.emit(theme.kv("备份目录", str(result.backup_dir)))

        self._run_in_thread("AutoSync-Classify-Apply", work_apply)

    def do_deps(self) -> None:
        service = self.make_deps_service(self.core)
        self.emit(theme.marked(theme.MARK_INFO, "依赖检查", "开始只读扫描 jar 元数据（不会改动任何文件）"))

        def work() -> None:
            report = service.analyze()
            lines = list(service.report_chat_lines(report))
            lines.append(theme.kv("JSON 报告", str(service.report_path)))
            self.emit_lines(lines)

        self._run_in_thread("AutoSync-Deps", work)

    def do_deps_fix(self, selection: Sequence[str]) -> None:
        if not self.core.config.deps_fix_enabled:
            self.emit(theme.warn("自动下载缺失前置已禁用（deps_fix_enabled=false），可在 config.json 里打开后 reload"))
            return
        service = self.make_deps_fix_service(self.core)
        tokens = [str(token) for token in selection]

        if not tokens:
            self.emit(theme.marked(theme.MARK_INFO, "缺失前置", "开始分析清单（只分析，不会下载/改动任何文件）"))

            def work_plan() -> None:
                plan = service.plan()
                lines = list(service.plan_lines(plan))
                lines.append(theme.kv("JSON 报告", str(service.report_path)))
                self.emit_lines(lines)

            self._run_in_thread("AutoSync-DepsFix-Plan", work_plan)
            return

        if tokens[:1] == ["apply"]:
            self.emit(theme.marked(theme.MARK_INFO, "下载", "全选推荐候选，下载到 <dist_dir>/mods/"))

            def work_all() -> None:
                self._emit_outcome(service, service.apply())

            self._run_in_thread("AutoSync-DepsFix-Apply", work_all)
            return

        self.emit(
            theme.marked(theme.MARK_INFO, "下载", "按编号 {} 下载到 <dist_dir>/mods/".format(" ".join(tokens)))
        )

        def work_selected() -> None:
            self._emit_outcome(service, service.apply_selection(tokens))

        self._run_in_thread("AutoSync-DepsFix-Apply", work_selected)

    def _emit_outcome(self, service: DepsFixService, outcome: Any) -> None:
        lines = list(service.outcome_lines(outcome))
        lines.append(theme.kv("JSON 报告", str(service.report_path)))
        self.emit_lines(lines)
        for err in outcome.errors:
            self.emit(theme.marked(theme.MARK_BAD, "错误", str(err)))

    # ------------------------------------------------------------------ 线程
    def _run_in_thread(self, name: str, func: Callable[[], None]) -> None:
        """在标准库线程里跑耗时任务，等它跑完再回到提示符（输出顺序稳定）。

        MCDR 时期的 ``@new_thread`` 装饰器在这里换成 :class:`threading.Thread`：
        不再有 watchdog 兜底，所以线程内异常一律就地捕获并打印，绝不让 shell 崩掉。
        """

        def guarded() -> None:
            try:
                func()
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("AutoSync 任务执行失败：%r", exc)
                self.emit(theme.marked(theme.MARK_BAD, "任务失败", repr(exc)))

        thread = threading.Thread(target=guarded, name=name, daemon=False)
        thread.start()
        thread.join()


def split_lines(lines: Any) -> List[str]:
    """把入参规整成**逐行**列表：多行字符串拆开，空行原样保留。"""
    if isinstance(lines, str):
        lines = [lines]
    result: List[str] = []
    for item in lines:
        text = item if isinstance(item, str) else str(item)
        if "\n" in text:
            result.extend(text.split("\n"))
        else:
            result.append(text)
    return result
