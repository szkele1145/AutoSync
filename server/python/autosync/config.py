"""AutoSync 配置对象。

本模块只用标准库，配置是一份纯 JSON 文件（默认 ``./config.json``），
独立程序、CLI、shell 与测试都能直接用。
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_CONFIG_NAME",
    "AutoSyncConfig",
    "resolve_path",
    "ensure_config_file",
    "save_config_file",
    "load_config_file",
]

#: 默认配置文件名（相对**当前工作目录**，可用 ``--config`` 指定别的路径）
DEFAULT_CONFIG_NAME = "config.json"

#: 配置默认值；同时用于「配置文件不存在时自动生成的默认配置」
DEFAULT_CONFIG: Dict[str, Any] = {
    # 客户端分发目录。相对路径基准见 base_dir
    "dist_dir": "client-dist",
    # 相对路径基准：
    #   ""          -> 自动（当前工作目录，即 config.json 所在目录）
    #   "mcdr_root" -> 兼容 MCDR 时期的旧值，等价于当前工作目录
    #   其它绝对/相对路径 -> 直接使用 / 相对当前工作目录解析
    "base_dir": "",
    # MSFP（裸 TCP）文件分发服务。阿里云大陆节点会拦 HTTP，所以不使用 HTTP。
    "tcp_host": "0.0.0.0",
    "tcp_port": 8123,
    "tcp_enabled": True,
    # 连接空闲多少秒后服务端主动断开（协议规定 30）
    "tcp_idle_timeout_seconds": 30,
    # urls 顺序：False = 自托管优先（默认，中国直连 Modrinth CDN 很慢）
    "prefer_third_party": False,
    # 启动时自动构建
    "auto_build_on_start": False,
    # 定时检测重建（秒），0 = 关闭。只在文件快照变化时真正重新哈希
    "poll_interval_seconds": 0,
    # Manifest
    "manifest_name": "manifest.json",
    # 测速文件：客户端会用 <更新源根>/speedtest.bin 做多源并发测速
    "speedtest_enabled": True,
    "speedtest_name": "speedtest.bin",
    "speedtest_size": 524288,  # 512 KiB
    # 自托管地址在 urls 里的前缀。MSFP 要求"相对分发目录根的路径"，所以默认空：
    # urls 里直接写 mods/xxx.jar，客户端拿到后发 GET 0 -1 mods/xxx.jar。
    # 若改用 HTTP 静态服务（不推荐，见 README），可设为 "files" 并打开 url_encode_paths。
    "hosted_url_prefix": "",
    # 自托管 URL 是否做 percent-encoding。MSFP 的行协议本身支持空格/中文/方括号，
    # 所以默认 False：编码反而会让服务端找不到文件。
    "url_encode_paths": False,
    # 不被纳入分发的文件名（glob，匹配相对路径或文件名）
    "exclude_globs": [
        "manifest.json",
        "manifest.json.*",
        "*.tmp",
        "*.part",
        "*.bak",
        "*.downloading",
    ],
    # 只有这些后缀的文件才会去 Modrinth 查询（其余仍会进清单，只是仅自托管）
    "modrinth_extensions": [".jar"],
    # Modrinth API
    "modrinth_api_base": "https://api.modrinth.com/v2",
    "modrinth_user_agent": "CSMC-AutoSync/1.0 (+https://github.com/)",
    "modrinth_batch_size": 100,
    "modrinth_timeout_seconds": 20,
    "modrinth_max_retries": 3,
    # 缓存有效期（小时）。0 = 永不过期（只在新文件出现时查询）
    "modrinth_cache_hours": 0,
    # 分发目录里扫不到任何文件时是否仍然生成空清单。
    # 默认 False：目录配错时宁可报错，也不要生成 "全部删除" 的清单把客户端 mods 清空。
    "allow_empty_dist": False,
    # ---- 模组分类与搬运（shell: classify / classify apply）----
    # 是否启用分类命令；false 时 shell 的 classify 会提示已禁用
    "classify_enabled": True,
    # 服务端 mods 目录：相对 <dist_dir>（即 client-dist）解析，"../mods" 就是 <server>/mods
    "classify_server_mods_dir": "../mods",
    # 搬运前是否把将被移动/删除的原文件备份到 <数据目录>/classify-backup/<时间戳>/
    "classify_backup": True,
    # 无法判定（Modrinth 查不到且 TOML 也判不出）的模组怎么处理：
    #   "both" -> 按双端处理（留下 + 复制到服务端，推断，报告里单独标注）
    #   "none" -> 原地不动，只列进待定清单
    "classify_unknown_as": "both",
    # 判为「纯服务端」的模组是否从 client-dist/mods **移走**（默认 false = 只复制、不移走）。
    # 为什么默认 false：Modrinth 的 client_side 由 mod 作者自填、经常不准，
    # 曾经把被 18 个 mod 依赖的核心前置 sable 误判为纯服务端并移走，直接导致客户端
    # 报 Missing or unsupported mandatory dependencies 崩游戏。
    # 多发给客户端一个纯服务端 mod 顶多是启动警告，漏掉一个前置就是崩游戏 —— 代价不对等。
    "classify_move_pure_server": False,
    # 配套工具 ModSideDetector 生成的 side-report.json：它逐个 jar 给出 client/server/both/unknown
    # 的侧别判定与置信度（综合 Modrinth + mcmod + 启发式），比 Modrinth 里 mod 作者自填的
    # client_side / server_side 更可信。classify 会**优先**采用它，命中后**不再查 Modrinth**
    # （省请求、更准）；未命中的条目照旧走原有的 Modrinth -> TOML 兜底流程。
    #   ""                      -> 自动探测 <dist_dir>/mods/side-report.json（存在才用；
    #                              文件不存在 / 读取失败时行为与本功能引入前完全一致）
    #   "none"/"off"/"disabled" -> 关闭该功能（行为完全等同改造前）
    #   其它（相对路径）        -> 相对 <dist_dir> 解析，例如 "mods/side-report.json"
    #   绝对路径                -> 直接使用
    "classify_side_report": "",
    # ---- ModSideDetector 网络上报（MSFP REPORT 命令）----
    # 配套工具 ModSideDetector 能用 MSFP 的 REPORT 命令把 side-report.json 直接推上来，
    # 省掉人工拷贝（工具与服务端不在同一台机器时尤其有用）。它**不新开端口、不引入
    # HTTP**：复用 tcp_host / tcp_port 与同一个 accept 循环（阿里云大陆节点会按 Host 头
    # 拦 HTTP，只有裸 TCP 通）。
    #   REPORT <token> <length>\n + length 字节 JSON  ->  OK <条目数> / ERR <原因>
    # 开关默认 **false**（关闭）：关闭时 REPORT 一律回 ERR disabled，行为与改造前一致。
    "side_report_enabled": False,
    # 共享令牌（单行、不含空格）。**为空时即使开关打开也一律回 ERR disabled**：
    # 绝不允许「没配令牌就能往磁盘写文件」。比较用 hmac.compare_digest（防时序侧信道）。
    "side_report_token": "",
    # 报告落盘路径：留空 = <data_dir>/side-report.json；相对路径相对 data_dir 解析。
    # classify 在 classify_side_report 留空（自动探测）且 <dist_dir>/mods/side-report.json
    # 不存在时，会回退到这里 —— 所以默认值下「上报完就能被 classify 读到」。
    "side_report_path": "",
    # ---- 依赖检查（shell: deps）----
    # build 完成后是否自动跑一次依赖检查（只读、不阻断 build；有缺失前置就醒目告警）
    "deps_check_after_build": False,
    # ---- 自动下载缺失前置（shell: deps fix）----
    # 是否启用 deps fix / deps fix apply 子命令
    "deps_fix_enabled": True,
    # 目标环境：选版时用于筛选 Modrinth 版本
    "deps_fix_game_version": "1.21.1",
    "deps_fix_loader": "neoforge",
    # 永不自动下载的 modId（互斥场景留后路），比较时小写
    "deps_fix_exclude": [],
    # 单个文件大小上限（MB），超过则不下并报告，避免误下大包；0 = 不限制
    "deps_fix_max_size_mb": 50,
    # 清单里是否列出预发布候选：true = 正式版(a)/测试版(b)/早期测试版(c) 各列一个；
    # false = 只列正式版（只有 (a)）
    "deps_fix_show_prerelease": True,
    # ---- CurseForge 兜底下载源（shell: deps fix）----
    # Modrinth 定位失败/无匹配版本时改试 CurseForge。
    # 官方 API v1 强制 x-api-key，所以这里为空时**不走官方 API**，改用免 key 的第三方源
    # CFWidget（数据可能滞后/不完整，建议还是去 https://console.curseforge.com/ 申请一个 key）。
    "curseforge_api_key": "",
    "curseforge_api_base": "https://api.curseforge.com",
    # 免 key 第三方数据源 CFWidget：官方 API 之外的兜底（第三方数据可能滞后，可用 false 关掉）
    "curseforge_cfwidget_enabled": True,
    "curseforge_cfwidget_api_base": "https://api.cfwidget.com",
    # 出站 HTTP 代理（对 Modrinth / CurseForge 的所有请求与下载生效）。空 = 直连。
    # 只支持 http://host:port（Python 标准库不支持 SOCKS；socks5h:// 会被忽略并告警）。
    # 设了它就走代理且**忽略系统环境变量代理**，避免服务器上残留的 HTTP_PROXY 影响行为。
    "http_proxy": "",
}


def resolve_path(base: Path, value: str) -> Path:
    """把配置里的路径解析成绝对路径。"""
    p = Path(os.path.expanduser(str(value)))
    if p.is_absolute():
        return p
    return (base / p)


@dataclass
class AutoSyncConfig:
    dist_dir: str = "client-dist"
    base_dir: str = ""
    tcp_host: str = "0.0.0.0"
    tcp_port: int = 8123
    tcp_enabled: bool = True
    tcp_idle_timeout_seconds: int = 30
    prefer_third_party: bool = False
    auto_build_on_start: bool = False
    poll_interval_seconds: int = 0
    manifest_name: str = "manifest.json"
    speedtest_enabled: bool = True
    speedtest_name: str = "speedtest.bin"
    speedtest_size: int = 524288
    hosted_url_prefix: str = ""
    url_encode_paths: bool = False
    exclude_globs: List[str] = field(default_factory=lambda: list(DEFAULT_CONFIG["exclude_globs"]))
    modrinth_extensions: List[str] = field(default_factory=lambda: list(DEFAULT_CONFIG["modrinth_extensions"]))
    modrinth_api_base: str = "https://api.modrinth.com/v2"
    modrinth_user_agent: str = "CSMC-AutoSync/1.0 (+https://github.com/)"
    modrinth_batch_size: int = 100
    modrinth_timeout_seconds: int = 20
    modrinth_max_retries: int = 3
    modrinth_cache_hours: int = 0
    allow_empty_dist: bool = False
    classify_enabled: bool = True
    classify_server_mods_dir: str = "../mods"
    classify_backup: bool = True
    classify_unknown_as: str = "both"
    classify_move_pure_server: bool = False
    #: ModSideDetector 的 side-report.json 路径（"" = 自动探测 <dist_dir>/mods/side-report.json；
    #: "none"/"off"/"disabled" = 关闭；相对路径相对 dist_dir 解析）
    classify_side_report: str = ""
    #: MSFP REPORT 上报开关（默认关；关闭时 REPORT 一律 ERR disabled）
    side_report_enabled: bool = False
    #: 上报共享令牌（为空时即使开关打开也一律 ERR disabled）
    side_report_token: str = ""
    #: 上报落盘路径（"" = <data_dir>/side-report.json；相对路径相对 data_dir）
    side_report_path: str = ""
    deps_check_after_build: bool = False
    deps_fix_enabled: bool = True
    deps_fix_game_version: str = "1.21.1"
    deps_fix_loader: str = "neoforge"
    deps_fix_exclude: List[str] = field(default_factory=list)
    deps_fix_max_size_mb: int = 50
    deps_fix_show_prerelease: bool = True
    curseforge_api_key: str = ""
    curseforge_api_base: str = "https://api.curseforge.com"
    curseforge_cfwidget_enabled: bool = True
    curseforge_cfwidget_api_base: str = "https://api.cfwidget.com"
    http_proxy: str = ""

    # ---------------------------------------------------------------- 构造
    #: 旧版（HTTP 时期）配置键 -> 新键。早期版本会把默认值一并写进
    #: 配置文件，所以不能只看"新键是否存在"：只有新键仍是默认值时才用旧键迁移，
    #: 这样用户显式配置过 tcp_port 就不会被陈旧的 http_port 覆盖。
    _LEGACY_KEYS = {"http_host": "tcp_host", "http_port": "tcp_port", "http_enabled": "tcp_enabled"}

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "AutoSyncConfig":
        merged = copy.deepcopy(DEFAULT_CONFIG)
        if isinstance(data, dict):
            for key, value in data.items():
                if key in merged:
                    merged[key] = value
            for legacy_key, new_key in cls._LEGACY_KEYS.items():
                if legacy_key not in data:
                    continue
                if data.get(new_key, DEFAULT_CONFIG[new_key]) == DEFAULT_CONFIG[new_key]:
                    merged[new_key] = data[legacy_key]
        cfg = cls()
        for key, value in merged.items():
            setattr(cfg, key, value)
        cfg.normalize()
        return cfg

    def normalize(self) -> None:
        """类型纠偏，避免用户手改配置写错类型时整个插件崩掉。"""
        self.dist_dir = str(self.dist_dir or DEFAULT_CONFIG["dist_dir"])
        self.base_dir = str(self.base_dir or "")
        self.tcp_host = str(self.tcp_host or "0.0.0.0")
        self.tcp_port = _as_int(self.tcp_port, 8123)
        self.tcp_enabled = bool(self.tcp_enabled)
        self.tcp_idle_timeout_seconds = max(5, _as_int(self.tcp_idle_timeout_seconds, 30))
        self.prefer_third_party = bool(self.prefer_third_party)
        self.auto_build_on_start = bool(self.auto_build_on_start)
        self.poll_interval_seconds = max(0, _as_int(self.poll_interval_seconds, 0))
        self.manifest_name = str(self.manifest_name or "manifest.json")
        self.speedtest_enabled = bool(self.speedtest_enabled)
        self.speedtest_name = str(self.speedtest_name or "speedtest.bin").strip("/") or "speedtest.bin"
        self.speedtest_size = max(0, _as_int(self.speedtest_size, 524288))
        self.hosted_url_prefix = str(self.hosted_url_prefix or "").strip("/")
        self.url_encode_paths = bool(self.url_encode_paths)
        if not isinstance(self.exclude_globs, (list, tuple)):
            self.exclude_globs = list(DEFAULT_CONFIG["exclude_globs"])
        self.exclude_globs = [str(x) for x in self.exclude_globs]
        if not isinstance(self.modrinth_extensions, (list, tuple)):
            self.modrinth_extensions = list(DEFAULT_CONFIG["modrinth_extensions"])
        self.modrinth_extensions = [
            (str(x) if str(x).startswith(".") else "." + str(x)).lower()
            for x in self.modrinth_extensions
        ]
        self.modrinth_api_base = str(self.modrinth_api_base or DEFAULT_CONFIG["modrinth_api_base"]).rstrip("/")
        self.modrinth_user_agent = str(self.modrinth_user_agent or DEFAULT_CONFIG["modrinth_user_agent"])
        self.modrinth_batch_size = max(1, min(1000, _as_int(self.modrinth_batch_size, 100)))
        self.modrinth_timeout_seconds = max(1, _as_int(self.modrinth_timeout_seconds, 20))
        self.modrinth_max_retries = max(0, _as_int(self.modrinth_max_retries, 3))
        self.modrinth_cache_hours = max(0, _as_int(self.modrinth_cache_hours, 0))
        self.allow_empty_dist = bool(self.allow_empty_dist)
        self.classify_enabled = bool(self.classify_enabled)
        self.classify_server_mods_dir = str(
            self.classify_server_mods_dir or DEFAULT_CONFIG["classify_server_mods_dir"]
        )
        self.classify_backup = bool(self.classify_backup)
        self.classify_unknown_as = _as_choice(
            self.classify_unknown_as, ("both", "none"), DEFAULT_CONFIG["classify_unknown_as"]
        )
        self.classify_move_pure_server = bool(self.classify_move_pure_server)
        # side-report 路径：只 strip；"none"/"off"/"disabled" 的关闭语义由 classify 解释
        self.classify_side_report = str(self.classify_side_report or "").strip()
        # 网络上报（REPORT）：开关默认关；令牌/路径只 strip，为空时的语义由 side_report 解释
        self.side_report_enabled = bool(self.side_report_enabled)
        self.side_report_token = str(self.side_report_token or "").strip()
        self.side_report_path = str(self.side_report_path or "").strip()
        self.deps_check_after_build = bool(self.deps_check_after_build)
        self.deps_fix_enabled = bool(self.deps_fix_enabled)
        self.deps_fix_game_version = str(self.deps_fix_game_version or "1.21.1").strip() or "1.21.1"
        self.deps_fix_loader = str(self.deps_fix_loader or "neoforge").strip().lower() or "neoforge"
        if not isinstance(self.deps_fix_exclude, (list, tuple)):
            self.deps_fix_exclude = []
        self.deps_fix_exclude = [str(x).strip() for x in self.deps_fix_exclude if str(x).strip()]
        self.deps_fix_max_size_mb = max(0, _as_int(self.deps_fix_max_size_mb, 50))
        self.deps_fix_show_prerelease = bool(self.deps_fix_show_prerelease)
        # CurseForge：key 为空 = 不启用官方 API（此时由免 key 的 CFWidget 兜底）
        self.curseforge_api_key = str(self.curseforge_api_key or "").strip()
        self.curseforge_api_base = str(
            self.curseforge_api_base or DEFAULT_CONFIG["curseforge_api_base"]
        ).rstrip("/")
        self.curseforge_cfwidget_enabled = bool(self.curseforge_cfwidget_enabled)
        self.curseforge_cfwidget_api_base = str(
            self.curseforge_cfwidget_api_base or DEFAULT_CONFIG["curseforge_cfwidget_api_base"]
        ).rstrip("/")
        # 代理：只认 http(s)://；SOCKS 由 autosync.modrinth.normalize_proxy 负责告警忽略
        self.http_proxy = str(self.http_proxy or "").strip()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dist_dir": self.dist_dir,
            "base_dir": self.base_dir,
            "tcp_host": self.tcp_host,
            "tcp_port": self.tcp_port,
            "tcp_enabled": self.tcp_enabled,
            "tcp_idle_timeout_seconds": self.tcp_idle_timeout_seconds,
            "prefer_third_party": self.prefer_third_party,
            "auto_build_on_start": self.auto_build_on_start,
            "poll_interval_seconds": self.poll_interval_seconds,
            "manifest_name": self.manifest_name,
            "speedtest_enabled": self.speedtest_enabled,
            "speedtest_name": self.speedtest_name,
            "speedtest_size": self.speedtest_size,
            "hosted_url_prefix": self.hosted_url_prefix,
            "url_encode_paths": self.url_encode_paths,
            "exclude_globs": list(self.exclude_globs),
            "modrinth_extensions": list(self.modrinth_extensions),
            "modrinth_api_base": self.modrinth_api_base,
            "modrinth_user_agent": self.modrinth_user_agent,
            "modrinth_batch_size": self.modrinth_batch_size,
            "modrinth_timeout_seconds": self.modrinth_timeout_seconds,
            "modrinth_max_retries": self.modrinth_max_retries,
            "modrinth_cache_hours": self.modrinth_cache_hours,
            "allow_empty_dist": self.allow_empty_dist,
            "classify_enabled": self.classify_enabled,
            "classify_server_mods_dir": self.classify_server_mods_dir,
            "classify_backup": self.classify_backup,
            "classify_unknown_as": self.classify_unknown_as,
            "classify_move_pure_server": self.classify_move_pure_server,
            "classify_side_report": self.classify_side_report,
            "side_report_enabled": self.side_report_enabled,
            "side_report_token": self.side_report_token,
            "side_report_path": self.side_report_path,
            "deps_check_after_build": self.deps_check_after_build,
            "deps_fix_enabled": self.deps_fix_enabled,
            "deps_fix_game_version": self.deps_fix_game_version,
            "deps_fix_loader": self.deps_fix_loader,
            "deps_fix_exclude": list(self.deps_fix_exclude),
            "deps_fix_max_size_mb": self.deps_fix_max_size_mb,
            "deps_fix_show_prerelease": self.deps_fix_show_prerelease,
            "curseforge_api_key": self.curseforge_api_key,
            "curseforge_api_base": self.curseforge_api_base,
            "curseforge_cfwidget_enabled": self.curseforge_cfwidget_enabled,
            "curseforge_cfwidget_api_base": self.curseforge_cfwidget_api_base,
            "http_proxy": self.http_proxy,
        }

    # ---------------------------------------------------------------- 查询
    def wants_modrinth(self, rel_path: str) -> bool:
        lower = rel_path.lower()
        return any(lower.endswith(ext) for ext in self.modrinth_extensions)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_choice(value: Any, choices: tuple, default: str) -> str:
    """枚举型配置纠偏：取值不合法时回退默认值。"""
    text = str(value or "").strip().lower()
    return text if text in choices else default


def load_config_file(path: Path) -> AutoSyncConfig:
    """独立运行时从 JSON 读取配置；文件不存在则用默认值。"""
    data: Dict[str, Any] = {}
    if path.is_file():
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    return AutoSyncConfig.from_dict(data)


def save_config_file(path: Path, config: Union[AutoSyncConfig, Dict[str, Any]]) -> Path:
    """把配置写成 JSON（UTF-8、缩进 2、LF）。目录不存在时自动创建。"""
    payload = config.to_dict() if isinstance(config, AutoSyncConfig) else dict(config)
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    return target


def ensure_config_file(path: Path) -> bool:
    """配置文件不存在时**自动生成默认配置**；返回是否新建了文件。

    默认配置直接用 :data:`DEFAULT_CONFIG`（含 ``tcp_port=8123`` 在内的全部配置项），
    用户改完重跑（或 shell 里 ``reload``）即可生效；已存在的文件绝不覆盖。
    """
    target = Path(path)
    if target.is_file():
        return False
    save_config_file(target, DEFAULT_CONFIG)
    return True
