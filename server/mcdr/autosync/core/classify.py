"""模组分类与搬运（``classify`` / ``classify apply``）。

服主的「客户端分发目录」``<dist_dir>``（默认 ``server/client-dist``）下的 ``mods/`` 里
混了三类模组，本模块负责判定并把服务端真正需要的那部分搬到 ``<server>/mods``：

============  ==========================================  ==================================================
类型          判定依据                                    期望处理
============  ==========================================  ==================================================
纯客户端      Modrinth ``server_side == "unsupported"``   原样留在 ``client-dist/mods``
双端          ``client_side`` / ``server_side`` 都支持    留下 **并且** 复制到服务端 mods
纯服务端      Modrinth ``client_side == "unsupported"``   **默认只复制**到服务端 mods（客户端目录保留一份）
待定          Modrinth 查不到 + TOML 也判不出             按 ``classify_unknown_as`` 处理
============  ==========================================  ==================================================

**默认保守（``classify_move_pure_server = false``）**：判为纯服务端的模组只**复制**到
``server/mods``，**保留**在 ``client-dist/mods`` 里不动。理由是代价不对等：
Modrinth 的 ``client_side`` 由 mod 作者自行填写、经常不准，多发给客户端一个纯服务端 mod
顶多是启动时一条警告，而**漏掉一个前置就是客户端直接崩游戏**
（真实事故：被 18 个 mod 依赖的核心前置 ``sable`` 被误判为纯服务端并移走，NeoForge 报
``Missing or unsupported mandatory dependencies``）。想把客户端目录清干净时，
把 ``classify_move_pure_server`` 设为 ``true`` 恢复旧的「移动」语义，报告里会醒目提示风险。

**依赖保护**：只要某个 mod 被**其他 mod 的必需依赖**引用（见 :mod:`autosync.deps` 的依赖图），
它就**永远不会被移走**，即使被判定为纯服务端、即使开了 ``classify_move_pure_server``；
报告里标注「被 N 个 mod 依赖，已强制保留」。

判定优先级（配置项 ``classify_side_report``，默认自动探测 ``<dist_dir>/mods/side-report.json``）：

1. **ModSideDetector 的 ``side-report.json``**（配套工具生成，综合 Modrinth + mcmod + 启发式）：
   按 **sha1 精确匹配**优先、sha1 缺失时按文件名 ``file`` 匹配；命中即采用该结论并**跳过
   Modrinth 查询**。``confidence=low`` 或 ``needs_review`` / ``conflict`` 为 true 的条目
   **仍然采用**，但报告里标注「建议人工复核」。文件不存在 / 损坏 / 字段不合法时，行为与
   没有这个功能时**完全一致**（只多一个为 0 的统计字段）。
2. Modrinth（sha1 -> project_id -> client_side / server_side），查不到时兜底读 jar 内
   ``META-INF/neoforge.mods.toml``：

* ``clientSideOnly = true`` -> 纯客户端；
* ``displayTest = "IGNORE_ALL_VERSION"`` -> **不确定**（纯客户端和纯服务端都可能这么写，
  绝不猜），列为待定；
* ``[[dependencies]]`` 里的 ``side="CLIENT"/"SERVER"`` 只描述依赖，**不用于判定 mod 自身**。

报告透明性：判为纯服务端的条目会带上**原始**依据 —— Modrinth 来源是它返回的
``client_side`` / ``server_side`` 值与 ``project_id``，side-report 来源是 ModSideDetector 的
``side`` / ``confidence`` / ``notes`` 原文（``entry.detail`` 里完整保留，不截断），
便于人工复核（上次事故就是因为看不到原始依据）。

安全约定：
* :meth:`ClassifyService.analyze` 只读不写（除报告 JSON），绝不改动分发目录；
* :meth:`ClassifyService.apply` 只消费最近一次 analyze 的报告，报告缺失 / 过期 /
  源目录已变化时**拒绝执行**；目标同名文件一律**不覆盖**，冲突只上报；
* 待定项默认按双端处理（可配置），报告里单独标注「按双端处理（推断）」。

本模块只用标准库，方便脱离主程序单测（见 ``python -m autosync classify``）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import theme
from .config import AutoSyncConfig
from .deps import (
    DependencyGraph,
    TOML_CANDIDATES,
    build_dependency_graph,
    read_mod_toml,
    resolve_server_mods_dir,
)
from .manifest import now_iso
from .modrinth import (
    ModrinthClient,
    ModrinthHit,
    ModrinthProject,
    SIDE_UNKNOWN,
    dump_cached_hits,
    dump_cached_projects,
    is_unsupported,
    parse_cached_hits,
    parse_cached_projects,
)
from .scanner import hash_file, iter_files, snapshot_key
#: MSFP ``REPORT`` 上报的落盘位置（工具通过网络推过来的报告可能在这里）
from .side_report import resolve_side_report_path as resolve_side_report_upload_path

__all__ = [
    "CATEGORY_CLIENT_ONLY",
    "CATEGORY_BOTH",
    "CATEGORY_SERVER_ONLY",
    "CATEGORY_UNKNOWN",
    "CATEGORY_NOT_MOD",
    "SOURCE_MODRINTH",
    "SOURCE_TOML",
    "SOURCE_UNKNOWN",
    "SOURCE_SIDE_REPORT",
    "SIDE_REPORT_CATEGORY",
    "SIDE_REPORT_OFF_VALUES",
    "SideReportEntry",
    "resolve_side_report_path",
    "load_side_report",
    "ACTION_KEEP",
    "ACTION_COPY",
    "ACTION_MOVE",
    "ACTION_NONE",
    "MOVE_PURE_SERVER_WARNING",
    "ClassifyEntry",
    "ClassifyReport",
    "ApplyResult",
    "ClassifyService",
    "read_mod_toml",
    "TOML_CANDIDATES",
    "judge_toml",
    "REPORT_MAX_AGE_SECONDS",
]

# ---------------------------------------------------------------- 枚举常量
CATEGORY_CLIENT_ONLY = "client_only"
CATEGORY_BOTH = "both"
CATEGORY_SERVER_ONLY = "server_only"
CATEGORY_UNKNOWN = "unknown"
CATEGORY_NOT_MOD = "not_mod"

CATEGORY_LABELS = {
    CATEGORY_CLIENT_ONLY: "纯客户端",
    CATEGORY_BOTH: "双端",
    CATEGORY_SERVER_ONLY: "纯服务端",
    CATEGORY_UNKNOWN: "待定",
    CATEGORY_NOT_MOD: "非模组文件",
}

SOURCE_MODRINTH = "modrinth"
SOURCE_TOML = "toml"
SOURCE_UNKNOWN = "unknown"
#: 判定来自配套工具 ModSideDetector 的 ``side-report.json``（**不复用** SOURCE_MODRINTH：
#: 报告里必须一眼看出这条结论不是 Modrinth 自填的 side 字段）
SOURCE_SIDE_REPORT = "modsidedetector"

#: side-report.json 的 ``side`` -> 本模块的 category
SIDE_REPORT_CATEGORY = {
    "client": CATEGORY_CLIENT_ONLY,
    "server": CATEGORY_SERVER_ONLY,
    "both": CATEGORY_BOTH,
    "unknown": CATEGORY_UNKNOWN,
}

#: ``classify_side_report`` 里表示「关闭该功能」的取值（比较时小写）
SIDE_REPORT_OFF_VALUES = ("none", "off", "disabled")

ACTION_KEEP = "keep"
ACTION_COPY = "copy"
ACTION_MOVE = "move"
ACTION_NONE = "none"

ACTION_LABELS = {
    ACTION_KEEP: "留下",
    ACTION_COPY: "复制到 server/mods",
    ACTION_MOVE: "移到 server/mods",
    ACTION_NONE: "不动",
}

#: 目标目录预检结果的中文说明
TARGET_STATE_LABELS = {
    "absent": "目标无同名文件",
    "same": "目标已有同内容文件（会跳过）",
    "different": "目标同名文件内容不同（会冲突，不覆盖）",
    "dir": "目标同名路径是目录（会冲突）",
    "error": "目标状态无法读取",
}


def _append_note(note: str, extra: str) -> str:
    """在备注后追加一句（用「；」分隔，保持可读）。"""
    if not extra:
        return note
    return f"{note}；{extra}" if note else extra


def _brief_list(values: List[str], limit: int = 5) -> str:
    """把列表压成一行（超出部分省略），用于报告备注。"""
    items = list(values)
    if len(items) <= limit:
        return "、".join(items)
    return "、".join(items[:limit]) + f"…（共 {len(items)} 个）"

#: 最近一次 classify 报告的有效期：超时后 apply 会拒绝执行（要求重新 classify）
REPORT_MAX_AGE_SECONDS = 1800.0

#: ``classify_move_pure_server = true`` 时在干跑报告里显示的醒目风险提示
MOVE_PURE_SERVER_WARNING = (
    "!!! 风险：classify_move_pure_server=true，判为「纯服务端」的 mod 会从 "
    "client-dist/mods 移除（只留在 server/mods）!!!  Modrinth 的 client_side 由 mod 作者"
    "自填、经常不准；一旦它其实是被别的 mod 依赖的前置，客户端会直接崩游戏"
    "（Missing or unsupported mandatory dependencies）。建议保持默认 false（只复制不移走）。"
)

#: 保守模式（默认）下在干跑报告里的说明
KEEP_PURE_SERVER_NOTE = (
    "保守模式（classify_move_pure_server=false，默认）：判为「纯服务端」的 mod 只复制到 "
    "server/mods，仍保留在 client-dist/mods。理由：多发给客户端一个纯服务端 mod 顶多是"
    "启动警告，漏掉一个前置就是客户端直接崩游戏，两者代价不对等。"
)

_CLIENT_SIDE_ONLY_RE = re.compile(
    r"^\s*clientSideOnly\s*=\s*(true|false)\s*(?:#.*)?$", re.IGNORECASE | re.MULTILINE
)
_DISPLAY_TEST_RE = re.compile(r"^\s*displayTest\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE | re.MULTILINE)


# ---------------------------------------------------------------- TOML 兜底
def judge_toml(text: Optional[str]) -> Tuple[Optional[str], str]:
    """按 TOML 兜底规则判定，返回 ``(类型 或 None, 依据说明)``。

    只有两条规则：
    * ``clientSideOnly = true`` -> ``CATEGORY_CLIENT_ONLY``；
    * ``displayTest = "IGNORE_ALL_VERSION"`` -> ``CATEGORY_UNKNOWN``（明确判不了，不猜）。
    ``[[dependencies]]`` 的 ``side=`` 只描述依赖，**不参与**判定。
    """
    if not text:
        return None, ""
    match = _CLIENT_SIDE_ONLY_RE.search(text)
    if match is not None and match.group(1).lower() == "true":
        return CATEGORY_CLIENT_ONLY, "toml: clientSideOnly=true"
    match = _DISPLAY_TEST_RE.search(text)
    if match is not None and match.group(1).strip().upper() == "IGNORE_ALL_VERSION":
        return CATEGORY_UNKNOWN, 'toml: displayTest="IGNORE_ALL_VERSION"（无法区分纯客户端/纯服务端）'
    return None, ""


# ---------------------------------------------------------------- side-report 接入
#: side-report.json 里 sha1 的合法形状（40 位十六进制）
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


def _side_report_warn(logger: Any, message: str) -> None:
    """side-report 的所有异常都只记日志：这个文件是可选的，绝不能让 classify 失败。"""
    if logger is None:
        return
    log = getattr(logger, "warning", None) or getattr(logger, "info", None)
    if log is not None:
        log(message)


@dataclass
class SideReportEntry:
    """``side-report.json`` 里一条**可用**的判定（只保留判定与报告要用的字段）。"""

    file: str = ""
    sha1: str = ""
    mod_id: str = ""
    display_name: str = ""
    side: str = ""  # client / server / both / unknown
    confidence: str = ""  # high / medium / low
    notes: str = ""
    needs_review: bool = False
    conflict: bool = False

    @property
    def low_confidence(self) -> bool:
        return self.confidence.strip().lower() == "low"

    @property
    def evidence(self) -> str:
        """写进 ``entry.detail`` 的依据文字：必须带上**原始** confidence 与 notes。

        （本项目上一次事故就是因为报告里看不到原始依据，所以这里不做截断。）
        """
        confidence = self.confidence.strip() or "(未提供)"
        parts = f"side={self.side}, confidence={confidence}"
        if self.mod_id.strip():
            parts += f", mod_id={self.mod_id.strip()}"
        notes = self.notes.strip() or "(无 notes)"
        return f"modsidedetector({parts})：{notes}"

    @property
    def review_note(self) -> str:
        """低置信 / 需复核时追加到 ``entry.note`` 的提示（空串 = 不需要提示）。"""
        reasons = []
        if self.low_confidence:
            reasons.append("confidence=low")
        if self.needs_review:
            reasons.append("needs_review=true")
        if self.conflict:
            reasons.append("conflict=true")
        if not reasons:
            return ""
        return "ModSideDetector {}，建议人工复核".format("、".join(reasons))


def resolve_side_report_path(config: Any, dist_dir: Path) -> Tuple[Optional[Path], bool]:
    """算出 side-report.json 的路径，返回 ``(路径, 是否是用户显式配置的)``。

    * 配置为 ``"none"`` / ``"off"`` / ``"disabled"`` -> ``(None, False)`` = 功能关闭；
    * 配置留空 -> ``<dist_dir>/mods/side-report.json``（自动探测，不算显式配置）；
    * 相对路径 -> 相对 ``<dist_dir>`` 解析；绝对路径 -> 直接用。
    """
    raw = str(getattr(config, "classify_side_report", "") or "").strip()
    if raw.lower() in SIDE_REPORT_OFF_VALUES:
        return None, False
    if not raw:
        return Path(dist_dir) / "mods" / "side-report.json", False
    expanded = Path(os.path.expanduser(raw))
    if expanded.is_absolute():
        return expanded, True
    return Path(dist_dir) / expanded, True


def _side_report_basename(value: str) -> str:
    """取出文件名（side-report 的 ``file``/``rel_path`` 可能是带目录的路径）。"""
    return str(value or "").replace("\\", "/").rsplit("/", 1)[-1].strip().casefold()


def load_side_report(
    path: Path, logger: Any = None
) -> Tuple[Dict[str, SideReportEntry], Dict[str, SideReportEntry]]:
    """读取并解析 side-report.json，返回 ``(按 sha1 索引, 按文件名索引)``。

    容错原则：文件不存在、JSON 损坏、字段缺失、``side`` 取值不认识 —— 一律**忽略**
    （整条记录或整个文件）并记一条 warning，**绝不抛异常**，绝不让整轮 classify 失败。
    """
    by_sha1: Dict[str, SideReportEntry] = {}
    by_file: Dict[str, SideReportEntry] = {}
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except FileNotFoundError:
        # 自动探测时文件不存在是常态；显式配置却不存在由调用方给出提示
        return by_sha1, by_file
    except (OSError, ValueError) as exc:
        _side_report_warn(
            logger,
            f"side-report 读取/解析失败，已忽略该文件（照旧走 Modrinth）：{path}：{exc!r}",
        )
        return by_sha1, by_file

    mods = data.get("mods") if isinstance(data, dict) else None
    if not isinstance(mods, list):
        _side_report_warn(
            logger,
            f"side-report 顶层缺少 mods 数组，已忽略该文件（照旧走 Modrinth）：{path}",
        )
        return by_sha1, by_file

    skipped = 0
    examples: List[str] = []

    def skip(reason: str) -> None:
        nonlocal skipped
        skipped += 1
        if len(examples) < 3:
            examples.append(reason)

    for index, item in enumerate(mods):
        if not isinstance(item, dict):
            skip(f"#{index} 不是对象")
            continue
        side = str(item.get("side") or "").strip().lower()
        if side not in SIDE_REPORT_CATEGORY:
            skip(f"#{index} side={item.get('side')!r} 不是 client/server/both/unknown")
            continue
        sha1 = str(item.get("sha1") or "").strip().lower()
        if sha1 and not _SHA1_RE.match(sha1):
            sha1 = ""  # 形状不对就当没有，退回按文件名匹配
        file_name = _side_report_basename(item.get("file") or item.get("rel_path") or "")
        if not sha1 and not file_name:
            skip(f"#{index} sha1 与 file 都缺失")
            continue
        entry = SideReportEntry(
            file=str(item.get("file") or ""),
            sha1=sha1,
            mod_id=str(item.get("mod_id") or ""),
            display_name=str(item.get("display_name") or ""),
            side=side,
            confidence=str(item.get("confidence") or ""),
            notes=str(item.get("notes") or ""),
            needs_review=bool(item.get("needs_review")),
            conflict=bool(item.get("conflict")),
        )
        if sha1 and sha1 not in by_sha1:
            by_sha1[sha1] = entry
        if file_name and file_name not in by_file:
            by_file[file_name] = entry
    if skipped:
        _side_report_warn(
            logger,
            "side-report 有 {} 条记录字段缺失/取值不合法，已忽略（其余条目照常生效）：{}".format(
                skipped, "；".join(examples)
            ),
        )
    return by_sha1, by_file


# ---------------------------------------------------------------- 数据结构
@dataclass
class ClassifyEntry:
    """一个待分类文件的判定结果（只描述计划，不含任何文件改动）。"""

    rel_path: str  # 相对 dist_dir，例如 "mods/xxx.jar"
    rel_in_mods: str  # 相对 dist_dir/mods，用于在服务端 mods 里复现目录结构
    name: str
    size: int = 0
    sha1: str = ""
    sha256: str = ""
    category: str = CATEGORY_UNKNOWN
    source: str = SOURCE_UNKNOWN
    detail: str = ""
    action: str = ACTION_NONE
    project_id: str = ""
    project_title: str = ""
    note: str = ""
    #: Modrinth 返回的**原始** side 值（判定依据，便于人工复核；TOML 兜底时为空）
    client_side: str = ""
    server_side: str = ""
    #: 该 jar 在 TOML 里声明的 modId（依赖保护用）
    mod_ids: List[str] = field(default_factory=list)
    #: 该 jar 通过 JiJ 嵌套自带的 modId（这些依赖无需单独安装，也不算「依赖别人」）
    jij_mod_ids: List[str] = field(default_factory=list)
    #: 必需它的其他 mod（文件名列表）；非空 = 永远不能移走
    depended_by: List[str] = field(default_factory=list)
    #: 是否因为「被其他 mod 依赖」而强制保留了客户端副本
    forced_keep: bool = False
    #: 目标目录预检结果：absent / same / different / dir / error
    target_state: str = ""
    target_note: str = ""

    @property
    def category_label(self) -> str:
        return CATEGORY_LABELS.get(self.category, self.category)

    @property
    def action_label(self) -> str:
        return ACTION_LABELS.get(self.action, self.action)

    @property
    def has_dependents(self) -> bool:
        return bool(self.depended_by)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rel_path": self.rel_path,
            "rel_in_mods": self.rel_in_mods,
            "name": self.name,
            "size": self.size,
            "sha1": self.sha1,
            "sha256": self.sha256,
            "category": self.category,
            "category_label": self.category_label,
            "source": self.source,
            "detail": self.detail,
            "action": self.action,
            "action_label": self.action_label,
            "project_id": self.project_id,
            "project_title": self.project_title,
            "client_side": self.client_side,
            "server_side": self.server_side,
            "mod_ids": list(self.mod_ids),
            "jij_mod_ids": list(self.jij_mod_ids),
            "depended_by": list(self.depended_by),
            "forced_keep": self.forced_keep,
            "note": self.note,
            "target_state": self.target_state,
            "target_note": self.target_note,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ClassifyEntry":
        return cls(
            rel_path=str(data.get("rel_path") or ""),
            rel_in_mods=str(data.get("rel_in_mods") or ""),
            name=str(data.get("name") or ""),
            size=int(data.get("size") or 0),
            sha1=str(data.get("sha1") or ""),
            sha256=str(data.get("sha256") or ""),
            category=str(data.get("category") or CATEGORY_UNKNOWN),
            source=str(data.get("source") or SOURCE_UNKNOWN),
            detail=str(data.get("detail") or ""),
            action=str(data.get("action") or ACTION_NONE),
            project_id=str(data.get("project_id") or ""),
            project_title=str(data.get("project_title") or ""),
            note=str(data.get("note") or ""),
            client_side=str(data.get("client_side") or ""),
            server_side=str(data.get("server_side") or ""),
            mod_ids=[str(x) for x in (data.get("mod_ids") or [])],
            jij_mod_ids=[str(x) for x in (data.get("jij_mod_ids") or [])],
            depended_by=[str(x) for x in (data.get("depended_by") or [])],
            forced_keep=bool(data.get("forced_keep")),
            target_state=str(data.get("target_state") or ""),
            target_note=str(data.get("target_note") or ""),
        )


@dataclass
class ClassifyReport:
    """一次 analyze 的完整结果（也是 apply 的唯一输入）。"""

    ok: bool = True
    message: str = ""
    generated_at: str = ""
    generated_ts: float = 0.0
    source_dir: str = ""  # dist_dir（绝对路径）
    source_mods_dir: str = ""  # dist_dir/mods（绝对路径）
    target_dir: str = ""  # 服务端 mods（绝对路径）
    target_exists: bool = False
    unknown_as: str = "both"
    #: 本次分析采用的 classify_move_pure_server（apply 时用来核对配置是否被改过）
    move_pure_server: bool = False
    snapshot_key: str = ""
    counts: Dict[str, int] = field(default_factory=dict)
    entries: List[ClassifyEntry] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    modrinth_hits: int = 0
    toml_fallbacks: int = 0
    #: 判定来自 ModSideDetector 的 side-report.json 的条目数（旧报告无此字段，读回时默认 0）
    side_report_hits: int = 0
    #: 被其他 mod 必需依赖的 modId 数量（这些 mod 永远不会被移走）
    protected_mod_ids: int = 0
    elapsed: float = 0.0

    # ---------------------------------------------------------- 便捷视图
    def by_category(self, category: str) -> List[ClassifyEntry]:
        return [entry for entry in self.entries if entry.category == category]

    @property
    def unknown_entries(self) -> List[ClassifyEntry]:
        return self.by_category(CATEGORY_UNKNOWN)

    @property
    def planned_entries(self) -> List[ClassifyEntry]:
        return [entry for entry in self.entries if entry.action in (ACTION_COPY, ACTION_MOVE)]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "generated_at": self.generated_at,
            "generated_ts": self.generated_ts,
            "source_dir": self.source_dir,
            "source_mods_dir": self.source_mods_dir,
            "target_dir": self.target_dir,
            "target_exists": self.target_exists,
            "unknown_as": self.unknown_as,
            "move_pure_server": self.move_pure_server,
            "snapshot_key": self.snapshot_key,
            "counts": dict(self.counts),
            "entries": [entry.to_dict() for entry in self.entries],
            "errors": list(self.errors),
            "modrinth_hits": self.modrinth_hits,
            "toml_fallbacks": self.toml_fallbacks,
            "side_report_hits": self.side_report_hits,
            "protected_mod_ids": self.protected_mod_ids,
            "elapsed": round(self.elapsed, 3),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ClassifyReport":
        report = cls(
            ok=bool(data.get("ok", True)),
            message=str(data.get("message") or ""),
            generated_at=str(data.get("generated_at") or ""),
            generated_ts=float(data.get("generated_ts") or 0.0),
            source_dir=str(data.get("source_dir") or ""),
            source_mods_dir=str(data.get("source_mods_dir") or ""),
            target_dir=str(data.get("target_dir") or ""),
            target_exists=bool(data.get("target_exists")),
            unknown_as=str(data.get("unknown_as") or "both"),
            move_pure_server=bool(data.get("move_pure_server")),
            snapshot_key=str(data.get("snapshot_key") or ""),
            counts={str(k): int(v) for k, v in (data.get("counts") or {}).items()},
            errors=[str(x) for x in (data.get("errors") or [])],
            modrinth_hits=int(data.get("modrinth_hits") or 0),
            toml_fallbacks=int(data.get("toml_fallbacks") or 0),
            # 旧报告没有这个字段：默认 0（保证能读回改造前的报告）
            side_report_hits=int(data.get("side_report_hits") or 0),
            protected_mod_ids=int(data.get("protected_mod_ids") or 0),
            elapsed=float(data.get("elapsed") or 0.0),
        )
        report.entries = [
            ClassifyEntry.from_dict(item) for item in (data.get("entries") or []) if isinstance(item, dict)
        ]
        return report


@dataclass
class ApplyResult:
    """一次 apply 的汇总。"""

    ok: bool = False
    message: str = ""
    copied: int = 0
    moved: int = 0
    skipped: int = 0
    conflicts: int = 0
    unknown: int = 0
    cleaned: int = 0
    failed: int = 0
    backup_dir: str = ""
    actions: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    generated_at: str = ""
    elapsed: float = 0.0
    report_path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "copied": self.copied,
            "moved": self.moved,
            "skipped": self.skipped,
            "conflicts": self.conflicts,
            "unknown": self.unknown,
            "cleaned": self.cleaned,
            "failed": self.failed,
            "backup_dir": self.backup_dir,
            "actions": list(self.actions),
            "errors": list(self.errors),
            "generated_at": self.generated_at,
            "elapsed": round(self.elapsed, 3),
            "report_path": self.report_path,
        }


# ---------------------------------------------------------------- 服务
class ClassifyService:
    """分类分析 + 搬运。线程安全由调用方保证（shell 用 threading.Thread + 自身锁）。"""

    def __init__(
        self,
        config: AutoSyncConfig,
        dist_dir: Path,
        data_dir: Path,
        logger: Any = None,
        state_path: Optional[Path] = None,
        modrinth_client: Optional[ModrinthClient] = None,
    ) -> None:
        self.config = config
        self.dist_dir = Path(dist_dir)
        self.data_dir = Path(data_dir)
        self.logger = logger
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # 复用 AutoSyncCore 的 state.json 里的 Modrinth sha1 缓存（已命中的不必重查）
        self.state_path = Path(state_path) if state_path is not None else (self.data_dir / "state.json")
        self.cache_path = self.data_dir / "classify-cache.json"
        self.report_path = self.data_dir / "classify-report.json"

        self._hits: Dict[str, ModrinthHit] = {}
        self._projects: Dict[str, ModrinthProject] = {}
        self._load_cache()
        self._client = modrinth_client
        self._lock = None  # 由 entry 侧注入（可选）
        # ModSideDetector 的 side-report.json（可选来源）：默认空 = 未命中，行为与改造前一致
        self._side_report_by_sha1: Dict[str, SideReportEntry] = {}
        self._side_report_by_file: Dict[str, SideReportEntry] = {}
        self._side_report_path: Optional[Path] = None

    # -------------------------------------------------------------- 路径
    @property
    def client_mods_dir(self) -> Path:
        """客户端分发目录里的 mods/（判定与搬运的源）。"""
        return self.dist_dir / "mods"

    @property
    def server_mods_dir(self) -> Path:
        """服务端 mods 目录。

        ``classify_server_mods_dir`` 相对 ``dist_dir`` 解析：默认 ``"../mods"``
        即 ``<dist_dir 的上级>/mods``（默认布局下 = ``<MCDR根>/server/mods``）。
        也可以直接写绝对路径。
        """
        return resolve_server_mods_dir(self.config, self.dist_dir)

    # -------------------------------------------------------------- 日志
    def _log(self, level: str, message: str) -> None:
        if self.logger is None:
            return
        getattr(self.logger, level, self.logger.info)(message)

    def _make_client(self) -> ModrinthClient:
        if self._client is None:
            cfg = self.config
            self._client = ModrinthClient(
                user_agent=cfg.modrinth_user_agent,
                api_base=cfg.modrinth_api_base,
                batch_size=cfg.modrinth_batch_size,
                timeout=cfg.modrinth_timeout_seconds,
                max_retries=cfg.modrinth_max_retries,
                logger=self.logger,
                proxy=getattr(cfg, "http_proxy", ""),
            )
        return self._client

    # -------------------------------------------------------------- 缓存
    def _load_cache(self) -> None:
        raw: Dict[str, Any] = {}
        try:
            with open(self.cache_path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            if isinstance(data, dict):
                raw = data
        except (OSError, ValueError):
            raw = {}
        self._hits = parse_cached_hits(raw.get("hits"))
        self._projects = parse_cached_projects(raw.get("projects"))
        # 兜底种子：AutoSyncCore 构建时缓存的 sha1 -> 版本（同一 data 目录）
        if self.state_path.is_file():
            try:
                with open(self.state_path, "r", encoding="utf-8") as fp:
                    state = json.load(fp)
                seeded = parse_cached_hits(state.get("modrinth") if isinstance(state, dict) else None)
            except (OSError, ValueError):
                seeded = {}
            for sha1, hit in seeded.items():
                self._hits.setdefault(sha1, hit)
        self._log("info", f"分类缓存：sha1 命中 {len(self._hits)} 条，项目 {len(self._projects)} 条")

    def _save_cache(self) -> None:
        payload = {
            "hits": dump_cached_hits(self._hits.values()),
            "projects": dump_cached_projects(self._projects.values()),
        }
        tmp = self.cache_path.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, self.cache_path)
        except OSError as exc:
            self._log("warning", f"分类缓存写入失败：{exc!r}")

    def _project_expired(self, project: ModrinthProject) -> bool:
        hours = self.config.modrinth_cache_hours
        if hours <= 0 or not project.queried_at:
            return False
        return (time.time() - project.queried_at) > hours * 3600

    # -------------------------------------------------------------- side-report
    def lookup_side_report(self, entry: ClassifyEntry) -> Optional[SideReportEntry]:
        """查这个文件在 side-report 里的判定：**sha1 精确匹配优先**，缺失时按文件名兜底。"""
        sha1 = str(entry.sha1 or "").strip().lower()
        if sha1:
            hit = self._side_report_by_sha1.get(sha1)
            if hit is not None:
                return hit
        name = _side_report_basename(entry.name)
        if name:
            return self._side_report_by_file.get(name)
        return None

    def _load_side_report(self) -> None:
        """加载 side-report.json（可选来源）。

        关闭 / 文件不存在 / 解析失败时什么都不做：``_side_report_*`` 保持空，
        整轮 classify 的行为与本功能引入前**完全一致**（只是统计字段为 0）。

        ``classify_side_report`` 留空（自动探测）时多一条回退：``<dist_dir>/mods/
        side-report.json`` 不存在就再看 MSFP ``REPORT`` 的落盘位置
        （``side_report_path``，默认 ``<data_dir>/side-report.json``）—— 工具上报完
        就能直接被 classify 读到。**用户显式配置过 classify_side_report 时只认它**，
        绝不被上报路径覆盖。
        """
        path, explicit = resolve_side_report_path(self.config, self.dist_dir)
        if path is None:
            return
        if not explicit and not path.is_file():
            fallback = resolve_side_report_upload_path(self.config, self.data_dir)
            if fallback != path and fallback.is_file():
                path = fallback
        if not path.is_file():
            if explicit:
                self._log("warning", f"side-report 文件不存在，照旧走 Modrinth：{path}")
            return
        by_sha1, by_file = load_side_report(path, logger=self.logger)
        if not by_sha1 and not by_file:
            return  # 空文件 / 全是无效记录：当作没有这个来源
        self._side_report_by_sha1 = by_sha1
        self._side_report_by_file = by_file
        self._side_report_path = path
        self._log(
            "info",
            "AutoSync 分类：已加载 ModSideDetector side-report（sha1 {} 条、文件名 {} 条）：{}".format(
                len(by_sha1), len(by_file), path
            ),
        )

    def _decide_by_side_report(self, entry: ClassifyEntry, hit: SideReportEntry) -> None:
        """按 side-report 的结论直接定类别（命中后**不再查 Modrinth**）。"""
        entry.category = SIDE_REPORT_CATEGORY[hit.side]
        entry.source = SOURCE_SIDE_REPORT
        entry.detail = hit.evidence
        # 低置信 / 标记冲突的条目仍然采用（那正是接入的意义），但必须提示人工复核
        note = hit.review_note
        if note:
            entry.note = _append_note(entry.note, note)

    # -------------------------------------------------------------- 报告落盘
    def load_last_report(self) -> Optional[ClassifyReport]:
        """读回最近一次 classify 的报告（apply 的唯一输入）。"""
        try:
            with open(self.report_path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return ClassifyReport.from_dict(data)

    def _write_json(self, path: Path, payload: Dict[str, Any]) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError as exc:
            self._log("warning", f"报告写入失败 {path}：{exc!r}")

    # -------------------------------------------------------------- 分析
    def analyze(self, refresh: bool = False) -> ClassifyReport:
        """只分析、只出报告，**绝不改动任何分发文件**。"""
        started = time.monotonic()
        cfg = self.config
        mods_dir = self.client_mods_dir
        target_dir = self.server_mods_dir
        report = ClassifyReport(
            generated_at=now_iso(),
            generated_ts=time.time(),
            source_dir=str(self.dist_dir),
            source_mods_dir=str(mods_dir),
            target_dir=str(target_dir),
            target_exists=target_dir.is_dir(),
            unknown_as=cfg.classify_unknown_as,
            move_pure_server=bool(cfg.classify_move_pure_server),
        )

        self._log("info", f"AutoSync 分类：客户端 mods = {mods_dir}")
        self._log("info", f"AutoSync 分类：服务端 mods = {target_dir}（存在={report.target_exists}）")

        if not mods_dir.is_dir():
            report.ok = False
            report.message = f"客户端 mods 目录不存在：{mods_dir}"
            report.errors.append(report.message)
            self._log("error", f"AutoSync {report.message}")
            report.elapsed = time.monotonic() - started
            return report

        # ---------------------------------------------------------- 扫描 + 哈希
        candidates: List[Tuple[str, Path, int]] = []
        for rel, abs_path, size, _mtime in iter_files(mods_dir, None):
            candidates.append((rel, abs_path, size))
        if not candidates:
            report.ok = False
            report.message = f"客户端 mods 目录里没有任何文件：{mods_dir}"
            report.errors.append(report.message)
            self._log("error", f"AutoSync {report.message}")
            report.elapsed = time.monotonic() - started
            return report

        jars: List[ClassifyEntry] = []
        others: List[ClassifyEntry] = []
        for rel, abs_path, size in candidates:
            entry = ClassifyEntry(
                rel_path=f"mods/{rel}",
                rel_in_mods=rel,
                name=Path(rel).name,
                size=size,
            )
            if not rel.lower().endswith(".jar"):
                entry.category = CATEGORY_NOT_MOD
                entry.source = SOURCE_UNKNOWN
                entry.detail = "非 .jar 文件，不参与判定"
                entry.action = ACTION_KEEP
                others.append(entry)
                continue
            entry.sha256, entry.sha1 = hash_file(abs_path)
            jars.append(entry)

        self._log(
            "info",
            f"AutoSync 分类：扫描到 {len(jars)} 个 jar（另有 {len(others)} 个非 jar 文件，跳过判定）",
        )

        # ---------------------------------------------------------- side-report（优先来源）
        # ModSideDetector 的结论比 Modrinth 里作者自填的 side 字段可信：命中的条目直接采用，
        # 并且**跳过 Modrinth 查询**（省请求）。未命中的条目照旧走原流程。
        self._load_side_report()
        side_hits = 0
        lookup_jars: List[ClassifyEntry] = []
        for entry in jars:
            if self.lookup_side_report(entry) is not None:
                side_hits += 1
            else:
                lookup_jars.append(entry)
        if side_hits:
            self._log(
                "info",
                "AutoSync 分类：{} 个 jar 命中 side-report（ModSideDetector），本次不再查询 Modrinth".format(
                    side_hits
                ),
            )

        # ---------------------------------------------------------- Modrinth：sha1 -> 项目
        if lookup_jars:
            self._lookup_hits(lookup_jars, refresh=refresh, errors=report.errors)
            self._lookup_projects(lookup_jars, refresh=refresh, errors=report.errors)

        # ---------------------------------------------------------- 判定
        toml_cache: Dict[str, Tuple[Optional[str], str]] = {}
        for entry in jars:
            hit = self._hits.get(entry.sha1)
            project = self._projects.get(hit.project_id) if (hit is not None and hit.project_id) else None
            self._decide(entry, project, toml_cache)

        # ---------------------------------------------------------- 依赖图（保护被依赖的 mod）
        # 保护判定基于**修正后的依赖图**：某条依赖若由依赖方自己的 JiJ 嵌套 jar 提供，
        # 就是自给自足的，不会把别处同名的顶层 mod 钉在客户端；反之，提供 JiJ 的父 mod
        # 本身也会被保护（它被移走，依赖它的 mod 一样会崩）。详见 deps._compute_protection。
        graph = build_dependency_graph(mods_dir, logger=self.logger)
        for entry in jars:
            entry.mod_ids = list(graph.file_mod_ids.get(entry.rel_in_mods, []))
            entry.jij_mod_ids = list(graph.file_jij_mod_ids.get(entry.rel_in_mods, []))
            entry.depended_by = list(graph.protection.get(entry.rel_in_mods, []))
        report.protected_mod_ids = sum(1 for rel in graph.protection if graph.file_mod_ids.get(rel))
        if report.protected_mod_ids:
            self._log(
                "info",
                "AutoSync 分类：{} 个 mod 被其他 mod 必需依赖，它们永远不会被移出客户端目录"
                "（另有 {} 个 jar 的依赖由 JiJ 嵌套自带，不计入依赖保护压力）".format(
                    report.protected_mod_ids, sum(1 for ids in graph.file_jij_mod_ids.values() if ids)
                ),
            )

        # ---------------------------------------------------------- 模式说明
        if cfg.classify_move_pure_server:
            self._log("warning", "AutoSync 分类：" + MOVE_PURE_SERVER_WARNING)
        else:
            self._log("info", "AutoSync 分类：" + KEEP_PURE_SERVER_NOTE)

        # ---------------------------------------------------------- 定动作
        for entry in jars:
            if entry.category == CATEGORY_UNKNOWN:
                # 用 _append_note：命中 side-report 的待定条目可能已带「建议人工复核」提示
                entry.note = _append_note(
                    entry.note,
                    "按双端处理（推断）" if cfg.classify_unknown_as == "both" else "待定，原地不动",
                )
            entry.action = self._plan_action(entry)
        for entry in others:
            entry.action = ACTION_KEEP

        # ---------------------------------------------------------- 目标目录预检（只读）
        self._inspect_target(jars, target_dir, report)

        report.entries = sorted(jars, key=lambda item: item.rel_path) + sorted(
            others, key=lambda item: item.rel_path
        )
        report.modrinth_hits = sum(1 for entry in jars if entry.source == SOURCE_MODRINTH)
        report.toml_fallbacks = sum(1 for entry in jars if entry.source == SOURCE_TOML)
        report.side_report_hits = sum(1 for entry in jars if entry.source == SOURCE_SIDE_REPORT)
        report.counts = {
            CATEGORY_CLIENT_ONLY: sum(1 for e in jars if e.category == CATEGORY_CLIENT_ONLY),
            CATEGORY_BOTH: sum(1 for e in jars if e.category == CATEGORY_BOTH),
            CATEGORY_SERVER_ONLY: sum(1 for e in jars if e.category == CATEGORY_SERVER_ONLY),
            CATEGORY_UNKNOWN: sum(1 for e in jars if e.category == CATEGORY_UNKNOWN),
            CATEGORY_NOT_MOD: len(others),
            "unknown_as_both": sum(
                1 for e in jars if e.category == CATEGORY_UNKNOWN and e.action == ACTION_COPY
            ),
            "unknown_kept": sum(
                1 for e in jars if e.category == CATEGORY_UNKNOWN and e.action == ACTION_NONE
            ),
            "planned_copy": sum(1 for e in jars if e.action == ACTION_COPY),
            "planned_move": sum(1 for e in jars if e.action == ACTION_MOVE),
            "planned_keep": sum(1 for e in jars if e.action == ACTION_KEEP),
            "planned_none": sum(1 for e in jars if e.action == ACTION_NONE),
            "planned_skip": sum(1 for e in jars if e.target_state == "same"),
            "planned_conflict": sum(1 for e in jars if e.target_state in ("different", "dir", "error")),
            "server_only_kept": sum(
                1 for e in jars if e.category == CATEGORY_SERVER_ONLY and e.action != ACTION_MOVE
            ),
            "forced_keep": sum(1 for e in jars if e.forced_keep),
            "total": len(candidates),
        }
        report.snapshot_key = snapshot_key(self._snapshot())
        report.elapsed = time.monotonic() - started
        report.message = (
            f"分类完成：纯客户端 {report.counts[CATEGORY_CLIENT_ONLY]} / "
            f"双端 {report.counts[CATEGORY_BOTH]} / "
            f"纯服务端 {report.counts[CATEGORY_SERVER_ONLY]} / "
            f"待定 {report.counts[CATEGORY_UNKNOWN]}"
            f"（其中按双端处理 {report.counts['unknown_as_both']}）"
            f"，计划 复制 {report.counts['planned_copy']} / 移动 {report.counts['planned_move']}"
            f" / 跳过 {report.counts['planned_skip']} / 冲突 {report.counts['planned_conflict']}"
            f"，共 {report.counts['total']} 个文件，耗时 {report.elapsed:.2f}s"
            + (
                f"；{report.counts['forced_keep']} 个被依赖的 mod 已强制保留"
                if report.counts["forced_keep"]
                else ""
            )
            + (f"；有 {len(report.errors)} 条查询错误，apply 会被拒绝" if report.errors else "")
        )
        self._write_json(self.report_path, report.to_dict())
        self._save_cache()
        self._log("info", f"AutoSync {report.message}")
        self._log("info", f"AutoSync 分类报告已写入：{self.report_path}")
        return report

    # -------------------------------------------------------------- 目标目录预检
    def _inspect_target(self, jars: List[ClassifyEntry], target_dir: Path, report: ClassifyReport) -> None:
        """只读检查目标目录的同名文件，让干跑报告能预告「跳过」与「冲突」。

        这一步**不写任何文件**：只比对 SHA-256，让服主在 apply 之前就看到
        「哪些会跳过、哪些会冲突」。
        """
        for entry in jars:
            if entry.action not in (ACTION_COPY, ACTION_MOVE):
                continue
            target = target_dir / entry.rel_in_mods
            if not target.exists():
                entry.target_state = "absent"
                continue
            if target.is_dir():
                entry.target_state = "dir"
                entry.target_note = "目标同名路径是目录，apply 会按冲突处理（不覆盖）"
                continue
            try:
                target_sha256, _ = hash_file(target)
            except OSError as exc:
                entry.target_state = "error"
                entry.target_note = f"目标哈希失败：{exc!r}"
                continue
            if target_sha256 == entry.sha256:
                entry.target_state = "same"
                entry.target_note = "目标已存在同内容文件，apply 会跳过复制"
            else:
                entry.target_state = "different"
                entry.target_note = "目标同名文件内容不同，apply 会按冲突处理（绝不覆盖）"

    def _lookup_hits(self, jars: List[ClassifyEntry], refresh: bool, errors: List[str]) -> None:
        missing = []
        for entry in jars:
            cached = self._hits.get(entry.sha1)
            if refresh or cached is None or self._hit_expired(cached):
                missing.append(entry.sha1)
        if not missing:
            self._log("info", "AutoSync 分类：所有 jar 的 Modrinth sha1 结果均来自缓存")
            return
        self._log("info", f"AutoSync 分类：查询 {len(missing)} 个 sha1 对应的 Modrinth 项目")
        result = self._make_client().lookup_sha1(missing)
        self._hits.update(result.hits)
        errors.extend(result.errors)
        self._log(
            "info",
            f"AutoSync 分类：Modrinth sha1 查询结束，命中 {len(result.hits)}（失败批次 {result.failed_batches}）",
        )

    def _hit_expired(self, hit: ModrinthHit) -> bool:
        hours = self.config.modrinth_cache_hours
        if hours <= 0 or not hit.queried_at:
            return False
        return (time.time() - hit.queried_at) > hours * 3600

    def _lookup_projects(self, jars: List[ClassifyEntry], refresh: bool, errors: List[str]) -> None:
        wanted: List[str] = []
        for entry in jars:
            hit = self._hits.get(entry.sha1)
            if hit is None or not hit.project_id:
                continue
            cached = self._projects.get(hit.project_id)
            if refresh or cached is None or self._project_expired(cached):
                if hit.project_id not in wanted:
                    wanted.append(hit.project_id)
        if not wanted:
            self._log("info", "AutoSync 分类：Modrinth 项目信息全部来自缓存，跳过网络查询")
            return
        self._log("info", f"AutoSync 分类：批量查询 {len(wanted)} 个 Modrinth 项目（client_side / server_side）")
        result = self._make_client().lookup_projects(wanted)
        self._projects.update(result.projects)
        errors.extend(result.errors)
        self._log(
            "info",
            "AutoSync 分类：项目查询结束，命中 {}（未返回 {}，失败批次 {}）".format(
                len(result.projects), len(result.missing), result.failed_batches
            ),
        )

    def _decide(
        self,
        entry: ClassifyEntry,
        project: Optional[ModrinthProject],
        toml_cache: Dict[str, Tuple[Optional[str], str]],
    ) -> None:
        """优先 side-report（ModSideDetector），其次 Modrinth，再 TOML 兜底，都不行就是待定。"""
        side_hit = self.lookup_side_report(entry)
        if side_hit is not None:
            self._decide_by_side_report(entry, side_hit)
            return
        if project is not None and project.client_side != SIDE_UNKNOWN and project.server_side != SIDE_UNKNOWN:
            entry.project_id = project.project_id
            entry.project_title = project.title or project.slug
            # 保留 Modrinth 返回的原始值，便于人工复核（上次事故就是看不到原始依据）
            entry.client_side = project.client_side
            entry.server_side = project.server_side
            entry.source = SOURCE_MODRINTH
            sides = f"client={project.client_side}, server={project.server_side}"
            if is_unsupported(project.client_side):
                entry.category = CATEGORY_SERVER_ONLY
                entry.detail = f"modrinth({sides}) -> 客户端不支持"
            elif is_unsupported(project.server_side):
                entry.category = CATEGORY_CLIENT_ONLY
                entry.detail = f"modrinth({sides}) -> 服务端不支持"
            else:
                entry.category = CATEGORY_BOTH
                entry.detail = f"modrinth({sides})"
            return

        jar_path = self.client_mods_dir / entry.rel_in_mods
        if entry.rel_in_mods not in toml_cache:
            toml_cache[entry.rel_in_mods] = read_mod_toml(jar_path)
        text, source_name = toml_cache[entry.rel_in_mods]
        if project is not None:
            entry.project_id = project.project_id
            entry.project_title = project.title or project.slug
            entry.client_side = project.client_side
            entry.server_side = project.server_side
        category, detail = judge_toml(text)
        if category is not None:
            entry.category = category
            entry.source = SOURCE_TOML
            entry.detail = f"{detail} [{source_name}]"
            if project is not None:
                entry.detail += f"（Modrinth 项目 {project.project_id} 的 side 字段不完整）"
            return

        entry.category = CATEGORY_UNKNOWN
        entry.source = SOURCE_UNKNOWN
        if project is not None:
            entry.project_id = project.project_id
            entry.project_title = project.title or project.slug
            entry.client_side = project.client_side
            entry.server_side = project.server_side
            entry.detail = (
                f"modrinth 项目 {project.project_id} 的 side 字段不完整"
                f"（client={project.client_side}, server={project.server_side}），"
                f"toml 也判不出"
            )
        elif text:
            entry.detail = f"modrinth 查不到；{source_name} 无 clientSideOnly/displayTest 线索"
        else:
            entry.detail = "modrinth 查不到；jar 内没有模组元数据 TOML"

    def _plan_action(self, entry: ClassifyEntry) -> str:
        """按「类别 + 配置 + 依赖保护」决定计划动作。

        纯服务端有两种可能：``ACTION_MOVE``（移出客户端目录，仅在
        ``classify_move_pure_server=true`` 时）与 ``ACTION_COPY``（只复制，默认）。
        只要该 mod 被别的 mod 必需依赖，就无论如何都不会移走。
        """
        cfg = self.config
        if entry.category == CATEGORY_SERVER_ONLY:
            if entry.has_dependents:
                entry.forced_keep = True
                entry.note = _append_note(
                    entry.note,
                    "被 {} 个 mod 依赖，已强制保留（依赖方：{}）".format(
                        len(entry.depended_by), _brief_list(entry.depended_by)
                    ),
                )
                return ACTION_COPY
            if cfg.classify_move_pure_server:
                entry.note = _append_note(entry.note, "按 classify_move_pure_server=true 移到服务端（有风险）")
                return ACTION_MOVE
            entry.note = _append_note(entry.note, "保守模式：只复制到服务端，不移出客户端目录")
            return ACTION_COPY
        if entry.category == CATEGORY_BOTH:
            return ACTION_COPY
        if entry.category == CATEGORY_CLIENT_ONLY:
            return ACTION_KEEP
        if entry.category == CATEGORY_UNKNOWN:
            return ACTION_COPY if cfg.classify_unknown_as == "both" else ACTION_NONE
        return ACTION_KEEP

    def _snapshot(self) -> Dict[str, List[int]]:
        snap: Dict[str, List[int]] = {}
        for rel, _abs_path, size, mtime_ns in iter_files(self.client_mods_dir, None):
            snap[rel] = [size, mtime_ns]
        return snap

    # -------------------------------------------------------------- 报告文本
    def report_lines(self, report: ClassifyReport, detail_limit: int = 400) -> List[str]:
        """把报告渲染成纯文本行（MCDR 回复与 CLI 输出共用）。"""
        counts = report.counts
        lines = [
            theme.title("AutoSync 模组分类（dry-run：未改动任何文件）"),
            theme.marked(theme.MARK_INFO, "分发目录", report.source_dir),
            theme.marked(theme.MARK_INFO, "客户端 mods", report.source_mods_dir),
            theme.marked(
                theme.MARK_OK if report.target_exists else theme.MARK_WARN,
                "服务端 mods",
                report.target_dir
                + ("" if report.target_exists else "  " + theme.warn("[!] 目录不存在，apply 会拒绝执行")),
            ),
            theme.info(
                "  统计：纯客户端 {} / 双端 {} / 纯服务端 {} / 待定 {}（其中按双端处理 {}，原地不动 {}）{}".format(
                    counts.get(CATEGORY_CLIENT_ONLY, 0),
                    counts.get(CATEGORY_BOTH, 0),
                    counts.get(CATEGORY_SERVER_ONLY, 0),
                    counts.get(CATEGORY_UNKNOWN, 0),
                    counts.get("unknown_as_both", 0),
                    counts.get("unknown_kept", 0),
                    "，非 jar 文件 {} 个（不动）".format(counts.get(CATEGORY_NOT_MOD, 0))
                    if counts.get(CATEGORY_NOT_MOD)
                    else "",
                )
            ),
            theme.info(
                "  计划动作：复制 {} / 移动 {} / 跳过 {} / 冲突 {} / 待定 {}".format(
                    counts.get("planned_copy", 0),
                    counts.get("planned_move", 0),
                    counts.get("planned_skip", 0),
                    counts.get("planned_conflict", 0),
                    counts.get(CATEGORY_UNKNOWN, 0),
                )
            ),
            theme.hint(
                "  模式：classify_move_pure_server={}（{}）".format(
                    "true" if report.move_pure_server else "false",
                    "纯服务端会从 client-dist/mods 移走"
                    if report.move_pure_server
                    else "纯服务端只复制、不移走（默认，保守）",
                )
            ),
            theme.hint(
                "  依据来源：ModSideDetector {} 个、Modrinth {} 个、TOML 兜底 {} 个；"
                "依赖保护：{} 个被依赖的 modId".format(
                    report.side_report_hits,
                    report.modrinth_hits,
                    report.toml_fallbacks,
                    report.protected_mod_ids,
                )
            ),
        ]
        if report.move_pure_server:
            lines.append(theme.bad("[!] " + MOVE_PURE_SERVER_WARNING))
        else:
            lines.append(theme.hint("[i] " + KEEP_PURE_SERVER_NOTE))
        if counts.get("forced_keep"):
            lines.append(
                theme.info(
                    "[i] 有 {} 个 mod 被其他 mod 必需依赖，已强制保留在 client-dist/mods（即使判为纯服务端）".format(
                        counts.get("forced_keep", 0)
                    )
                )
            )

        lines.append(theme.info("明细（{} 项）：".format(len(report.entries))))
        for entry in report.entries[:detail_limit]:
            note = f"｜{entry.note}" if entry.note else ""
            # 纯服务端项必须能一眼看到 Modrinth 的原始依据，便于人工复核
            evidence = ""
            if entry.category == CATEGORY_SERVER_ONLY:
                evidence = "｜原始依据：project_id={} client_side={} server_side={}".format(
                    entry.project_id or "(无)", entry.client_side or "(无)", entry.server_side or "(无)"
                )
                if entry.depended_by:
                    evidence += "；被 {} 个 mod 依赖：{}".format(
                        len(entry.depended_by), _brief_list(entry.depended_by)
                    )
            target = ""
            if entry.target_state:
                target = "｜目标预检：{}".format(TARGET_STATE_LABELS.get(entry.target_state, entry.target_state))
            if entry.jij_mod_ids:
                note += "｜自带 JiJ 前置 {} 个（{}，无需单独安装，也不构成依赖保护压力）".format(
                    len(entry.jij_mod_ids), _brief_list(entry.jij_mod_ids)
                )
            lines.append(
                "  [{}] {} → 依据={}（{}）｜动作={}{}{}{}".format(
                    entry.category_label,
                    entry.rel_path,
                    entry.source,
                    entry.detail,
                    entry.action_label,
                    evidence,
                    target,
                    note,
                )
            )
        if len(report.entries) > detail_limit:
            lines.append(f"  …（其余 {len(report.entries) - detail_limit} 项见 JSON 报告）")

        # 单独再列一次纯服务端清单：这一列最容易出事，必须能一眼核对原始依据
        server_only = report.by_category(CATEGORY_SERVER_ONLY)
        if server_only:
            lines.append(theme.hint(f"纯服务端清单（{len(server_only)} 项，含 Modrinth 原始依据，供人工复核）："))
            for entry in server_only:
                keep = "（被依赖，已强制保留在客户端）" if entry.forced_keep else ""
                if entry.source == SOURCE_SIDE_REPORT:
                    # side-report 给的是 ModSideDetector 的结论，依据在 detail 里（含原始 confidence/notes）
                    evidence = entry.detail
                else:
                    evidence = "project_id={} client_side={} server_side={}".format(
                        entry.project_id or "(无)",
                        entry.client_side or "(无)",
                        entry.server_side or "(无)",
                    )
                lines.append(
                    "  * {}｜{}｜计划动作={}{}".format(
                        entry.rel_path,
                        evidence,
                        entry.action_label,
                        keep,
                    )
                )

        unknown = report.unknown_entries
        if unknown:
            lines.append(theme.hint(f"待定清单（{len(unknown)} 项，需人工复核）："))
            for entry in unknown:
                lines.append(f"  ? {entry.rel_path}｜{entry.detail}｜计划动作={entry.action_label}")
        else:
            lines.append(theme.hint("待定清单：无"))
        if report.errors:
            lines.append(
                theme.warn(
                    f"查询错误：{len(report.errors)} 条（见日志/JSON 报告）；"
                    f"存在查询错误时 apply 会拒绝执行，请先修好网络再重新 classify"
                )
            )
        lines.append(theme.separator())
        return lines

    # -------------------------------------------------------------- 执行
    def apply(self, report: Optional[ClassifyReport] = None) -> ApplyResult:
        """按最近一次 classify 的结果搬运（复制 / 移动）。"""
        started = time.monotonic()
        cfg = self.config
        result = ApplyResult(generated_at=now_iso())
        report = report if report is not None else self.load_last_report()

        def fail(message: str) -> ApplyResult:
            result.ok = False
            result.message = message
            result.errors.append(message)
            result.elapsed = time.monotonic() - started
            self._log("error", f"AutoSync 分类搬运中止：{message}")
            return result

        if report is None:
            return fail(
                f"没有可用的 classify 结果（{self.report_path} 不存在），已拒绝执行；"
                f"请先运行 classify 做干跑分析"
            )

        target_dir = self.server_mods_dir
        if report.source_dir != str(self.dist_dir) or report.target_dir != str(target_dir):
            return fail(
                "分发目录/服务端 mods 目录与上次 classify 不一致（报告里为 {} -> {}，当前为 {} -> {}），"
                "已拒绝执行；请重新 classify".format(
                    report.source_dir, report.target_dir, self.dist_dir, target_dir
                )
            )
        age = time.time() - (report.generated_ts or 0.0)
        if age > REPORT_MAX_AGE_SECONDS:
            return fail(
                "上次 classify 已过去 {:.0f} 分钟（上限 {:.0f} 分钟），已拒绝执行；请重新 classify".format(
                    age / 60.0, REPORT_MAX_AGE_SECONDS / 60.0
                )
            )
        current_key = snapshot_key(self._snapshot())
        if current_key != report.snapshot_key:
            return fail("客户端 mods 目录在上次 classify 之后发生变化，已拒绝执行；请重新 classify")
        if not target_dir.is_dir():
            return fail(f"服务端 mods 目录不存在：{target_dir}（不会自动创建，请先创建或修正 classify_server_mods_dir）")
        if report.errors:
            return fail(
                "上次 classify 有 {} 条 Modrinth 查询错误，分类结果不完整（查不到的会按待定处理），"
                "已拒绝执行；请在网络正常时重新 classify".format(len(report.errors))
            )
        if os.path.normcase(str(target_dir)) == os.path.normcase(str(self.client_mods_dir)):
            return fail("服务端 mods 目录与客户端 mods 目录相同，已拒绝执行（配置有误）")
        if bool(report.move_pure_server) != bool(cfg.classify_move_pure_server):
            return fail(
                "classify_move_pure_server 在上次 classify 之后被改动（报告里为 {}，当前为 {}），"
                "移动/复制的语义已经不同，已拒绝执行；请重新 classify".format(
                    report.move_pure_server, cfg.classify_move_pure_server
                )
            )

        result.unknown = report.counts.get(CATEGORY_UNKNOWN, 0)
        planned = [entry for entry in report.entries if entry.action in (ACTION_COPY, ACTION_MOVE)]

        # ---------------------------------------------------------- 0) 移动降级双保险
        # 即使报告是旧版本生成的（当时还没有依赖保护、或当时 classify_move_pure_server=true），
        # 只要当前配置是保守模式、或该 mod 被别的 mod 依赖，就绝不删除客户端副本。
        downgraded = 0
        for entry in planned:
            if entry.action != ACTION_MOVE:
                continue
            reason = ""
            if not cfg.classify_move_pure_server:
                reason = "当前配置 classify_move_pure_server=false"
            elif entry.depended_by:
                reason = "被 {} 个 mod 依赖".format(len(entry.depended_by))
            if reason:
                entry.action = ACTION_COPY
                entry.forced_keep = True
                entry.note = _append_note(
                    entry.note, "已从「移动」降级为「复制」（{}），客户端副本保留".format(reason)
                )
                downgraded += 1
        if downgraded:
            self._log(
                "warning",
                "AutoSync 有 {} 个原计划「移动」的项已降级为「复制」：保守模式或该 mod 被其他 mod 依赖，"
                "删除客户端副本的风险不可接受".format(downgraded),
            )

        self._log(
            "info",
            "AutoSync 分类搬运开始：计划复制 {} 个、移动 {} 个（备份 {}）".format(
                sum(1 for e in planned if e.action == ACTION_COPY),
                sum(1 for e in planned if e.action == ACTION_MOVE),
                "开启" if cfg.classify_backup else "关闭",
            ),
        )

        # ---------------------------------------------------------- 1) 备份
        backup_root: Optional[Path] = None
        backed_up = 0
        if cfg.classify_backup:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_root = self.data_dir / "classify-backup" / stamp
            for entry in planned:
                if entry.action != ACTION_MOVE:
                    continue  # 只有会被删除的源文件才需要备份
                source = self.client_mods_dir / entry.rel_in_mods
                if not source.is_file():
                    continue
                dest = backup_root / "mods" / entry.rel_in_mods
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, dest)
                    backed_up += 1
                    self._log("info", f"备份：{source} -> {dest}")
                except OSError as exc:
                    entry.note = (entry.note + "；" if entry.note else "") + f"备份失败，未搬运：{exc!r}"
                    result.errors.append(f"备份失败 {source}：{exc!r}")
                    result.failed += 1
            if backed_up:
                result.backup_dir = str(backup_root)
            if result.failed:
                self._log("warning", "有 {} 个文件备份失败，将跳过它们的搬运".format(result.failed))

        # ---------------------------------------------------------- 2) 执行
        for entry in planned:
            source = self.client_mods_dir / entry.rel_in_mods
            target = target_dir / entry.rel_in_mods
            action_record = {
                "rel_path": entry.rel_path,
                "category": entry.category,
                "action": entry.action,
                "source": str(source),
                "target": str(target),
                "result": "",
                "note": "",
            }
            if "备份失败" in entry.note:
                action_record["result"] = "failed"
                action_record["note"] = "备份失败，已跳过"
                result.actions.append(action_record)
                continue
            if not source.is_file():
                action_record["result"] = "failed"
                action_record["note"] = "源文件不存在"
                result.failed += 1
                result.errors.append(f"源文件不存在：{source}")
                result.actions.append(action_record)
                continue

            same_content = False
            if target.exists():
                if target.is_dir():
                    action_record["result"] = "conflict"
                    action_record["note"] = "目标同名路径是目录，不覆盖"
                    result.conflicts += 1
                    result.actions.append(action_record)
                    self._log("warning", f"冲突：目标 {target} 是目录，未覆盖，请人工处理")
                    continue
                try:
                    target_sha256, _ = hash_file(target)  # hash_file 返回 (sha256, sha1)
                except OSError as exc:
                    action_record["result"] = "failed"
                    action_record["note"] = f"目标哈希失败：{exc!r}"
                    result.failed += 1
                    result.errors.append(f"目标哈希失败 {target}：{exc!r}")
                    result.actions.append(action_record)
                    continue
                if target_sha256 == entry.sha256:
                    same_content = True
                else:
                    action_record["result"] = "conflict"
                    action_record["note"] = "目标同名文件内容不同，未覆盖"
                    result.conflicts += 1
                    result.actions.append(action_record)
                    self._log(
                        "warning",
                        f"冲突：{target} 已存在且 SHA-256 不同（保留服务端现有文件，未覆盖，请人工处理）",
                    )
                    continue

            try:
                if not same_content:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                if entry.action == ACTION_MOVE:
                    if same_content:
                        result.skipped += 1
                        result.cleaned += 1
                        action_record["result"] = "skipped"
                        action_record["note"] = "目标已存在同内容文件，跳过复制并按移动语义清理源文件"
                        self._log(
                            "info",
                            f"跳过复制（目标已存在同内容文件）：{target}；清理源文件 {source}",
                        )
                    else:
                        result.moved += 1
                        action_record["result"] = "moved"
                        self._log("info", f"移动：{source} -> {target}（复制后删除源文件）")
                    os.remove(source)
                else:
                    if same_content:
                        result.skipped += 1
                        action_record["result"] = "skipped"
                        action_record["note"] = "目标已存在同内容文件，跳过复制"
                        self._log("info", f"跳过复制（目标已存在同内容文件）：{target}")
                    else:
                        result.copied += 1
                        action_record["result"] = "copied"
                        self._log("info", f"复制：{source} -> {target}")
            except OSError as exc:
                action_record["result"] = "failed"
                action_record["note"] = repr(exc)
                result.failed += 1
                result.errors.append(f"搬运失败 {source} -> {target}：{exc!r}")
                self._log("error", f"搬运失败：{source} -> {target}：{exc!r}")
            result.actions.append(action_record)

        result.ok = result.failed == 0
        result.message = (
            "搬运完成：复制 {} 个、移动 {} 个、跳过 {} 个、冲突 {} 个、待定 {} 个"
            "（额外清理源文件 {} 个、失败 {} 个{}），耗时 {:.2f}s".format(
                result.copied,
                result.moved,
                result.skipped,
                result.conflicts,
                result.unknown,
                result.cleaned,
                result.failed,
                "、{} 个「移动」已降级为「复制」".format(downgraded) if downgraded else "",
                time.monotonic() - started,
            )
        )
        result.elapsed = time.monotonic() - started
        if result.backup_dir:
            self._log("info", f"本次备份目录：{result.backup_dir}")
        apply_path = self.data_dir / f"classify-apply-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        self._write_json(apply_path, result.to_dict())
        self._write_json(self.data_dir / "classify-apply-latest.json", result.to_dict())
        result.report_path = str(apply_path)
        self._log("info", f"AutoSync {result.message}")
        self._log("info", f"搬运报告已写入：{apply_path}")
        return result
