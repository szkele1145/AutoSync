"""``mcdreforged.api.command`` 的桩：只实现入口用到的 ``Literal`` / ``GreedyText``。"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional


class _Node:
    """命令树节点：``node.then(child)`` 挂子命令，``node.runs(handler)`` 挂回调。"""

    def __init__(self, name: str, kind: str = "literal") -> None:
        self.name = name
        self.kind = kind
        self.children: List["_Node"] = []
        self.handler: Optional[Callable] = None

    # -- 兼容真实 mcdreforged 的 ArgumentNode 接口 -------------------------
    def runs(self, handler: Callable) -> "_Node":
        self.handler = handler
        return self

    def then(self, child: "_Node") -> "_Node":
        self.children.append(child)
        return self

    def requires(self, predicate: Callable, *args: Any, **kwargs: Any) -> "_Node":  # pragma: no cover
        # 入口把权限判断放在处理函数里，所以这里只做兼容占位
        return self

    # -- 供测试遍历 -------------------------------------------------------
    def walk(self) -> List["_Node"]:
        found = [self]
        for child in self.children:
            found.extend(child.walk())
        return found

    def path_of(self, target: "_Node", prefix: str = "") -> Optional[str]:
        path = (prefix + " " + self.name).strip()
        if self is target:
            return path
        for child in self.children:
            found = child.path_of(target, path)
            if found is not None:
                return found
        return None

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "<{}({}) children={}>".format(type(self).__name__, self.name, len(self.children))


class Literal(_Node):
    def __init__(self, name: str) -> None:
        super().__init__(name, "literal")


class GreedyText(_Node):
    def __init__(self, name: str) -> None:
        super().__init__(name, "greedy")


class QuotableText(_Node):
    def __init__(self, name: str) -> None:
        super().__init__(name, "quotable")


def make_command_context(mapping: Dict[str, Any]) -> Dict[str, Any]:
    """构造一个 ``context``（真实 MCDR 是 ``CommandContext``，支持 ``context["key"]``）。"""
    return dict(mapping)
