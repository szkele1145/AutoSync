"""QBM 风格输出主题：彩色分隔线框 / 状态标记 / 缩进键值行。

三条输出路径（同一份 ``§`` 标记，三种落地形态）
---------------------------------------------
============================  ==========================================
:func:`to_ansi`               **终端**：``§a`` -> ``\\033[92m``（TTY 且无 ``NO_COLOR`` 时）
:func:`to_console`            **日志文件/重定向**：去色 + 按目标编码逐字符降级
:func:`strip_colors`          **纯文本**：只去色，不做编码降级
============================  ==========================================

设计要点
--------
* 只依赖标准库，CLI、shell 与测试可以照常复用。
* 每个函数只产出**单行**字符串：调用方必须逐行输出（``print`` / ``logger.info``），
  绝不能把多行拼成一个大字符串——终端与日志都会把多行折叠成一行。
* 颜色统一用 ``§`` 代码作内部标记；终端由 :func:`to_ansi` 转 ANSI，
  MC 客户端聊天栏可直接吃 ``§``，日志文件由 :func:`to_console` 去掉。
* Linux 终端是 UTF-8，``✔ ⚠ ℹ ✘`` **不降级**（只有目标编码真的编不出来时才退化为近义符号）。

可点击快捷按钮
--------------
theme 只负责把「按钮」编码成一个**可被纯文本承载**的记号：

    记号：\x00<颜色>B[<可见标签>]\x00<命令>\x01<悬停说明>\x00
    纯文本：``[<可见标签>]``（终端/日志/测试桩看到的样子，不带任何控制字符）

独立运行版已经没有 MCDR 的 RText 了，所以按钮记号在终端里一律折叠成 ``[标签]``；
保留这套记号是为了让 ``status`` / ``help`` 的输出既能给终端看，也能原样送给 MC 聊天栏
（那边再按 ``§`` 上色、按记号补点击事件）。
"""

from __future__ import annotations

import os
import re
import sys
import unicodedata
from typing import Any, Iterable, List, Optional, TextIO

__all__ = [
    "ANSI_RESET",
    "to_ansi",
    "strip_ansi",
    "ansi_color_enabled",
    "C_FRAME",
    "C_TITLE",
    "C_LABEL",
    "C_VALUE",
    "C_OK",
    "C_BAD",
    "C_WARN",
    "C_INFO",
    "C_RESET",
    "MARK_OK",
    "MARK_BAD",
    "MARK_WARN",
    "MARK_INFO",
    "BOX_WIDTH",
    "LABEL_WIDTH",
    "SEPARATOR_CHAR",
    "BUTTON_TOKEN_PREFIX",
    "BUTTON_FIELD_SEP",
    "BUTTON_RE",
    "BUTTON_SAFE",
    "BUTTON_OK",
    "BUTTON_WARN",
    "BUTTON_DANGER",
    "strip_colors",
    "to_console",
    "display_width",
    "pad_label",
    "title",
    "separator",
    "box",
    "kv",
    "marked",
    "group",
    "hint",
    "ok",
    "warn",
    "bad",
    "info",
    "color_number",
    "has_buttons",
    "button",
    "button_line",
    "button_label",
    "button_plain",
    "human_size",
]

# --------------------------------------------------------------------------- 调色板
C_FRAME = "§6"  # 金色分隔线
C_TITLE = "§b"  # 青色标题
C_LABEL = "§7"  # 灰色标签
C_VALUE = "§f"  # 白色数值
C_OK = "§a"  # 绿色：正常
C_BAD = "§c"  # 红色：异常
C_WARN = "§e"  # 黄色：需要注意
C_INFO = "§b"  # 青色：信息
C_RESET = "§r"

#: 彩色状态标记（QBM 风格）
MARK_OK = C_OK + "✔"
MARK_BAD = C_BAD + "✘"
MARK_WARN = C_WARN + "⚠"
MARK_INFO = C_INFO + "ℹ"

#: 分隔线总宽度：落在 40~50 字符之间
BOX_WIDTH = 44
#: 键值行标签对齐宽度（按显示宽度计，中文算 2）
LABEL_WIDTH = 12
SEPARATOR_CHAR = "═"

_COLOR_RE = re.compile("§.")
_EMPTY_WIDTH = 0

