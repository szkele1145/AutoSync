"""``mcdreforged`` 测试桩：让 ``autosync_mcdr.entry`` 能在没有装 MCDR 的环境里导入。

用途
----
* 单元测试（``tests/test_mcdr_entry.py``）与 ``tools/build-mcdr.ps1`` 的冒烟检查；
* 用法：把本目录（``tests/``）加入 ``sys.path``，再 ``import mcdr_stub``。

桩覆盖了入口真正用到的部分：命令树（``Literal`` / ``GreedyText``）、
``RText`` / ``RTextList`` / ``RColor`` / ``RAction``、``PluginServerInterface``、
``CommandSource``。真实 MCDR 的行为（tellraw 序列化、权限判定、后台线程调度）
不在桩的范围内，所以桩只用于「能导入 / 能注册 / 能出文本」这类结构性验证。
"""

from __future__ import annotations

from .install import install, installed

__all__ = ["install", "installed"]
