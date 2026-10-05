"""AutoSync —— MCDR 插件入口（命令注册、生命周期、耗时任务丢到独立线程）。

设计要点
--------
* **业务逻辑零重复**：所有构建 / 分类 / 依赖检查都交给内置的独立版核心包
  :mod:`autosync.core`（``AutoSyncCore`` / ``ClassifyService`` / ``DependencyService`` /
  ``DepsFixService``），本模块只做三件事：MCDR 命令树、配置来源、把 ``§`` 文本翻译成 ``RText``。
* **输出复用** :mod:`autosync.core.theme` 的 QBM 风格（``theme.box`` / ``theme.marked`` /
  ``theme.kv`` / ``theme.button``），通过 :func:`_to_rtext` 上色；
  游戏内每条命令还是可点按钮（``suggest_command``，点击后按回车执行）。
* **``!!autosync`` 命令**由内置的 :class:`autosync.core.shell.AutoSyncShell` 执行，
  所以 MCDR 版与独立版的行为、措辞、按钮完全一致（``help`` / ``status`` / ``build`` /
  ``build refresh`` / ``reload`` / ``tcp start|stop|restart`` / ``classify`` /
  ``classify apply`` / ``deps`` / ``deps fix`` / ``deps fix 1a 2a`` / ``deps fix apply``）。
* 需要 ``PermissionLevel.HELPER``（等级 2）的操作：``build`` / ``build refresh`` /
  ``reload`` / ``tcp start|stop|restart`` / ``classify apply`` / ``deps fix <编号>`` /
  ``deps fix apply``；只读操作（``status`` / ``classify`` / ``deps`` / ``deps fix`` / ``help``）人人可用。

游戏内命令：``!!autosync``（``!!as`` 是等价别名）。
"""

from __future__ import annotations

import functools
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from mcdreforged.api.all import (
    CommandSource,
    GreedyText,
    Literal,
    PluginServerInterface,
    RColor,
    RText,
    RTextList,
)

try:  # 点击事件的动作类型：老版本 MCDR 可能没有，缺了按钮自动退化成纯文本
    from mcdreforged.api.rtext import RAction
except ImportError:  # pragma: no cover - 仅老版本 MCDR 会走到
    RAction = None  # type: ignore[assignment]

from . import __version__
from .core import theme
from .core.builder import AutoSyncCore
from .core.config import DEFAULT_CONFIG, AutoSyncConfig, ensure_config_file, load_config_file
from .core.shell import AutoSyncShell

#: MCDR 插件 id（与 mcdreforged.plugin.json 一致）
PLUGIN_ID = "autosync"
#: 主命令前缀
PREFIX = "!!autosync"
#: 等价短别名
PREFIX_ALIAS = "!!as"
#: 会改动磁盘 / 联网下载的命令所需权限（MCDR PermissionLevel.HELPER == 2）
REQUIRE_HELPER_LEVEL = 2
#: 插件自带的日志前缀
LOG_PREFIX = "AutoSync "

_core: Optional[AutoSyncCore] = None
_config_path: Optional[Path] = None
_data_dir: Optional[Path] = None
_logger: logging.Logger = logging.getLogger("AutoSync")


