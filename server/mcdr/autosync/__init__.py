"""AutoSync 的 MCDR 插件包（打包时 ``autosync/core/`` 是独立版核心代码的副本）。

* ``autosync.core``  —— 与 ``server/python/autosync`` **完全相同**的一套核心代码
  （由 ``tools/build-mcdr.ps1`` 同步，仓库里 ``server/mcdr/autosync/core/`` 就是它的副本）；
* ``autosync.entry`` —— MCDR 入口（命令树 / 生命周期 / RText 输出），是唯一依赖 ``mcdreforged`` 的模块。

``mcdreforged.plugin.json`` 的 ``entrypoint`` 是 ``autosync.entry``：
MCDR 要求入口点必须位于与插件 id（``autosync``）同名的包内，所以不能叫 ``autosync_mcdr``。
"""

from __future__ import annotations

from . import core

#: 程序版本号（与 server/python/autosync/__init__.py 保持一致）
__version__ = core.__version__

__all__ = ["__version__", "core", "entry"]