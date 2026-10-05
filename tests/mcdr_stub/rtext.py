"""``mcdreforged.api.rtext`` 的桩：``RColor`` / ``RAction`` / ``RText`` / ``RTextList``。

刻意实现了 ``to_json_object`` / ``set_color`` / ``c`` / ``h`` 这几个方法，
这样 ``autosync_mcdr.entry`` 会走「真实 RText」分支——测试因此能验证
**按钮记号确实被翻译成了带点击事件的节点**，而不是退化成纯文本。
"""

from __future__ import annotations

from typing import Any, Iterable, List, Optional


class _EnumValue:
    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "<{}>".format(self.name)


class RColor:
    """只列出主题用得到的颜色（值是 ``_EnumValue``，对齐真实 MCDR 的枚举用法）。"""

    black = _EnumValue("black")
    dark_blue = _EnumValue("dark_blue")
    dark_green = _EnumValue("dark_green")
    dark_aqua = _EnumValue("dark_aqua")
    dark_red = _EnumValue("dark_red")
    dark_purple = _EnumValue("dark_purple")
    gold = _EnumValue("gold")
    gray = _EnumValue("gray")
    dark_gray = _EnumValue("dark_gray")
    blue = _EnumValue("blue")
    green = _EnumValue("green")
    aqua = _EnumValue("aqua")
    red = _EnumValue("red")
    light_purple = _EnumValue("light_purple")
    yellow = _EnumValue("yellow")
    white = _EnumValue("white")
    reset = _EnumValue("reset")


class RAction:
    run_command = _EnumValue("run_command")
    suggest_command = _EnumValue("suggest_command")
    open_url = _EnumValue("open_url")
    copy_to_clipboard = _EnumValue("copy_to_clipboard")


class RTextBase:
    """所有 RText 节点的公共基类：可拼接、可序列化。"""

    def __init__(self, text: Any = "", color: Any = None) -> None:
        self.text = "" if text is None else str(text)
        self.color = color
        self.click: Optional[tuple] = None
        self.hover: Optional[str] = None
        self.extra: List["RTextBase"] = []

    # -- 链式 API（真实 MCDR 同款） ---------------------------------------
    def set_color(self, color: Any) -> "RTextBase":
        self.color = color
        return self

    def c(self, action: Any, value: Any, *args: Any, **kwargs: Any) -> "RTextBase":
        """``.c(RAction.suggest_command, '!!autosync status')``。"""
        self.click = (action, str(value))
        return self

    def h(self, text: Any) -> "RTextBase":
        self.hover = str(text)
        return self

    def append(self, other: Any) -> "RTextBase":
        self.extra.append(other if isinstance(other, RTextBase) else RText(other))
        return self

    # -- 序列化 -----------------------------------------------------------
    def to_json_object(self) -> Any:
        return self._json()

    def _json(self) -> Any:
        payload: dict = {"text": self.text}
        if self.color is not None:
            payload["color"] = getattr(self.color, "name", str(self.color))
        if self.click is not None:
            action, value = self.click
            payload["clickEvent"] = {"action": getattr(action, "name", str(action)), "value": value}
        if self.hover is not None:
            payload["hoverEvent"] = {"action": "show_text", "value": self.hover}
        if self.extra:
            payload["extra"] = [child._json() for child in self.extra]
        return payload

    def to_plain_text(self) -> str:
        return self.text + "".join(child.to_plain_text() for child in self.extra)

    def __str__(self) -> str:
        return self.to_plain_text()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "<{} '{}'>".format(type(self).__name__, self.to_plain_text())


class RText(RTextBase):
    pass


class RTextList(RTextBase):
    def __init__(self, *items: Any) -> None:
        super().__init__("")
        for item in items:
            self.extra.append(item if isinstance(item, RTextBase) else RText(item))

    @property
    def children(self) -> List[RTextBase]:
        return self.extra

    def __iter__(self) -> Iterable[RTextBase]:
        return iter(self.extra)


def rtext_join(*items: Any) -> RTextList:
    """真实 MCDR 里也有这个辅助函数（``RTextList`` 的便捷构造）。"""
    return RTextList(*items)
