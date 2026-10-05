"""AutoSync —— 客户端模组分发清单生成 + MSFP（裸 TCP）文件分发服务 + 交互式 shell。

**独立 Python 程序：纯标准库，不依赖 mcdreforged**，目标环境 Python 3.11+（Ubuntu 24 / 3.12）。

模块划分
--------
* :mod:`autosync.config`      配置对象（默认 ``./config.json``，缺失时自动生成；含旧 ``http_*`` 键迁移）
* :mod:`autosync.scanner`     分发目录扫描 + SHA-256/SHA-1
* :mod:`autosync.modrinth`    Modrinth ``/v2/version_files`` 批量查询
* :mod:`autosync.manifest`    清单结构 / 内容指纹 / 版本号 / 原子写入
* :mod:`autosync.speedtest`   确定性生成并复用 512KB ``speedtest.bin``
* :mod:`autosync.tcp_server`  MSFP v1 服务端（多线程、keep-alive、路径规范化）
* :mod:`autosync.tcp_client`  MSFP v1 客户端（自检、验收、多线程分块下载）
* :mod:`autosync.builder`     构建编排、状态与缓存、定时轮询
* :mod:`autosync.theme`       QBM 风格输出主题（``§`` 标记 -> 终端 ANSI / 日志纯文本）
* :mod:`autosync.classify`    模组分类与搬运（纯客户端/双端/纯服务端/待定 -> server/mods）
* :mod:`autosync.deps`        依赖检查（解析 jar 元数据含 JiJ 嵌套层）
* :mod:`autosync.deps_fix`    缺失前置自动下载（Modrinth 优先，CurseForge/CFWidget 兜底）
* :mod:`autosync.curseforge`  CurseForge 官方 API / CFWidget 查询
* :mod:`autosync.shell`       交互式 REPL（``AutoSync> ``，指令不带前缀）
* :mod:`autosync.__main__`    命令行入口：shell / build / status / serve / check / classify / deps / deps-fix
"""

from __future__ import annotations

#: 程序版本号
__version__ = "1.0.0"
__all__ = ["__version__"]