# --------------------------------------------------------------------------- 按钮记号
#: 按钮记号的前导控制字符（正常文本里绝不会出现，便于精确定位）
BUTTON_TOKEN_PREFIX = "\x00"
#: 按钮记号内部的分隔符：``\x00<颜色>B[标签]\x00命令\x01悬停\x00``
BUTTON_FIELD_SEP = "\x01"
#: 解析按钮记号：label=可见标签，color=颜色代码（可为空），cmd=点击执行的命令，hover=悬停说明
BUTTON_RE = re.compile(
    re.escape(BUTTON_TOKEN_PREFIX)
    + r"(?P<color>§.)?B\[(?P<label>[^\]\x00\x01]*)\]"
    + re.escape(BUTTON_TOKEN_PREFIX)
    + r"(?P<cmd>[^\x00\x01]*)\x01(?P<hover>[^\x00\x01]*)"
    + re.escape(BUTTON_TOKEN_PREFIX)
)


def _button_sub(text: Any) -> str:
    """把按钮记号折叠成纯文本 ``[标签]``（自动带上原颜色代码）。"""
    return BUTTON_RE.sub(
        lambda m: "{color}[{label}]".format(color=m.group("color") or "", label=m.group("label")),
        str(text),
    )


def strip_colors(text: Any) -> str:
    """去掉 ``§`` 颜色代码，得到可在控制台安全打印的纯文本。

    按钮记号（见模块文档）会折叠成 ``[标签]``：**控制台路径永远不会看到**
    ``\\x00`` / ``\\x01`` 这些控制字符，也不会有任何点击语义。
    """
    return _COLOR_RE.sub("", _button_sub(text))


# --------------------------------------------------------------------------- ANSI（终端）
#: ``§`` 代码 -> ANSI SGR 转义。用的是亮色系（90-97），在黑色/深色终端上都看得清。
_ANSI_TABLE = {
    "§0": "\033[30m",
    "§1": "\033[34m",
    "§2": "\033[32m",
    "§3": "\033[36m",
    "§4": "\033[31m",
    "§5": "\033[35m",
    "§6": "\033[33m",
    "§7": "\033[37m",
    "§8": "\033[90m",
    "§9": "\033[94m",
    "§a": "\033[92m",
    "§b": "\033[96m",
    "§c": "\033[91m",
    "§d": "\033[95m",
    "§e": "\033[93m",
    "§f": "\033[97m",
    "§r": "\033[0m",
    "§l": "\033[1m",
    "§o": "\033[3m",
    "§n": "\033[4m",
    "§m": "\033[9m",
}
#: 收尾复位：一行结束时补上，避免颜色渗到下一行（尤其是被 readline 重绘的提示符）
ANSI_RESET = "\033[0m"

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def strip_ansi(text: Any) -> str:
    """去掉 ANSI 转义序列（只去转义，不动 ``§`` 代码）。"""
    return _ANSI_RE.sub("", str(text))