# --------------------------------------------------------------------------- 小工具
def new_thread(name: str) -> Callable:
    """``@new_thread("名字")``：把被装饰函数整体丢进独立守护线程，函数内部的异常只记日志。

    与 MCDR 自带 ``mcdreforged.api.decorator.new_thread`` 的区别：
    这里**不**把参数原样透传（我们只装饰无参闭包），并保证异常不会冒泡打断 MCDR 的命令分发。
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> None:
            def guarded() -> None:
                try:
                    func(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 - 后台任务异常不该炸掉 MCDR
                    _logger.exception("%s后台任务 %s 执行失败：%r", LOG_PREFIX, name, exc)

            threading.Thread(target=guarded, name="AutoSync-{}".format(name), daemon=True).start()

        return wrapper

    return decorator


# --------------------------------------------------------------------------- RText
#: 真实 MCDR 的 RText 具备序列化与上色能力；测试桩没有这些方法时退化为纯字符串
_REAL_RTEXT = hasattr(RText, "to_json_object") and hasattr(RText, "set_color")
#: 真实 RText 是否支持点击 / 悬停
_REAL_CLICK = _REAL_RTEXT and hasattr(RText, "c") and hasattr(RText, "h")
#: 点击动作用 ``suggest_command``：MC 1.19.1+ 的 tellraw 只接受以 ``/`` 开头的 run_command 值，
#: 而 ``!!autosync xxx`` 不以 ``/`` 开头，用 run_command 在 1.21.1 客户端上「点了没反应」。
_CLICK_ACTION = getattr(RAction, "suggest_command", None) if RAction is not None else None
_FALLBACK_ACTION = getattr(RAction, "run_command", None) if RAction is not None else None
#: 点击只是把命令填进聊天框，必须让玩家知道还要按一次回车
CLICK_HINT = "点击后按回车执行（只会填入聊天框，不会自动执行）"

_SPLIT_RE = re.compile("(§.)")
_COLOR_NAMES = {
    "0": "black",
    "1": "dark_blue",
    "2": "dark_green",
    "3": "dark_aqua",
    "4": "dark_red",
    "5": "dark_purple",
    "6": "gold",
    "7": "gray",
    "8": "dark_gray",
    "9": "blue",
    "a": "green",
    "b": "aqua",
    "c": "red",
    "d": "light_purple",
    "e": "yellow",
    "f": "white",
    "r": "reset",
}


def _color_object(code: str) -> Optional[Any]:
    """``§6`` -> ``RColor.gold``；未知代码返回 ``None``（继承上一段颜色）。"""
    if not code:
        return None
    name = _COLOR_NAMES.get(str(code).lstrip("§").lower())
    return getattr(RColor, name, None) if name else None


def _plain_rtext(text: str, color: Optional[Any] = None) -> Any:
    """把一段可含 ``§`` 代码的普通文本转成 RText / RTextList。"""
    if "§" not in text:
        return RText(text, color) if color is not None else RText(text)
    result: List[Any] = []
    current = color
    for piece in _SPLIT_RE.split(text):
        if not piece:
            continue
        if len(piece) == 2 and piece.startswith("§"):
            current = _color_object(piece) or current
            continue
        result.append(RText(piece, current) if current is not None else RText(piece))
    if not result:
        return RText(text)
    if len(result) == 1:
        return result[0]
    return RTextList(*result)


def _button_rtext(label: str, color_code: str, command: str, hover: str) -> Any:
    """把主题按钮记号翻译成可点击 RText（回退链：suggest_command -> run_command -> 纯文本）。"""
    color = _color_object(color_code)
    plain = RText("[{}]".format(label), color) if color is not None else RText("[{}]".format(label))
    action = _CLICK_ACTION or _FALLBACK_ACTION
    if action is None or not command:
        return plain
    tooltip = "{}；{}".format(hover, CLICK_HINT) if hover else CLICK_HINT
    try:
        return plain.c(action, command).h(tooltip)
    except Exception:  # noqa: BLE001 - 老版本 RText 不支持点击事件时退化成纯文本
        try:
            if _FALLBACK_ACTION is not None and action is not _FALLBACK_ACTION:
                return plain.c(_FALLBACK_ACTION, command).h(tooltip)
        except Exception:  # noqa: BLE001
            pass
        return plain


def _to_rtext(text: Any) -> Any:
    """把主题输出的一行转成 RText。

    * 游戏内：MCDR 序列化成 tellraw JSON，``§`` 变颜色，按钮记号变可点按钮；
    * MCDR 控制台：``reply`` 走 ``print_text_to_console``（ANSI 上色），点击不生效但不报错；
    * 没有真实 RText（测试桩）时原样返回字符串。
    """
    raw = text if isinstance(text, str) else str(text)
    if not _REAL_RTEXT:
        return raw
    if theme.has_buttons(raw):
        result: List[Any] = []
        cursor = 0
        for match in theme.BUTTON_RE.finditer(raw):
            head = raw[cursor : match.start()]
            if head:
                result.append(_plain_rtext(head))
            if _REAL_CLICK:
                result.append(
                    _button_rtext(
                        match.group("label"),
                        match.group("color") or "",
                        match.group("cmd"),
                        match.group("hover"),
                    )
                )
            else:
                result.append(RText("[{}]".format(match.group("label"))))
            cursor = match.end()
        tail = raw[cursor:]
        if tail:
            result.append(_plain_rtext(tail))
        if not result:
            return raw
        return result[0] if len(result) == 1 else RTextList(*result)
    if "§" not in raw:
        return raw
    return _plain_rtext(raw)


def split_lines(lines: Any) -> List[str]:
    """把入参规整成**逐行**列表：多行字符串拆开，空行原样保留。"""
    if isinstance(lines, str):
        lines = [lines]
    result: List[str] = []
    for item in lines or ():
        text = item if isinstance(item, str) else str(item)
        if "\n" in text:
            result.extend(text.split("\n"))
        else:
            result.append(text)
    return result


def reply_lines(source: Optional[CommandSource], lines: Any) -> None:
    """**逐行**回复：MCDR 会把多行字符串压成一行，所以这里强制拆行后逐条 reply。"""
    if source is None:
        return
    for line in split_lines(lines):
        source.reply(_to_rtext(line))


def log_lines(server: PluginServerInterface, lines: Any) -> None:
    """**逐行**写 MCDR 控制台日志：去色 + 编码降级（控制台不解析 ``§``）。"""
    for line in split_lines(lines):
        server.logger.info("%s%s", LOG_PREFIX, theme.to_console(line))


# --------------------------------------------------------------------------- 配置
def get_core() -> Optional[AutoSyncCore]:
    """当前的核心对象（未加载成功时为 ``None``）。"""
    return _core


def get_config_path() -> Optional[Path]:
    """当前使用的配置文件路径。"""
    return _config_path


def server_working_dir(server: PluginServerInterface) -> Optional[Path]:
    """MCDR 配置里的 ``working_directory``（一般是 MCDR 根目录下的 ``server/``）。"""
    getter = getattr(server, "get_mcdr_config", None)
    if not callable(getter):
        return None
    try:
        mcdr_config = getter()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(mcdr_config, dict):
        return None
    working_directory = mcdr_config.get("working_directory")
    if not working_directory:
        return None
    path = Path(str(working_directory))
    return path if path.is_absolute() else Path.cwd() / path


def resolve_base_dir(server: PluginServerInterface, config: AutoSyncConfig) -> Path:
    """解释 ``dist_dir`` 相对路径的基准目录。

    * ``base_dir`` 为空 -> 自动：MCDR 的 ``working_directory``（``server/``），退化到 MCDR 根目录；
    * ``mcdr_root`` -> MCDR 根目录（兼容旧值）；
    * 其它 -> 绝对路径直接用，相对路径按 MCDR 根目录解析。
    """
    raw = str(config.base_dir or "").strip()
    if raw == "mcdr_root":
        return Path.cwd()
    if raw:
        path = Path(os.path.expanduser(raw))
        return path if path.is_absolute() else Path.cwd() / path
    return server_working_dir(server) or Path.cwd()


def config_path_for(server: PluginServerInterface) -> Path:
    """配置文件路径：优先 ``config/autosync/config.json``，其次 MCDR 根目录的旧 ``config.json``。"""
    data_folder = Path(server.get_data_folder())
    candidate = data_folder / "config.json"
    legacy = Path.cwd() / "config.json"
    if not candidate.is_file() and legacy.is_file():
        return legacy
    return candidate


def load_config(server: PluginServerInterface) -> AutoSyncConfig:
    """读配置；文件不存在时按默认值生成一份（含全部键与中文注释）。"""
    global _config_path
    path = config_path_for(server)
    try:
        created = ensure_config_file(path)
    except OSError as exc:
        server.logger.warning("%s配置文件写入失败（%s），改用内置默认值", LOG_PREFIX, exc)
        created = False
    if created:
        server.logger.info("%s未找到配置文件，已生成默认配置：%s", LOG_PREFIX, path)
    _config_path = path
    try:
        return load_config_file(path)
    except Exception as exc:  # noqa: BLE001 - 配置写坏不该让插件加载失败
        server.logger.error("%s配置文件解析失败（%s），改用内置默认值：%r", LOG_PREFIX, path, exc)
        config = AutoSyncConfig.from_dict(DEFAULT_CONFIG)
        config.normalize()
        return config


def make_core(server: PluginServerInterface, config: AutoSyncConfig) -> AutoSyncCore:
    """构造核心对象：数据目录固定在 ``config/autosync/autosync-data/``。"""
    global _data_dir
    _data_dir = Path(server.get_data_folder()) / "autosync-data"
    return AutoSyncCore(
        config=config,
        data_dir=_data_dir,
        base_dir=resolve_base_dir(server, config),
        logger=server.logger,
    )


# --------------------------------------------------------------------------- 输出壳
class SourceShell(AutoSyncShell):
    """把 :class:`autosync.shell.AutoSyncShell` 的输出接到 MCDR 命令源。

    ``AutoSyncShell`` 的每个 ``do_*`` 方法都只依赖 ``self.out`` / ``self.emit``，
    所以这里换掉输出通道就等于把整套交互界面搬进了游戏内 / 控制台，
    业务逻辑一行都不用重写。
    """

    def __init__(self, core: AutoSyncCore, source: CommandSource, config_path: Optional[Path] = None) -> None:
        super().__init__(core=core, config_path=config_path, out=self._reply, logger=core.logger)
        self.source = source

    def emit(self, text: Any) -> None:
        """**覆盖基类的 ANSI 转换**：直接交付带 ``§`` 与按钮记号的原始文本。

        基类会把 ``§`` 转成 ANSI、把按钮记号折叠成 ``[标签]``（那是终端需要的形态）；
        进了游戏/控制台要走 :func:`_to_rtext`，所以这里必须保留原始标记。
        """
        self._reply(text)

    def show_help(self) -> None:
        """游戏内用的帮助：命令文案与 shell 版一致，但每条命令都是**可点按钮**。

        覆盖基类（基类用的是不带前缀的终端版帮助），所以 ``!!autosync help``
        在游戏里给的是 :func:`help_box`。
        """
        self._reply(help_box())

    def show_status(self) -> None:
        """状态面板：核心状态 + 配置文件/数据目录 + 一排快捷按钮。"""
        self._reply(status_lines(self.config_path))

    def _reply(self, text: Any) -> None:
        # ``out`` 回调与 ``emit`` 都汇到这里；``reply_lines`` 负责逐行 + RText 化
        reply_lines(self.source, [text] if isinstance(text, str) else text)


def run_command(source: CommandSource, line: str) -> bool:
    """执行一行 AutoSync 口令（``status`` / ``build refresh`` / ``deps fix 1a`` ...）。"""
    if _core is None:
        reply_lines(source, [theme.marked(theme.MARK_BAD, "AutoSync", "插件未加载成功，请查看 MCDR 控制台日志")])
        return False
    shell = SourceShell(core=_core, source=source, config_path=_config_path)
    try:
        return shell.execute(line)
    except Exception as exc:  # noqa: BLE001 - 单条命令失败不该让 MCDR 报错堆栈
        _logger.exception("%s命令 %s 执行失败：%r", LOG_PREFIX, line, exc)
        reply_lines(source, [theme.marked(theme.MARK_BAD, "命令失败", repr(exc))])
        return False


# --------------------------------------------------------------------------- 帮助
#: 帮助里的按钮悬停说明
HELP_TOOLTIPS: Dict[str, str] = {
    "status": "查看文件数 / 清单版本 / MSFP 端口与连接统计（只读）",
    "build": "重建客户端清单与测速文件（后台线程，会重写 manifest.json）",
    "build refresh": "忽略 Modrinth 缓存重新查询后重建（更慢，请求更多）",
    "reload": "重载 config.json 并重启 MSFP 服务与定时轮询",
    "tcp start|stop|restart": "控制 MSFP（裸 TCP）分发服务：start 启动 / stop 停止 / restart 重启",
    "classify": "干跑分析纯客户端/双端/纯服务端/待定（只读，不改动任何文件）",
    "classify apply": "按上次 classify 结果搬运：会改动磁盘（双端复制、纯服务端按配置复制或移动）",
    "deps": "依赖检查：只读扫描 jar 元数据（含 JiJ 嵌套层），列出真缺失前置 / JiJ 已提供 / 无法解析",
    "deps fix": "列出缺失前置编号清单与候选版本（只分析，不下载任何文件）",
    "deps fix 1a 2a": "只下载编号 1a、2a 这两项（会联网下载并写入 <dist_dir>/mods/）",
    "deps fix apply": "全选推荐候选（每项取 a）并真正下载到 <dist_dir>/mods/（会联网下载并写入文件）",
    "help": "显示本帮助（每条命令都可点击直接执行）",
}

#: 有副作用的命令用红色按钮
DANGER_COMMANDS = ("deps fix apply", "classify apply")
#: 会联网下载的命令用黄色按钮
WARN_COMMANDS = ("deps fix 1a 2a", "deps fix")


def help_buttons() -> List[str]:
    """``!!autosync help`` 的快捷按钮行（**游戏内可点**，控制台显示为 ``[标签]``）。"""
    rows = ("status", "build", "build refresh", "tcp restart", "classify", "deps", "deps fix", "help")
    buttons = []
    for command in rows:
        if command in DANGER_COMMANDS:
            color = theme.BUTTON_DANGER
        elif command in WARN_COMMANDS:
            color = theme.BUTTON_WARN
        elif command == "help":
            color = theme.BUTTON_OK
        else:
            color = theme.BUTTON_SAFE
        tooltip = HELP_TOOLTIPS.get(command, HELP_TOOLTIPS.get("tcp start|stop|restart", ""))
        buttons.append(theme.button(command, "{} {}".format(PREFIX, command), tooltip, color))
    return buttons


def help_box() -> List[str]:
    """MCDR 版帮助：分组说明 + 底部一排可点按钮（文案与 shell 版一致，只多出前缀）。"""
    #: 命令列宽度（按终端显示宽度对齐，中文算 2）
    command_width = 26
    rows: List[Tuple[str, str]] = [
        ("基础", ""),
        (PREFIX, "显示本帮助（不带参数等同 help）"),
        ("{} status".format(PREFIX), "查看文件数 / 清单版本 / MSFP 端口与连接统计"),
        ("{} build".format(PREFIX), "重建客户端清单与测速文件"),
        ("{} build refresh".format(PREFIX), "忽略 Modrinth 缓存强制重查后重建"),
        ("{} reload".format(PREFIX), "重载 config.json 并重启 MSFP 服务与定时轮询"),
        ("{} tcp start|stop|restart".format(PREFIX), "控制 MSFP（裸 TCP）分发服务"),
        ("模组分类", ""),
        ("{} classify".format(PREFIX), "干跑：纯客户端 / 双端 / 纯服务端 / 待定（不改动文件）"),
        ("{} classify apply".format(PREFIX), "按上次结果搬运（双端复制；纯服务端默认也只复制）"),
        ("依赖检查与缺失前置", ""),
        ("{} deps".format(PREFIX), "只读扫描 jar 元数据（含 JiJ 嵌套层）"),
        ("{} deps fix".format(PREFIX), "列出缺失前置编号与候选版本（不下载任何文件）"),
        ("{} deps fix 1a 2a".format(PREFIX), "只下载指定编号的前置"),
        ("{} deps fix apply".format(PREFIX), "全选推荐候选并下载到 <dist_dir>/mods/"),
        ("权限与其它", ""),
        ("", "需要权限等级 {level}：build / reload / tcp 控制 / classify apply / deps fix 下载".format(
            level=REQUIRE_HELPER_LEVEL
        )),
    ]
    lines: List[str] = [
        theme.hint("用法：{} <子命令>（{} 是等价别名；控制台也可用 {}）".format(PREFIX, PREFIX_ALIAS, PREFIX[2:])),
    ]
    for command, note in rows:
        if not command and note:
            lines.append("  §7  └ {}".format(note))
            continue
        if not note:
            lines.append(theme.group(command))
            continue
        pad = " " * max(2, command_width - theme.display_width(command))
        lines.append("  §f{cmd}{pad}§7{note}".format(cmd=command, pad=pad, note=note))
    lines.append(theme.group("快捷按钮（游戏内可点，点击后按回车执行）"))
    lines.append(theme.button_line(*help_buttons()))
    return theme.box("AutoSync 帮助 v{}".format(__version__), lines)


def status_lines(config_path: Optional[Path] = None) -> List[str]:
    """``!!autosync status`` 的正文：核心状态 + 配置文件/数据目录 + 快捷按钮。"""
    lines: List[str] = []
    if _core is not None:
        lines.extend(_core.status_lines())
    lines.append(theme.kv("配置文件", str(config_path or _config_path) if (config_path or _config_path) else "(内置默认值)"))
    lines.append(theme.kv("数据目录", str(_data_dir) if _data_dir else "(未设置)"))
    lines.append(theme.kv("更多命令", "§f{} help §7（列出全部命令）".format(PREFIX)))
    lines.append(theme.button_line(*help_buttons()))
    return lines


def status_box(source: CommandSource) -> None:
    """``!!autosync status``：核心状态 + 快捷按钮（复用 ``core.status_lines()``）。"""
    if _core is None:
        reply_lines(source, [theme.marked(theme.MARK_BAD, "AutoSync", "插件未加载成功")])
        return
    reply_lines(source, status_lines())


# --------------------------------------------------------------------------- 命令处理
def _need_helper(source: CommandSource) -> bool:
    """低权限玩家请求了需要等级 2 的操作时，回一条提示并返回 ``False``。"""
    try:
        permitted = source.has_permission(REQUIRE_HELPER_LEVEL)
    except Exception:  # noqa: BLE001 - 测试桩 / 控制台源可能没有 has_permission
        permitted = True
    if permitted:
        return True
    reply_lines(
        source,
        [theme.marked(theme.MARK_BAD, "权限不足", "该操作需要权限等级 {}".format(REQUIRE_HELPER_LEVEL))],
    )
    return False


def _stream(source: CommandSource, line: str) -> None:
    """把一行口令交给内置 shell 执行，并把输出实时回复给命令源。"""
    run_command(source, line)


def _do_build(source: CommandSource, refresh: bool) -> None:
    if not _need_helper(source):
        return
    _stream(source, "build refresh" if refresh else "build")


def _do_reload(source: CommandSource) -> None:
    """重载 ``config.json``：重建核心对象并重启 MSFP 与定时轮询。"""
    global _core
    if not _need_helper(source):
        return
    if _core is None:
        reply_lines(source, [theme.marked(theme.MARK_BAD, "AutoSync", "插件未加载成功")])
        return
    config = load_config(source.get_server())
    # 先停掉旧服务与轮询，再换配置，避免旧端口/旧线程残留
    try:
        _core.stop_watcher()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("%s停止定时轮询异常：%r", LOG_PREFIX, exc)
    try:
        _core.stop_tcp()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("%s停止 MSFP 服务异常：%r", LOG_PREFIX, exc)
    _core = make_core(source.get_server(), config)
    reply_lines(source, [theme.marked(theme.MARK_INFO, "配置", "已加载 {}".format(_config_path))])
    shell = SourceShell(core=_core, source=source, config_path=_config_path)
    shell.start()
    reply_lines(
        source,
        [
            theme.marked(
                theme.MARK_OK,
                "配置已重载",
                "分发目录 {} §7· MSFP {} §7· 轮询 {}s".format(
                    _core.dist_dir, _core.endpoint(), config.poll_interval_seconds
                ),
            )
        ],
    )


def _do_tcp(source: CommandSource, verb: str) -> None:
    if not _need_helper(source):
        return
    _stream(source, "tcp {}".format(verb))


def _do_classify(source: CommandSource, apply: bool) -> None:
    if apply and not _need_helper(source):
        return
    _stream(source, "classify apply" if apply else "classify")


def _do_deps(source: CommandSource) -> None:
    _stream(source, "deps")


def _do_deps_fix(source: CommandSource, selection: str) -> None:
    tokens = str(selection or "").strip()
    if not tokens:
        # 只列清单，不下载：只读
        _stream(source, "deps fix")
        return
    if not _need_helper(source):
        return
    if tokens.lower() == "apply":
        _stream(source, "deps fix apply")
        return
    # 只允许数字+字母的编号，避免把任意文本塞进 shell
    safe = [token for token in tokens.split() if re.fullmatch(r"\d+[a-z]?", token.lower())]
    if not safe:
        reply_lines(source, [theme.bad("编号格式不对：应形如 1a / 2b（先执行 {} deps fix 查看清单）".format(PREFIX))])
        return
    _stream(source, "deps fix {}".format(" ".join(safe)))


def _do_help(source: CommandSource) -> None:
    reply_lines(source, help_box())


# --------------------------------------------------------------------------- 命令树
def register_commands(server: PluginServerInterface) -> None:
    """注册 ``!!autosync``（及别名 ``!!as``）命令树。"""

    def register(root: str) -> None:
        builder = (
            Literal(root)
            .runs(lambda source: _do_help(source))
            .then(Literal("help").runs(lambda source: _do_help(source)))
            .then(Literal("status").runs(lambda source: status_box(source)))
            .then(
                Literal("build")
                .runs(lambda source: _do_build(source, False))
                .then(Literal("refresh").runs(lambda source: _do_build(source, True)))
            )
            .then(Literal("reload").runs(lambda source: _do_reload(source)))
            .then(
                Literal("tcp")
                .then(Literal("start").runs(lambda source: _do_tcp(source, "start")))
                .then(Literal("stop").runs(lambda source: _do_tcp(source, "stop")))
                .then(Literal("restart").runs(lambda source: _do_tcp(source, "restart")))
            )
            .then(
                Literal("classify")
                .runs(lambda source: _do_classify(source, False))
                .then(Literal("apply").runs(lambda source: _do_classify(source, True)))
            )
            .then(
                Literal("deps")
                .runs(lambda source: _do_deps(source))
                .then(
                    Literal("fix")
                    .runs(lambda source: _do_deps_fix(source, ""))
                    .then(Literal("apply").runs(lambda source: _do_deps_fix(source, "apply")))
                    .then(
                        GreedyText("selection").runs(
                            lambda source, context: _do_deps_fix(source, context["selection"])
                        )
                    )
                )
            )
        )
        server.register_command(builder)
        server.register_help_message(root, "AutoSync —— 客户端模组自动同步（清单构建 / MSFP 分发 / 分类 / 依赖修复）")

    register(PREFIX)
    register(PREFIX_ALIAS)


# --------------------------------------------------------------------------- 生命周期
def _start_services(server: PluginServerInterface) -> None:
    """插件加载完成后拉起 MSFP 服务与定时轮询（按配置）。"""
    if _core is None:
        return
    if not _core.config.tcp_enabled:
        server.logger.info("%s配置里 tcp_enabled=false，未启动 MSFP 服务", LOG_PREFIX)
    else:
        shell = AutoSyncShell(core=_core, config_path=_config_path, out=lambda text: log_lines(server, [text]))
        shell.start()
    if _core.config.auto_build_on_start:
        server.logger.info("%sauto_build_on_start=true，开始后台构建清单", LOG_PREFIX)
        _background_build(server)


@new_thread("AutoBuild")
def _background_build(server: PluginServerInterface) -> None:
    if _core is None:
        return
    result = _core.build(refresh_modrinth=False)
    server.logger.info(
        "%s自动构建完成：ok=%s version=%s files=%s message=%s",
        LOG_PREFIX,
        result.ok,
        result.version,
        result.file_count,
        result.message,
    )


def on_load(server: PluginServerInterface, prev_module: Any) -> None:
    """插件加载：读配置 -> 建核心 -> 注册命令 -> 拉起服务。"""
    global _core, _logger
    _logger = server.logger
    server.logger.info("%s正在加载 v%s ...", LOG_PREFIX, __version__)
    try:
        config = load_config(server)
        _core = make_core(server, config)
    except Exception as exc:  # noqa: BLE001 - 加载失败也要让 MCDR 起来，命令里会给出提示
        _core = None
        server.logger.exception("%s初始化失败：%r", LOG_PREFIX, exc)
    try:
        register_commands(server)
    except Exception as exc:  # noqa: BLE001
        server.logger.exception("%s命令注册失败：%r", LOG_PREFIX, exc)
    if _core is not None:
        server.logger.info(
            "%s已就绪：分发目录 %s · 配置 %s",
            LOG_PREFIX,
            _core.dist_dir,
            _config_path,
        )
        try:
            _start_services(server)
        except Exception as exc:  # noqa: BLE001
            server.logger.exception("%s服务启动异常：%r", LOG_PREFIX, exc)


def on_unload(server: PluginServerInterface) -> None:
    """插件卸载 / MCDR 退出：停服务与轮询，不留后台线程。"""
    if _core is None:
        return
    try:
        _core.stop_watcher()
    except Exception as exc:  # noqa: BLE001
        server.logger.warning("%s停止定时轮询异常：%r", LOG_PREFIX, exc)
    try:
        _core.stop_tcp()
    except Exception as exc:  # noqa: BLE001
        server.logger.warning("%s停止 MSFP 服务异常：%r", LOG_PREFIX, exc)
    server.logger.info("%s已卸载", LOG_PREFIX)


def on_server_startup(server: PluginServerInterface) -> None:
    """服务端启动完成后确认 MSFP 仍在跑（MCDR 重载配置后偶尔会掉）。"""
    if _core is None or not _core.config.tcp_enabled:
        return
    if _core.tcp_running:
        return
    server.logger.info("%sMSFP 未在运行，重新启动：%s", LOG_PREFIX, _core.endpoint())
    try:
        _core.start_tcp()
    except Exception as exc:  # noqa: BLE001
        server.logger.warning("%sMSFP 重启失败：%r", LOG_PREFIX, exc)


__all__ = [
    "PLUGIN_ID",
    "PREFIX",
    "PREFIX_ALIAS",
    "REQUIRE_HELPER_LEVEL",
    "on_load",
    "on_unload",
    "on_server_startup",
    "register_commands",
    "help_box",
    "run_command",
    "get_core",
    "resolve_base_dir",
]
