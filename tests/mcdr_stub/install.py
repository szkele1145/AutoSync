"""把本桩注册成 ``mcdreforged`` 包（仅当环境里真的没有 MCDR 时才生效）。

用法::

    import mcdr_stub
    mcdr_stub.install()          # 之后 import mcdreforged.api.all 拿到的是桩
    import autosync_mcdr.entry   # 入口可正常导入

如果当前环境已安装真实 ``mcdreforged``，``install()`` 什么也不做（返回 ``False``），
测试与构建脚本因此可以「有真用真、没真用桩」。
"""

from __future__ import annotations

import sys
import types

from . import api as _api
from . import command as _command
from . import decorator as _decorator
from . import rtext as _rtext
from . import types as _types

__all__ = ["install", "installed"]


def _module(name: str, **attrs: object) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def installed() -> bool:
    """当前 ``sys.modules`` 里挂的是不是本桩。"""
    module = sys.modules.get("mcdreforged")
    return bool(module is not None and getattr(module, "__mcdr_stub__", False))


def install(force: bool = False) -> bool:
    """注册桩模块；返回是否**本次**完成了注册。

    ``force=True`` 时无条件覆盖（测试里连续跑多个用例时用得上）。
    """
    if not force:
        try:
            import mcdreforged  # noqa: F401
        except ImportError:
            pass
        else:
            return False

    root = _module("mcdreforged")
    root.__mcdr_stub__ = True  # type: ignore[attr-defined]
    root.api = _api
    sys.modules["mcdreforged"] = root
    sys.modules["mcdreforged.api"] = _api
    sys.modules["mcdreforged.api.all"] = _api.all
    sys.modules["mcdreforged.api.command"] = _command
    sys.modules["mcdreforged.api.decorator"] = _decorator
    sys.modules["mcdreforged.api.rtext"] = _rtext
    sys.modules["mcdreforged.api.types"] = _types
    return True