def ansi_color_enabled(stream: Optional[TextIO] = None) -> bool:
    """当前是否该给终端上色：``NO_COLOR`` 存在 或 stdout 不是 TTY 时一律去色。

    遵循 https://no-color.org/ ：只要设置了 ``NO_COLOR``（哪怕值是空串）就不再上色。
    """
    if os.environ.get("NO_COLOR") is not None:
        return False
    target = sys.stdout if stream is None else stream
    try:
        return bool(target.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def to_ansi(text: Any, color: Optional[bool] = None, stream: Optional[TextIO] = None) -> str:
    """终端用文本：按钮记号折叠成 ``[标签]``，``§x`` 转成 ANSI 转义。

    ``color=None`` 时按 :func:`ansi_color_enabled` 自动判断（``NO_COLOR`` / 非 TTY 去色）；
    显式传 ``True`` / ``False`` 可强制（测试与需要固定输出的场合）。

    **Linux 终端是 UTF-8，``✔ ⚠ ℹ ✘`` 不降级**：这里只在目标编码真的编不出来时
    才退化成近义符号（老 Windows 代码页场景），UTF-8 下是恒等变换。
    """
    plain = strip_ansi(_button_sub(text))
    enabled = ansi_color_enabled(stream) if color is None else bool(color)
    if not enabled:
        # 去色模式：连 ``§`` 代码一起去掉，输出是可直接比较的纯文本
        return _degrade(strip_colors(text), stream)
    colored = _COLOR_RE.sub(lambda match: _ANSI_TABLE.get(match.group(0).lower(), ""), plain)
    if "\033" not in colored:
        return _degrade(colored, stream)
    return _degrade(colored, stream) + ANSI_RESET


#: 控制台编码不支持时的降级表：优先换成 GBK/CP936 里**画得出来**的近义符号，
#: 这样老代码页下的控制台依旧能看出「成功/失败/警告/信息」四种状态。
_CONSOLE_FALLBACKS = {
    "✔": "√",
    "✘": "×",
    "⚠": "▲",
    "ℹ": "●",
    "═": "=",
    "·": "-",
    "…": "...",
    "→": "->",
    "｜": "|",
    "└": "\\-",
}


def _degrade(text: str, stream: Optional[TextIO] = None) -> str:
    """按目标流的编码**逐字符**降级：只换真正编不出来的字符（UTF-8 下恒等）。

    Windows 老代码页（GBK/CP936）下直接写 ``✔`` / ``ℹ`` 会让 ``logging`` 抛
    ``UnicodeEncodeError``；这里把编不出的字符换成近义符号，保证既不报错、也不整行乱码。
    """
    encoding = getattr(sys.stdout if stream is None else stream, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
        return text
    except (UnicodeEncodeError, LookupError):
        pass
    result = []
    for char in text:
        try:
            char.encode(encoding)
        except (UnicodeEncodeError, LookupError):
            result.append(_CONSOLE_FALLBACKS.get(char, "?"))
        else:
            result.append(char)
    return "".join(result)


def to_console(text: Any) -> str:
    """控制台/日志文件安全文本：去色 + 按当前终端编码逐字符降级。

    ``§`` 与按钮记号都会被折叠成可读纯文本（``[标签]``），返回的字符串可以安全丢给
    ``logging`` / 重定向文件。
    """
    return _degrade(strip_colors(text))


def display_width(text: Any) -> int:
    """按终端显示宽度计数：全角/中日韩字符算 2，其余算 1（颜色代码不计）。"""
    width = 0
    for char in strip_colors(text):
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def pad_label(label: Any, width: int = LABEL_WIDTH) -> str:
    """按显示宽度右侧补空格，让键值行的值列对齐（全角/半角混排也不会错位）。"""
    text = str(label)
    return text + _pad_spaces(text, width)


def _pad_spaces(label: Any, width: int = LABEL_WIDTH) -> str:
    """只返回对齐用的空格串（标签本身由调用方输出，避免标签重复）。"""
    return " " * max(1, int(width) - display_width(label))


def title(text: Any, width: int = BOX_WIDTH) -> str:
    """``§6══════ §b标题 §6══════``：带颜色的分隔线包住标题。"""
    label = str(text)
    inner_width = display_width(label) + 2  # 标题两侧各一个空格
    remaining = max(4, int(width) - inner_width)
    left = remaining // 2
    right = remaining - left
    return "{frame}{left} {title}{label} {frame}{right}".format(
        frame=C_FRAME,
        left=SEPARATOR_CHAR * left,
        title=C_TITLE,
        label=label,
        right=SEPARATOR_CHAR * right,
    )


def separator(width: int = BOX_WIDTH) -> str:
    """整条彩色分隔线。"""
    return C_FRAME + SEPARATOR_CHAR * max(4, int(width))


def box(heading: Any, body: Iterable[Any] = (), width: int = BOX_WIDTH) -> List[str]:
    """把若干行包进一个框：标题线 + 正文 + 底部分隔线（逐行返回）。"""
    lines = [title(heading, width)]
    lines.extend(str(item) for item in body)
    lines.append(separator(width))
    return lines


def kv(label: Any, value: Any, label_width: int = LABEL_WIDTH, indent: str = "  ") -> str:
    """缩进的键值行：``§7  <标签>§f<值>``（标签按显示宽度对齐）。"""
    text = strip_colors(label)
    return "{indent}{label}{pad}{value}{text}".format(
        indent=C_LABEL + indent,
        label=text,
        pad=_pad_spaces(text, label_width),
        value=C_VALUE,
        text=value,
    )


def marked(mark: str, label: Any, value: Any, label_width: int = LABEL_WIDTH) -> str:
    """带彩色状态标记的键值行：``§a✔ §r<标签>  §f<值>``。"""
    text = strip_colors(label)
    return "{mark} {reset}{label}{pad}{value}{text}".format(
        mark=mark,
        reset=C_RESET,
        label=text,
        pad=_pad_spaces(text, label_width),
        value=C_VALUE,
        text=value,
    )


def group(text: Any) -> str:
    """分组小标题：``§b[基础]``。"""
    return "{color}[{text}]{reset}".format(color=C_TITLE, text=strip_colors(text), reset=C_RESET)


def hint(text: Any) -> str:
    """灰色说明行（含按钮记号时折叠成可读的 ``[标签]``）。"""
    return C_LABEL + button_plain(text)


def ok(text: Any) -> str:
    return C_OK + str(text)


def warn(text: Any) -> str:
    return C_WARN + str(text)


def bad(text: Any) -> str:
    return C_BAD + str(text)


def info(text: Any) -> str:
    return C_INFO + str(text)


# --------------------------------------------------------------------------- 按钮
#: 只读类按钮（重建清单 / 依赖检查 / 查看状态）：金色，与正文颜色区分开
BUTTON_SAFE = C_FRAME
#: 正常类按钮（帮助）：绿色
BUTTON_OK = C_OK
#: 有副作用的按钮（会改文件 / 会下载）：黄色
BUTTON_WARN = C_WARN
#: 危险按钮（真正写盘、批量下载）：红色
BUTTON_DANGER = C_BAD

_BUTTON_CHARS = (BUTTON_TOKEN_PREFIX, BUTTON_FIELD_SEP)


def button(label: Any, cmd: Any, hover: Any = "", color: str = BUTTON_SAFE) -> str:
    """构造一个可点击按钮的**记号**（游戏内可点，控制台显示为 ``[标签]``）。

    记号形如 ``\\x00§6B[标签]\\x00命令\\x01悬停\\x00``；终端路径（:func:`to_ansi`）
    会把它折叠成 ``[标签]``，原样送去 MC 聊天栏时则由那边补上点击/悬停事件。

    ``label`` / ``cmd`` / ``hover`` 里的控制字符会被清掉，避免破坏记号本身；
    ``label`` / ``hover`` 里即使混进 ``§`` 代码也会先剥离（颜色一律由 ``color`` 参数决定），
    保证按钮行的配色可控。
    """
    clean_label = "".join(ch for ch in strip_colors(label) if ch not in _BUTTON_CHARS)
    clean_cmd = "".join(ch for ch in str(cmd) if ch not in _BUTTON_CHARS and ch != "]")
    clean_hover = "".join(ch for ch in strip_colors(hover) if ch not in _BUTTON_CHARS)
    safe_color = color if color in (C_FRAME, C_OK, C_WARN, C_BAD, C_LABEL, C_VALUE, C_INFO) else BUTTON_SAFE
    return "{p}{color}B[{label}]{p}{cmd}{sep}{hover}{p}".format(
        p=BUTTON_TOKEN_PREFIX,
        color=safe_color,
        label=clean_label,
        cmd=clean_cmd,
        sep=BUTTON_FIELD_SEP,
        hover=clean_hover,
    )


def has_buttons(text: Any) -> bool:
    """文本里是否含有按钮记号（供调用方判断要不要特殊处理）。"""
    return BUTTON_TOKEN_PREFIX in str(text)


def button_line(*buttons: Any) -> str:
    """把若干按钮拼成**独占一行**的按钮行：``[A] [B] [C]``。

    空按钮（``None`` / 空串）会被跳过；一个都不剩时返回空串（调用方可据此不输出该行）。
    """
    parts = [str(item) for item in buttons if item]
    return " ".join(parts)


def button_label(token: Any) -> str:
    """取出按钮的可见标签（不带颜色、不带控制字符）。"""
    match = BUTTON_RE.search(str(token))
    return match.group("label") if match else ""


def button_plain(text: Any) -> str:
    """把文本里的按钮记号替换成可见标签文本（控制台/测试桩的安全形态）。"""
    return _button_sub(text)


def color_number(value: Any, zero_is_ok: bool = True, bad_from: int = 0) -> str:
    """数字配色：0（或 ≤ ``bad_from``）绿色表示正常，有值黄色表示需要注意。

    ``zero_is_ok=False`` 时反过来：0 用黄色（例如「缺失 0 个」之外的中性计数）。
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "{}{}".format(C_VALUE, value)
    if number <= bad_from:
        return "{}{}".format(C_OK if zero_is_ok else C_WARN, number)
    return "{}{}".format(C_WARN if zero_is_ok else C_OK, number)


def human_size(num_bytes: Any) -> str:
    """把字节数渲染成 ``277.8 MB`` 这种人类可读形式。"""
    try:
        size = float(num_bytes or 0)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return "{:.0f} B".format(size) if unit == "B" else "{:.1f} {}".format(size, unit)
        size /= 1024
    return "{:.1f} TB".format(size)
