"""自动下载缺失前置（``deps fix`` / ``deps fix <编号...>`` / ``deps fix apply``）。

三步式（安全第一）：

* ``deps fix``（:meth:`DepsFixService.plan`）—— **只分析只报告**：对 ``deps`` 报告里的每个
  「真缺失」modId 定位项目、按版本范围筛选候选，输出**编号清单**，**不下载任何文件**；
  清单同时缓存到 ``<数据目录>/deps-fix-plan.json``，有效期 **30 分钟**；
* ``deps fix 1a 3a``（:meth:`DepsFixService.apply_selection`）—— 按编号**选择性下载**：
  只装选中的候选，**未选中的一律不装**，汇总里明确列「未选择，已跳过」；
* ``deps fix apply``（:meth:`DepsFixService.apply`）—— 等价于「全选推荐候选（每项取 a）」，
  即 ``deps fix 1a 2a 3a …``。

**定位顺序**（前一步没结果才走下一步，Modrinth 命中时**不查** CurseForge）：

1. **Modrinth**（默认）；
2. **CurseForge 官方 API v1**（配置了 ``curseforge_api_key`` 时；401/403 会明确提示 Key
   无效并建议去 https://console.curseforge.com/ 申请，**不阻断其它项**）；
3. **CFWidget**（免 key 的第三方数据源：官方 API 强制 ``x-api-key``，没 key 只能走这条）。
   第三方数据可能滞后/不完整、通常不给可校验直链，因此只用于**定位 + 选版 + 人工下载链接**，
   不参与自动下载；
4. 全部失败 -> 报告里给出人工下载链接建议
   ``https://www.curseforge.com/minecraft/mc-mods/<slug>/files``。

清单里每项都标来源（``[Modrinth]`` / ``[CurseForge/官方API]`` / ``[CurseForge/CFWidget]``）。

编号规则：**编号 = 缺失的前置本身**（同一前置只占一行），候选用 ``a`` / ``b`` / ``c``
（每项最多 3 个），排序 ``release`` > ``beta`` > ``alpha``，同级按发布时间倒序，``a`` 为推荐。
``1`` 等价 ``1a``。编号无效（``1z`` / ``9a`` / ``abc``）时**报错并列出可用编号，不执行任何下载**。

**排版**：每个候选版本独占一行，行宽尽量 ≤ 60 字符（版本号过长会截断），项与项之间空一行；
输出必须是**逐行**的（终端 print / 日志 info 各一行，不把整段多行文本塞给会折叠空白的接口）。

下载后**必须校验哈希**（sha512 > sha1 > md5 依次兜底），不匹配就删除并记为失败；
同名文件先比哈希：相同则跳过，不同则**不覆盖**、只列入冲突报告。

本模块只依赖标准库，可脱离 MCDR 单测（假 Modrinth / 假 CurseForge / 假 CFWidget 服务端即可覆盖全流程）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import theme
from .config import AutoSyncConfig
from .curseforge import (
    AUTH_HINT as CURSEFORGE_AUTH_HINT,
    CFWidgetClient,
    CurseForgeAuthError,
    CurseForgeClient,
    CurseForgeError,
    CurseForgeForbiddenError,
    CurseForgeSearchDisabled,
    SOURCE_CURSEFORGE_API,
    SOURCE_CURSEFORGE_CFWIDGET,
    cfwidget_versions,
    file_version_dict,
    manual_page_url,
)
from .deps import DependencyService, version_in_range
from .manifest import now_iso
from .modrinth import build_opener, normalize_proxy

__all__ = [
    "KNOWN_CONFLICT_GROUPS",
    "MAX_CANDIDATES",
    "MAX_DEPENDENT_FILES_SHOWN",
    "MAX_LINE_CHARS",
    "MAX_VERSION_CHARS",
    "PLAN_CACHE_TTL_SECONDS",
    "SOURCE_CURSEFORGE_API",
    "SOURCE_CURSEFORGE_CFWIDGET",
    "SOURCE_MODRINTH",
    "STATUS_PLANNED",
    "STATUS_EXCLUDED",
    "STATUS_TOO_LARGE",
    "STATUS_NOT_FOUND",
    "STATUS_NO_VERSION",
    "STATUS_MANUAL",
    "FixCandidate",
    "FixPlanItem",
    "FixPlan",
    "FixOutcome",
    "DepsFixService",
    "conflict_annotations",
    "conflict_group_of",
    "conflict_warnings",
    "fold_dependent_files",
    "format_selection_usage",
    "parse_selection_tokens",
    "available_selection_text",
    "truncate_text",
]

#: 「同类互斥」已知组：同一组内的 mod 同时安装极可能冲突，报告里会警告「只选一个」。
#: 注意这只是**内置的少量经验名单**，不是 Modrinth 元数据推断，覆盖有限（见 README）。
KNOWN_CONFLICT_GROUPS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("渲染替换（Sodium 系）", ("sodium", "embeddium", "rubidium", "magnesium", "sodium-extra")),
    ("光影加载器", ("iris", "oculus", "optifine", "shaders")),
    ("物品/配方查看器", ("jei", "rei", "emi")),
    ("小地图", ("journeymap", "xaeros-minimap", "xaerosminimap", "xaeros_minimap")),
)

STATUS_PLANNED = "planned"
STATUS_EXCLUDED = "excluded"
STATUS_TOO_LARGE = "too_large"
STATUS_NOT_FOUND = "not_found"
STATUS_NO_VERSION = "no_version"
#: 第三方源（CFWidget）定位到了、也列出了候选，但没有可校验的直链，只能人工下载
STATUS_MANUAL = "manual"

#: 来源标记
SOURCE_MODRINTH = "Modrinth"

#: 版本类型优先级：release > beta > alpha（数字越大越优先）
_VERSION_TYPE_RANK = {"release": 2, "beta": 1, "alpha": 0}

#: 全源失败时状态的信息量排序（越大越具体）：超限 > 无匹配版本 > 无法定位
_STATUS_SPECIFICITY = {STATUS_NOT_FOUND: 0, STATUS_NO_VERSION: 1, STATUS_TOO_LARGE: 2}

#: 下载分块大小
CHUNK_SIZE = 64 * 1024

#: 每个缺失前置最多列出几个候选版本（a / b / c）
MAX_CANDIDATES = 3
#: 候选编号后缀
CANDIDATE_LABELS: Tuple[str, ...] = ("a", "b", "c")
#: 候选字母与版本类型**固定对应**（某一类没有就不显示该字母，不顺延）：
#: a = 最新正式版 release，b = 最新测试版 beta，c = 最新早期测试版 alpha
CANDIDATE_BY_TYPE: Dict[str, str] = {"release": "a", "beta": "b", "alpha": "c"}
#: 候选展示顺序
CANDIDATE_TYPE_ORDER: Tuple[str, ...] = ("release", "beta", "alpha")

#: 需求方文件名列表最多直接显示几个（超出折叠成「（其余 N 个）」）
MAX_DEPENDENT_FILES_SHOWN = 3

#: 清单里单行建议宽度（超出就截断，避免控制台/聊天框折行看不出版本差异）
MAX_LINE_CHARS = 60
#: 候选行里版本号的最大宽度（超出保留可辨识的前半段 + 「…」）
MAX_VERSION_CHARS = 20
#: 需求方文件名在行内的最大宽度（``createbetterfps-1.21.1-1.1.4.jar`` 这类要完整显示）
MAX_DEPENDENT_NAME_CHARS = 34

#: ``deps fix`` 清单缓存有效期（秒）
PLAN_CACHE_TTL_SECONDS = 30 * 60

#: ``deps fix <编号...>`` 的编号语法：``1`` / ``1a`` / ``12b``
_SELECTION_RE = re.compile(r"^(\d{1,4})([A-Za-z])?$")

#: 编号清单末尾的用法提示
USAGE_LINE = "用法：deps fix <编号> [<编号> ...]   例：deps fix 1a 2a"


def conflict_group_of(mod_id: str) -> str:
    """返回该 modId 所属的已知互斥组名；不属于任何组则返回空串。"""
    key = str(mod_id or "").strip().lower().replace("_", "-")
    for group, members in KNOWN_CONFLICT_GROUPS:
        for member in members:
            if key == member.replace("_", "-"):
                return group
    return ""


def conflict_warnings(mod_ids: Sequence[str]) -> List[str]:
    """对一批 modId 找「同类互斥」警告，返回中文警告文本列表。"""
    buckets: Dict[str, List[str]] = {}
    for mod_id in mod_ids:
        group = conflict_group_of(mod_id)
        if group:
            buckets.setdefault(group, []).append(mod_id)
    warnings: List[str] = []
    for group, members in buckets.items():
        if len(members) < 2:
            continue
        warnings.append(
            "同类互斥警告：{} —— {} 属于同一类，同时安装可能导致冲突，建议人工只选一个".format(
                group, " / ".join(members)
            )
        )
    return warnings


def conflict_annotations(items: Sequence["FixPlanItem"]) -> Dict[int, List[str]]:
    """给清单里的每个互斥项生成「与 [x] yyy 互斥」提示。

    要求：互斥警告在**两个相关项旁都标注**，所以按组遍历、组内两两互相点名。
    返回 ``{清单编号: [提示, ...]}``。
    """
    buckets: Dict[str, List["FixPlanItem"]] = {}
    for item in items:
        if not item.candidates:
            continue
        group = conflict_group_of(item.mod_id)
        if group:
            buckets.setdefault(group, []).append(item)
    annotations: Dict[int, List[str]] = {}
    for group, members in buckets.items():
        if len(members) < 2:
            continue
        for item in members:
            others = [
                "[{}] {}".format(other.index, other.mod_id) for other in members if other is not item
            ]
            annotations.setdefault(item.index, []).append(
                "与 {} 互斥，建议只选一个（同类：{}）".format("、".join(others), group)
            )
    return annotations


def fold_dependent_files(files: Sequence[str], limit: int = MAX_DEPENDENT_FILES_SHOWN) -> str:
    """折叠需求方文件名列表：最多 ``limit`` 个，其余显示「（其余 N 个）」。

    这是硬性要求：``sable`` 那种被 16 个 mod 需要的前置，全列出来会把清单撑爆。
    清单排版另见 :func:`format_dependents`（只显示第一个名字 + 「等 N 个」，保证行短）。
    """
    names = [str(name) for name in files if str(name or "").strip()]
    if not names:
        return "(未知)"
    shown = names[: max(1, int(limit))]
    rest = len(names) - len(shown)
    text = "、".join(shown)
    if rest > 0:
        text += "（其余 {} 个）".format(rest)
    return text


def truncate_text(text: str, limit: int = MAX_LINE_CHARS) -> str:
    """按字符数截断（保留前半段 + 「…」），保证清单每行不撑爆控制台/聊天框。"""
    value = str(text or "")
    width = int(limit)
    if width <= 1 or len(value) <= width:
        return value
    return value[: width - 1] + "…"


def format_dependents(files: Sequence[str]) -> str:
    """需求方一行的内容：``createbetterfps-1.21.1-1.1.4.jar 等 16 个``。

    只显示**一个**文件名（过长再截断）并给出总数，保证这一行足够短。
    """
    names = [str(name) for name in files if str(name or "").strip()]
    if not names:
        return "(未知)"
    head = truncate_text(names[0], MAX_DEPENDENT_NAME_CHARS)
    if len(names) > 1:
        return "{} 等 {} 个".format(head, len(names))
    return head


def format_size_mb(size_mb: float) -> str:
    """大小展示：≥10 MB 保留 1 位小数，否则保留 2 位。"""
    value = float(size_mb or 0.0)
    return "{:.1f}".format(value) if value >= 10 else "{:.2f}".format(value)


def format_selection_usage() -> str:
    """清单末尾的用法行。"""
    return USAGE_LINE


def parse_selection_tokens(tokens: Sequence[str]) -> Tuple[List[Tuple[int, str]], List[str]]:
    """把 ``1a`` / ``12`` / ``3b`` 解析成 ``[(编号, 候选字母)]``。

    返回 ``(选中, 错误列表)``；只要错误列表非空，调用方**必须放弃下载**。
    ``1`` 等价 ``1a``（缺省候选 = 推荐候选）。
    """
    picked: List[Tuple[int, str]] = []
    errors: List[str] = []
    for raw in tokens:
        text = str(raw or "").strip()
        if not text:
            continue
        match = _SELECTION_RE.match(text)
        if not match or match.group(1) == "0":
            errors.append("无效编号：{}（格式应为 <编号>[a-c]，例如 1a）".format(text))
            continue
        label = (match.group(2) or "a").lower()
        if label not in CANDIDATE_LABELS:
            errors.append("无效编号：{}（候选字母只能是 a / b / c）".format(text))
            continue
        picked.append((int(match.group(1)), label))
    return picked, errors


def available_selection_text(plan: "FixPlan") -> str:
    """列出清单里所有可用编号（无效编号报错时一并给出）。"""
    parts: List[str] = []
    for item in plan.items:
        if item.candidates:
            parts.append(" ".join("{}{}".format(item.index, cand.label) for cand in item.candidates))
        else:
            parts.append("[{}] {}（无可选版本）".format(item.index, item.mod_id))
    return "、".join(parts) if parts else "（清单里没有任何可选项）"


# ---------------------------------------------------------------- 报告结构
@dataclass
class FixCandidate:
    """一个候选版本（清单里的 ``a) 2.0.5``）。"""

    label: str = "a"
    version_id: str = ""
    version_number: str = ""
    version_type: str = ""
    date_published: str = ""
    filename: str = ""
    url: str = ""
    size: int = 0
    sha1: str = ""
    sha512: str = ""
    #: md5 兜底（CurseForge 官方 API 常给 sha1+md5）
    md5: str = ""
    #: 来源标记：Modrinth / CurseForge 官方 API / CFWidget
    source: str = SOURCE_MODRINTH
    #: True = 没有可校验的直链（第三方源），只能人工下载，绝不自动下载
    manual_only: bool = False
    #: 人工下载链接（manual_only 时给出文件页/项目文件列表页）
    manual_url: str = ""

    @property
    def size_mb(self) -> float:
        return self.size / 1048576.0

    @property
    def recommended(self) -> bool:
        return self.label == "a"

    @property
    def has_hash(self) -> bool:
        """有任何一个能校验的哈希（sha512/sha1/md5）才允许写盘。"""
        return bool(self.sha512 or self.sha1 or self.md5)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "version_id": self.version_id,
            "version_number": self.version_number,
            "version_type": self.version_type,
            "date_published": self.date_published,
            "filename": self.filename,
            "url": self.url,
            "size": self.size,
            "size_mb": round(self.size_mb, 3),
            "sha1": self.sha1,
            "sha512": self.sha512,
            "md5": self.md5,
            "source": self.source,
            "manual_only": self.manual_only,
            "manual_url": self.manual_url,
            "recommended": self.recommended,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "FixCandidate":
        data = data if isinstance(data, dict) else {}
        return cls(
            label=str(data.get("label") or "a"),
            version_id=str(data.get("version_id") or ""),
            version_number=str(data.get("version_number") or ""),
            version_type=str(data.get("version_type") or ""),
            date_published=str(data.get("date_published") or ""),
            filename=str(data.get("filename") or ""),
            url=str(data.get("url") or ""),
            size=int(data.get("size") or 0),
            sha1=str(data.get("sha1") or ""),
            sha512=str(data.get("sha512") or ""),
            md5=str(data.get("md5") or ""),
            source=str(data.get("source") or SOURCE_MODRINTH),
            manual_only=bool(data.get("manual_only", False)),
            manual_url=str(data.get("manual_url") or ""),
        )


@dataclass
class _CandidatePick:
    """``_pick_candidates`` 的内部结果：候选列表，或「为什么没有候选」。"""

    candidates: List["FixCandidate"] = field(default_factory=list)
    status: str = STATUS_PLANNED
    note: str = ""


@dataclass
class FixPlanItem:
    """一个缺失前置的编号项（编号 = 前置本身，同一前置只占一行）。"""

    mod_id: str
    version_range: str = ""
    dependent_files: List[str] = field(default_factory=list)
    status: str = STATUS_PLANNED
    note: str = ""
    #: 清单里的编号（从 1 开始）
    index: int = 0
    #: 最多 3 个候选版本，``a`` 为推荐
    candidates: List[FixCandidate] = field(default_factory=list)
    #: 来源标记（``Modrinth`` / ``CurseForge/官方API`` / ``CurseForge/CFWidget``）
    source: str = ""
    #: 人工下载链接（第三方源没有可校验直链、或全都定位失败时给出）
    manual_url: str = ""
    #: Modrinth 侧信息
    project_id: str = ""
    slug: str = ""
    title: str = ""
    locator: str = ""
    #: 以下字段等于推荐候选（a）的信息，保持与旧报告/旧调用方的兼容
    version_id: str = ""
    version_number: str = ""
    version_type: str = ""
    date_published: str = ""
    filename: str = ""
    url: str = ""
    size: int = 0
    sha1: str = ""
    sha512: str = ""

    @property
    def size_mb(self) -> float:
        return self.size / 1048576.0

    @property
    def recommended(self) -> Optional[FixCandidate]:
        """推荐候选（``a``）；没有候选时返回 ``None``。"""
        return self.candidates[0] if self.candidates else None

    @property
    def listed(self) -> bool:
        """清单里是否要按「有候选」的格式展示（含只能人工下载的项）。"""
        return bool(self.candidates) and self.status in (STATUS_PLANNED, STATUS_MANUAL)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "index": self.index,
            "version_range": self.version_range,
            "dependent_files": list(self.dependent_files),
            "status": self.status,
            "note": self.note,
            "candidates": [cand.to_dict() for cand in self.candidates],
            "source": self.source,
            "manual_url": self.manual_url,
            "project_id": self.project_id,
            "slug": self.slug,
            "title": self.title,
            "locator": self.locator,
            "version_id": self.version_id,
            "version_number": self.version_number,
            "version_type": self.version_type,
            "date_published": self.date_published,
            "filename": self.filename,
            "url": self.url,
            "size": self.size,
            "size_mb": round(self.size_mb, 3),
            "sha1": self.sha1,
            "sha512": self.sha512,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "FixPlanItem":
        data = data if isinstance(data, dict) else {}
        files = data.get("dependent_files")
        raw_candidates = data.get("candidates")
        return cls(
            mod_id=str(data.get("mod_id") or ""),
            version_range=str(data.get("version_range") or ""),
            dependent_files=[str(x) for x in files] if isinstance(files, (list, tuple)) else [],
            status=str(data.get("status") or STATUS_PLANNED),
            note=str(data.get("note") or ""),
            index=int(data.get("index") or 0),
            candidates=[FixCandidate.from_dict(x) for x in raw_candidates] if isinstance(raw_candidates, list) else [],
            source=str(data.get("source") or ""),
            manual_url=str(data.get("manual_url") or ""),
            project_id=str(data.get("project_id") or ""),
            slug=str(data.get("slug") or ""),
            title=str(data.get("title") or ""),
            locator=str(data.get("locator") or ""),
            version_id=str(data.get("version_id") or ""),
            version_number=str(data.get("version_number") or ""),
            version_type=str(data.get("version_type") or ""),
            date_published=str(data.get("date_published") or ""),
            filename=str(data.get("filename") or ""),
            url=str(data.get("url") or ""),
            size=int(data.get("size") or 0),
            sha1=str(data.get("sha1") or ""),
            sha512=str(data.get("sha512") or ""),
        )


@dataclass
class FixPlan:
    ok: bool = True
    message: str = ""
    generated_at: str = ""
    #: 生成时刻（epoch 秒），缓存有效期用
    generated_ts: float = 0.0
    #: 清单缓存有效期（秒）
    ttl_seconds: float = float(PLAN_CACHE_TTL_SECONDS)
    #: 生成清单时的 deps 结果指纹，与当前结果不一致则拒绝按编号安装
    signature: str = ""
    dry_run: bool = True
    game_version: str = ""
    loader: str = ""
    max_size_mb: int = 50
    client_mods_dir: str = ""
    missing_total: int = 0
    items: List[FixPlanItem] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def planned(self) -> List[FixPlanItem]:
        return [item for item in self.items if item.status == STATUS_PLANNED and item.candidates]

    @property
    def planned_bytes(self) -> int:
        """推荐候选（a）的总体积，仅用于概览。"""
        return sum(item.size for item in self.planned)

    @property
    def source_counts(self) -> Dict[str, int]:
        """可下载项按来源计数（``{来源: 个数}``），用于清单头部概览。"""
        counts: Dict[str, int] = {}
        for item in self.planned:
            key = item.source or SOURCE_MODRINTH
            counts[key] = counts.get(key, 0) + 1
        return counts

    def item_by_index(self, index: int) -> Optional[FixPlanItem]:
        return next((item for item in self.items if item.index == index), None)

    def conflict_annotations(self) -> Dict[int, List[str]]:
        return conflict_annotations(self.items)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "generated_at": self.generated_at,
            "generated_ts": self.generated_ts,
            "ttl_seconds": self.ttl_seconds,
            "signature": self.signature,
            "dry_run": self.dry_run,
            "game_version": self.game_version,
            "loader": self.loader,
            "max_size_mb": self.max_size_mb,
            "client_mods_dir": self.client_mods_dir,
            "missing_total": self.missing_total,
            "planned_count": len(self.planned),
            "planned_bytes": self.planned_bytes,
            "items": [item.to_dict() for item in self.items],
            "conflicts": list(self.conflicts),
            "errors": list(self.errors),
            "elapsed": round(self.elapsed, 3),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "FixPlan":
        data = data if isinstance(data, dict) else {}
        raw_items = data.get("items")
        plan = cls(
            ok=bool(data.get("ok", True)),
            message=str(data.get("message") or ""),
            generated_at=str(data.get("generated_at") or ""),
            generated_ts=float(data.get("generated_ts") or 0.0),
            ttl_seconds=float(data.get("ttl_seconds") or PLAN_CACHE_TTL_SECONDS),
            signature=str(data.get("signature") or ""),
            dry_run=bool(data.get("dry_run", True)),
            game_version=str(data.get("game_version") or ""),
            loader=str(data.get("loader") or ""),
            max_size_mb=int(data.get("max_size_mb") or 0),
            client_mods_dir=str(data.get("client_mods_dir") or ""),
            missing_total=int(data.get("missing_total") or 0),
            conflicts=[str(x) for x in (data.get("conflicts") or [])],
            errors=[str(x) for x in (data.get("errors") or [])],
            elapsed=float(data.get("elapsed") or 0.0),
        )
        if isinstance(raw_items, list):
            plan.items = [FixPlanItem.from_dict(x) for x in raw_items]
        return plan


@dataclass
class FixOutcome:
    ok: bool = True
    message: str = ""
    #: 本次选中的编号（如 ``["1a", "3a"]``）
    selected: List[str] = field(default_factory=list)
    downloaded: List[Dict[str, Any]] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    failed: List[Dict[str, Any]] = field(default_factory=list)
    #: 未选择、因而**没有安装**的项
    unselected: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    total_bytes: int = 0
    elapsed: float = 0.0
    report_path: str = ""

    @property
    def downloaded_count(self) -> int:
        return len(self.downloaded)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "selected": list(self.selected),
            "downloaded": list(self.downloaded),
            "skipped": list(self.skipped),
            "conflicts": list(self.conflicts),
            "failed": list(self.failed),
            "unselected": list(self.unselected),
            "errors": list(self.errors),
            "downloaded_count": self.downloaded_count,
            "total_bytes": self.total_bytes,
            "elapsed": round(self.elapsed, 3),
            "report_path": self.report_path,
        }


# ---------------------------------------------------------------- 服务
class DepsFixService:
    """``deps fix``：定位 → 选版 → 编号清单；``deps fix <编号...>`` 与 ``apply`` 才下载。"""

    def __init__(
        self,
        config: AutoSyncConfig,
        dist_dir: Path,
        data_dir: Path,
        logger: Any = None,
        deps_service: Optional[DependencyService] = None,
        api_base: str = "",
        timeout: float = 0.0,
        max_retries: Optional[int] = None,
        sleeper=time.sleep,
    ) -> None:
        self.config = config
        self.dist_dir = Path(dist_dir)
        self.data_dir = Path(data_dir)
        self.logger = logger
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.deps = deps_service or DependencyService(config, self.dist_dir, self.data_dir, logger=logger)
        self.api_base = str(api_base or getattr(config, "modrinth_api_base", "")).rstrip("/")
        self.user_agent = str(getattr(config, "modrinth_user_agent", "") or "AutoSync")
        self.timeout = float(timeout or getattr(config, "modrinth_timeout_seconds", 20) or 20)
        retries = getattr(config, "modrinth_max_retries", 3) if max_retries is None else max_retries
        self.max_retries = max(0, int(retries or 0))
        self._sleep = sleeper
        # ---- 出站代理（http_proxy 配置；对 Modrinth / CurseForge / 下载全部生效）----
        self.proxy = str(getattr(config, "http_proxy", "") or "").strip()
        self._opener = build_opener(self.proxy)
        _address, _warning = normalize_proxy(self.proxy)
        if _warning:
            self._log("warning", "AutoSync " + _warning)
        # ---- CurseForge 兜底（官方 API 需要 key；没 key 时退到免 key 的 CFWidget）----
        self.curseforge_api_key = str(getattr(config, "curseforge_api_key", "") or "").strip()
        self.curseforge_api_base = str(
            getattr(config, "curseforge_api_base", "") or "https://api.curseforge.com"
        ).rstrip("/")
        self.curseforge = CurseForgeClient(
            api_key=self.curseforge_api_key,
            user_agent=self.user_agent,
            api_base=self.curseforge_api_base,
            timeout=self.timeout,
            max_retries=self.max_retries,
            logger=logger,
            sleeper=sleeper,
            proxy=self.proxy,
        )
        self.cfwidget_api_base = str(
            getattr(config, "curseforge_cfwidget_api_base", "") or "https://api.cfwidget.com"
        ).rstrip("/")
        self.cfwidget = CFWidgetClient(
            user_agent=self.user_agent,
            api_base=self.cfwidget_api_base,
            timeout=self.timeout,
            max_retries=self.max_retries,
            logger=logger,
            sleeper=sleeper,
            proxy=self.proxy,
        )
        #: 官方 API 鉴权失败后，后续项不再重复打官方 API（CFWidget 仍会尝试）
        self._cf_api_disabled = False
        #: 官方 ``/v1/mods/search`` 被 403 禁用（新 key 常见）：静默改用 CFWidget 取项目 ID
        self._cf_search_disabled = False
        #: 鉴权失败的提示语（后续被跳过的项会引用同一句，保证提示一致）
        self._cf_auth_message = ""
        #: CFWidget 项目查询缓存（同一次 plan 内不重复请求第三方源）
        self._cfwidget_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        #: CFWidget 是否启用（默认始终启用；留出关闭口子便于排查）
        self._cfwidget_enabled = bool(getattr(config, "curseforge_cfwidget_enabled", True))
        #: 本次 plan 里 CurseForge 相关的失败说明（会并入 plan.errors）
        self._cf_errors: List[str] = []
        self.report_path = self.data_dir / "deps-fix-report.json"
        #: ``deps fix`` 生成的编号清单缓存（30 分钟内可被 ``deps fix <编号...>`` 复用）
        self.plan_cache_path = self.data_dir / "deps-fix-plan.json"

    # -------------------------------------------------------------- 配置
    @property
    def mods_dir(self) -> Path:
        return self.dist_dir / "mods"

    @property
    def curseforge_enabled(self) -> bool:
        """是否配置了官方 API Key（没配置就走免 key 的 CFWidget）。"""
        return bool(self.curseforge_api_key) and self.curseforge.enabled

    @property
    def cfwidget_enabled(self) -> bool:
        """免 key 第三方源 CFWidget 是否启用（官方 API 之外的兜底路径）。"""
        return bool(self._cfwidget_enabled)

    @property
    def game_version(self) -> str:
        return str(getattr(self.config, "deps_fix_game_version", "1.21.1") or "1.21.1")

    @property
    def loader(self) -> str:
        return str(getattr(self.config, "deps_fix_loader", "neoforge") or "neoforge")

    @property
    def max_size_mb(self) -> int:
        return int(getattr(self.config, "deps_fix_max_size_mb", 50) or 0)

    @property
    def exclude(self) -> List[str]:
        raw = getattr(self.config, "deps_fix_exclude", []) or []
        return [str(item).strip().lower() for item in raw if str(item).strip()]

    @property
    def show_prerelease(self) -> bool:
        """是否列出预发布候选（b = beta / c = alpha）。默认 true。"""
        return bool(getattr(self.config, "deps_fix_show_prerelease", True))

    @property
    def allowed_candidate_types(self) -> Tuple[str, ...]:
        """允许出现在清单里的版本类型（``deps_fix_show_prerelease=false`` 时只有 release）。"""
        return CANDIDATE_TYPE_ORDER if self.show_prerelease else ("release",)

    def _log(self, level: str, message: str) -> None:
        if self.logger is None:
            return
        getattr(self.logger, level, self.logger.info)(message)

    # -------------------------------------------------------------- HTTP
    def _get_json(self, url: str) -> Any:
        """带重试的 GET JSON（复用 modrinth 的 User-Agent / 超时 / 重试 / 代理配置）。"""
        request = urllib.request.Request(
            url, headers={"User-Agent": self.user_agent, "Accept": "application/json"}, method="GET"
        )
        last_error: Optional[BaseException] = None
        for attempt in range(self.max_retries + 1):
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    raw = response.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code == 404:
                    return None  # 项目不存在属于正常分支，交给调用方兜底
                if exc.code != 429 and 400 <= exc.code < 500:
                    raise RuntimeError(f"HTTP {exc.code} {exc.reason}") from exc
                delay = min(30.0, 1.5 ** attempt)
                self._log("warning", f"Modrinth HTTP {exc.code}，{delay:.1f}s 后重试（第 {attempt + 1} 次）")
            except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, ValueError) as exc:
                last_error = exc
                delay = min(30.0, 1.5 ** attempt)
                self._log("warning", f"Modrinth 请求异常 {exc!r}，{delay:.1f}s 后重试（第 {attempt + 1} 次）")
            if attempt < self.max_retries:
                self._sleep(delay)
        raise RuntimeError(f"重试 {self.max_retries} 次后仍失败：{last_error!r}")

    def _locate_project(self, mod_id: str) -> Tuple[Optional[Dict[str, Any]], str, str]:
        """定位 Modrinth 项目，返回 ``(project, 说明, 错误)``。

        先直接按 slug/id 试 ``GET /v2/project/{modId}``；失败再用搜索接口，
        只接受**名称或 slug 精确匹配**的结果（大小写不敏感）。
        """
        try:
            data = self._get_json(f"{self.api_base}/project/{urllib.parse.quote(mod_id)}")
            if isinstance(data, dict) and data.get("id"):
                return data, f"直接命中 project/{mod_id}", ""
        except Exception as exc:  # noqa: BLE001 - 定位失败要降级到搜索
            self._log("info", f"AutoSync deps fix: project/{mod_id} 查询失败（{exc}），改用搜索")
        facets = json.dumps(
            [["project_type:mod"], [f"versions:{self.game_version}"], [f"categories:{self.loader}"]],
            separators=(",", ":"),
        )
        url = "{}/search?query={}&facets={}&limit=10".format(
            self.api_base, urllib.parse.quote(mod_id), urllib.parse.quote(facets, safe="")
        )
        try:
            data = self._get_json(url)
        except Exception as exc:  # noqa: BLE001
            return None, "", f"搜索 {mod_id} 失败：{exc}"
        hits = (data or {}).get("hits") if isinstance(data, dict) else None
        target = mod_id.strip().lower()
        for hit in hits or []:
            if not isinstance(hit, dict):
                continue
            if str(hit.get("slug") or "").lower() == target or str(hit.get("title") or "").strip().lower() == target:
                return hit, f"搜索精确匹配命中 {hit.get('slug')}", ""
        return None, "", "无法定位（直接查询与搜索都没有精确匹配的 Modrinth 项目）"

    def _fetch_versions(self, project_id: str) -> List[Dict[str, Any]]:
        loaders = json.dumps([self.loader], separators=(",", ":"))
        games = json.dumps([self.game_version], separators=(",", ":"))
        url = "{}/project/{}/version?loaders={}&game_versions={}".format(
            self.api_base, urllib.parse.quote(project_id), urllib.parse.quote(loaders, safe=""), urllib.parse.quote(games, safe="")
        )
        data = self._get_json(url)
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    # -------------------------------------------------------------- 选版
    def _eligible_versions(self, versions: Sequence[Dict[str, Any]], ranges: Sequence[str]) -> Tuple[List[Dict], bool]:
        """按依赖声明的 versionRange 过滤候选版本。

        语义与 :func:`autosync.deps.version_in_range` 保持一致：裸版本是**软要求**（任何版本都
        满足），判不了（``None``）时保守接受。返回 ``(候选, 是否发生过范围过滤)``。
        全部被排除时返回 ``([], True)``，由调用方决定退回策略。
        """
        specs = [str(item).strip() for item in ranges if str(item or "").strip()]
        if not specs:
            return list(versions), False
        accepted = [
            version
            for version in versions
            if any(version_in_range(str(version.get("version_number") or ""), spec)[0] is not False for spec in specs)
        ]
        return accepted, True

    @staticmethod
    def _version_sort_key(version: Dict[str, Any]) -> Tuple[int, str]:
        rank = _VERSION_TYPE_RANK.get(str(version.get("version_type") or "").strip().lower(), -1)
        return rank, str(version.get("date_published") or "")

    @classmethod
    def _sort_versions(cls, versions: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """候选排序：``release`` > ``beta`` > ``alpha``，同级按发布时间倒序。"""
        return sorted(versions, key=cls._version_sort_key, reverse=True)

    @classmethod
    def _pick_version(cls, versions: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """优先 release，其次 beta/alpha；同一优先级取 date_published 最新的。"""
        ordered = cls._sort_versions(versions)
        return ordered[0] if ordered else None

    @staticmethod
    def _pick_file(version: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        files = version.get("files")
        if not isinstance(files, list):
            return None
        for candidate in files:
            if isinstance(candidate, dict) and candidate.get("primary"):
                return candidate
        return next((item for item in files if isinstance(item, dict)), None)

    @classmethod
    def _candidate_from(
        cls, version: Dict[str, Any], label: str, source: str = SOURCE_MODRINTH
    ) -> Optional[FixCandidate]:
        """把一个版本字典转成候选；没有可下载文件时返回 ``None``。

        版本字典可能是 Modrinth 版本，也可能是 :mod:`autosync.curseforge` 归一出来的
        CurseForge 文件（结构一致），所以这一段三源共用。
        """
        chosen_file = cls._pick_file(version)
        if chosen_file is None:
            return None
        hashes = chosen_file.get("hashes") if isinstance(chosen_file.get("hashes"), dict) else {}
        candidate = FixCandidate(
            label=label,
            version_id=str(version.get("id") or ""),
            version_number=str(version.get("version_number") or ""),
            version_type=str(version.get("version_type") or ""),
            date_published=str(version.get("date_published") or ""),
            filename=str(chosen_file.get("filename") or ""),
            url=str(chosen_file.get("url") or ""),
            size=int(chosen_file.get("size") or 0),
            sha1=str(hashes.get("sha1") or ""),
            sha512=str(hashes.get("sha512") or ""),
            md5=str(hashes.get("md5") or ""),
            source=str(version.get("source") or source),
            manual_only=bool(version.get("manual_only")),
            manual_url=str(version.get("manual_url") or ""),
        )
        if not candidate.filename:
            return None
        if not candidate.url and not candidate.manual_only:
            return None
        return candidate

    # -------------------------------------------------------------- 计划
    def plan(self) -> FixPlan:
        """只分析只报告：定位 + 选版 + 输出编号清单（**不下载任何文件**）并写清单缓存。"""
        started = time.monotonic()
        plan = FixPlan(
            generated_at=now_iso(),
            generated_ts=time.time(),
            ttl_seconds=float(PLAN_CACHE_TTL_SECONDS),
            dry_run=True,
            game_version=self.game_version,
            loader=self.loader,
            max_size_mb=self.max_size_mb,
            client_mods_dir=str(self.mods_dir),
        )
        self._cf_api_disabled = False
        self._cf_search_disabled = False
        self._cf_auth_message = ""
        self._cfwidget_cache = {}
        self._cf_errors = []
        report = self.deps.analyze()
        plan.missing_total = report.missing_count
        plan.signature = self._deps_signature(report)
        if not report.ok:
            plan.ok = False
            plan.message = f"依赖检查未完成，无法生成清单：{report.message}"
            plan.errors.append(plan.message)
            plan.elapsed = time.monotonic() - started
            return plan

        self._log(
            "info",
            "AutoSync deps fix：真缺失前置 {} 个，开始向 Modrinth（{} / {}）定位与选版"
            "（Modrinth 失败时兜底：CurseForge 官方 API={} / CFWidget={}；只生成清单，不下载）".format(
                report.missing_count,
                self.game_version,
                self.loader,
                "启用" if self.curseforge_enabled else "未配置 key，跳过",
                "启用" if self.cfwidget_enabled else "关闭",
            ),
        )
        exclude = self.exclude
        for position, missing in enumerate(report.missing, start=1):
            item = FixPlanItem(
                mod_id=missing.mod_id,
                version_range=missing.version_text,
                dependent_files=list(missing.dependent_files),
                index=position,
            )
            plan.items.append(item)
            if item.mod_id.lower() in exclude:
                item.status = STATUS_EXCLUDED
                item.note = "命中 deps_fix_exclude，永不自动下载"
                self._log("info", f"AutoSync deps fix: 跳过 {item.mod_id}（deps_fix_exclude）")
                continue
            try:
                self._fill_item(item, missing.version_ranges)
            except Exception as exc:  # noqa: BLE001 - 单个 mod 失败不能拖垮整个计划
                item.status = STATUS_NOT_FOUND
                item.note = f"定位/取版本失败：{exc}"
                plan.errors.append(f"{item.mod_id}：{exc}")
                self._log("warning", f"AutoSync deps fix: {item.mod_id} 处理失败 {exc!r}")
                continue

        # CurseForge 相关的失败（401 / 网络 / download-url 端点失败）统一记入报告
        for message in self._cf_errors:
            if message not in plan.errors:
                plan.errors.append(message)

        plan.conflicts = conflict_warnings([item.mod_id for item in plan.planned])
        for warning in plan.conflicts:
            self._log("warning", f"AutoSync deps fix: {warning}")
        plan.elapsed = time.monotonic() - started
        plan.message = (
            "缺失前置清单：缺失 {total} 个，可下载 {planned} 个（推荐候选合计约 {size:.2f} MB），"
            "排除 {excluded} 个，超限 {too_large} 个，无法定位/无版本 {not_found} 个，"
            "需人工下载 {manual} 个，耗时 {elapsed:.2f}s".format(
                total=plan.missing_total,
                planned=len(plan.planned),
                size=plan.planned_bytes / 1048576.0,
                excluded=sum(1 for i in plan.items if i.status == STATUS_EXCLUDED),
                too_large=sum(1 for i in plan.items if i.status == STATUS_TOO_LARGE),
                not_found=sum(1 for i in plan.items if i.status in (STATUS_NOT_FOUND, STATUS_NO_VERSION)),
                manual=sum(1 for i in plan.items if i.status == STATUS_MANUAL),
                elapsed=plan.elapsed,
            )
        )
        self._write_json(self.report_path, plan.to_dict())
        if plan.ok:
            # 只有成功的清单才写缓存：失败/半成品清单不能被 `deps fix <编号>` 复用
            self._write_json(self.plan_cache_path, plan.to_dict())
        self._log("info", f"AutoSync {plan.message}")
        return plan

    def _fill_item(self, item: FixPlanItem, ranges: Sequence[str]) -> None:
        """按优先级定位/选版：Modrinth -> CurseForge 官方 API -> CFWidget -> 人工下载链接。

        Modrinth 一命中就**不再查** CurseForge；每一步失败只记录原因，不抛给调用方，
        更不影响别的缺失项。
        """
        if self._try_modrinth(item, ranges):
            return
        reasons: List[str] = []
        statuses: List[str] = []
        if item.note:
            reasons.append("Modrinth：{}".format(item.note))
        statuses.append(item.status)

        # ---- 第二步：CurseForge 官方 API（只有配置了 key 才可用）----
        if self.curseforge_enabled and not self._cf_api_disabled:
            try:
                if self._try_curseforge_api(item, ranges):
                    return
                reasons.append("CurseForge 官方 API：{}".format(item.note or "无可用候选"))
                statuses.append(item.status)
            except CurseForgeAuthError as exc:
                # 401/403（Key 无效）：明确提示 + 后续项不再重复打官方 API（其它项照常处理）
                self._cf_api_disabled = True
                self._cf_auth_message = str(exc)
                self._record_cf_error(str(exc))
                reasons.append("CurseForge 官方 API：{}".format(exc))
                statuses.append(STATUS_NOT_FOUND)
            except CurseForgeForbiddenError as exc:
                # 项目级 403（作者禁止第三方 API 分发）：只影响这一项，**不**全局禁用官方 API
                self._record_cf_error("{}：{}".format(item.mod_id, exc))
                reasons.append("CurseForge 官方 API：{}".format(exc))
                statuses.append(STATUS_NOT_FOUND)
            except CurseForgeError as exc:
                self._record_cf_error("{}：{}".format(item.mod_id, exc))
                reasons.append("CurseForge 官方 API：{}".format(exc))
                statuses.append(STATUS_NOT_FOUND)
        elif self.curseforge_enabled:
            reasons.append(
                "CurseForge 官方 API：{}".format(
                    self._cf_auth_message or "API Key 无效/未配置，后续项已跳过官方 API"
                )
            )
            statuses.append(STATUS_NOT_FOUND)

        # ---- 第三步：免 key 第三方源 CFWidget（数据可能滞后/不完整）----
        if self.cfwidget_enabled:
            try:
                if self._try_cfwidget(item, ranges):
                    return
                reasons.append("CFWidget（第三方源，可能滞后）：{}".format(item.note or "无可用候选"))
                statuses.append(item.status)
            except CurseForgeError as exc:
                self._record_cf_error("{} CFWidget：{}".format(item.mod_id, exc))
                reasons.append("CFWidget：{}".format(exc))
                statuses.append(STATUS_NOT_FOUND)

        # ---- 全部失败：取最有信息量的状态 + 给人工下载链接建议 ----
        item.status = max(statuses, key=lambda value: _STATUS_SPECIFICITY.get(value, 0))
        if not item.manual_url:
            item.manual_url = manual_page_url(item.mod_id)
        reasons.append("建议人工下载：{}".format(item.manual_url))
        item.note = "；".join(reasons)
        self._log("warning", "AutoSync deps fix: {} 未能自动定位（{}）".format(item.mod_id, item.note))

    def _try_modrinth(self, item: FixPlanItem, ranges: Sequence[str]) -> bool:
        """第一步：Modrinth。成功（拿到候选）返回 True。"""
        project, note, error = self._locate_project(item.mod_id)
        if project is None:
            item.status = STATUS_NOT_FOUND
            item.note = error or "无法定位"
            self._log("warning", f"AutoSync deps fix: {item.mod_id} {item.note}")
            return False
        item.project_id = str(project.get("project_id") or project.get("id") or "")
        item.slug = str(project.get("slug") or "")
        item.title = str(project.get("title") or "")
        item.locator = note
        if not item.project_id:
            item.status = STATUS_NOT_FOUND
            item.note = "定位结果里没有 project id"
            return False
        versions = self._fetch_versions(item.project_id)
        if not versions:
            item.status = STATUS_NO_VERSION
            item.note = "Modrinth 上没有匹配 {} / {} 的版本".format(self.game_version, self.loader)
            return False
        return self._fill_candidates(item, versions, ranges, SOURCE_MODRINTH)

    def _try_curseforge_api(self, item: FixPlanItem, ranges: Sequence[str]) -> bool:
        """第二步：CurseForge 官方 API。

        定位数字 ``modId`` 有两条路：

        1. ``GET /v1/mods/search?gameId=432&slug=<modId>``（**很多新 key 被 403 禁用**，
           403 时静默回退，不刷错误）；
        2. search 不可用/无精确匹配 -> 用 **CFWidget（免 key）按 slug 拿数字 projectId**，
           再回到官方 API 取项目信息与文件列表（``/v1/mods/{id}``、``/v1/mods/{id}/files``）。

        只要文件列表来自官方 API，来源就标 ``[CurseForge/官方API]``，
        并在 ``locator`` 里注明「项目 ID 来自 CFWidget」。
        """
        mod_id, origin = self._curseforge_mod_id(item)
        if not mod_id:
            item.status = STATUS_NOT_FOUND
            item.note = "CurseForge 定位失败（官方 search 不可用且 CFWidget 也没有该项目）"
            return False

        origin_text = "官方 search" if origin == "search" else "CFWidget"
        project = self.curseforge.get_mod(mod_id)
        if isinstance(project, dict):
            item.slug = str(project.get("slug") or item.slug)
            item.title = str(project.get("name") or project.get("title") or item.title)
        files = self.curseforge.list_files(mod_id, self.game_version, self.loader)
        if not files:
            item.status = STATUS_NO_VERSION
            item.note = "CurseForge 官方 API 上没有匹配 {} / {} 的文件".format(self.game_version, self.loader)
            return False
        versions = [self._curseforge_version(mod_id, entry) for entry in files]
        versions = [version for version in versions if version is not None]
        if not versions:
            item.status = STATUS_NO_VERSION
            item.note = "CurseForge 文件都没能取到下载链接（downloadUrl 为空且 download-url 端点失败）"
            return False
        ok = self._fill_candidates(item, versions, ranges, SOURCE_CURSEFORGE_API)
        if ok:
            item.project_id = mod_id
            item.slug = item.slug or str(item.mod_id)
            item.locator = "CurseForge 官方 API（项目 ID {} 来自 {}）".format(mod_id, origin_text)
        return ok

    def _curseforge_mod_id(self, item: FixPlanItem) -> Tuple[str, str]:
        """拿 CurseForge 数字 projectId，返回 ``(id, 来源: search/cfwidget/"")``。"""
        if not self._cf_search_disabled:
            try:
                mod = self.curseforge.search_mod(item.mod_id)
            except CurseForgeSearchDisabled as exc:
                # key 有效但官方禁用了 search：静默回退到 CFWidget（只提示一次，不进报告）
                self._cf_search_disabled = True
                self._log("info", "AutoSync deps fix: {}".format(exc))
                mod = None
            if isinstance(mod, dict) and mod.get("id"):
                item.slug = str(mod.get("slug") or item.mod_id)
                item.title = str(mod.get("name") or mod.get("title") or "")
                return str(mod["id"]), "search"
        project = self._cfwidget_project(item.mod_id)
        if isinstance(project, dict):
            raw_id = str(project.get("id") or "").strip()
            if raw_id.isdigit():
                item.slug = str(project.get("slug") or item.mod_id)
                item.title = str(project.get("title") or "")
                return raw_id, "cfwidget"
        return "", ""

    def _cfwidget_project(self, mod_id: str) -> Optional[Dict[str, Any]]:
        """CFWidget 项目（每次 plan 内按 modId 缓存，避免同一次分析重复请求第三方源）。"""
        key = str(mod_id or "").strip().lower()
        if key in self._cfwidget_cache:
            return self._cfwidget_cache[key]
        project: Optional[Dict[str, Any]] = None
        try:
            candidate = self.cfwidget.fetch_project(mod_id)
            project = candidate if isinstance(candidate, dict) else None
        except CurseForgeError as exc:
            self._record_cf_error("{} CFWidget：{}".format(mod_id, exc))
            self._log("warning", f"AutoSync deps fix: CFWidget 查询 {mod_id} 失败 {exc}")
            project = None
        self._cfwidget_cache[key] = project
        return project

    def _try_cfwidget(self, item: FixPlanItem, ranges: Sequence[str]) -> bool:
        """第三步：纯 CFWidget 数据（免 key 第三方源；只做定位/选版/人工链接）。"""
        project = self._cfwidget_project(item.mod_id)
        if not isinstance(project, dict):
            item.status = STATUS_NOT_FOUND
            item.note = "CFWidget 查不到该项目（第三方源数据可能滞后，也可能是 slug 不匹配）"
            return False
        slug = str(project.get("slug") or item.mod_id)
        versions, page = cfwidget_versions(project, slug)
        item.project_id = str(project.get("id") or slug)
        item.slug = slug
        item.title = str(project.get("title") or "")
        item.locator = "CFWidget（免 key 第三方源）命中 {}".format(slug)
        if not versions:
            item.status = STATUS_NOT_FOUND
            item.manual_url = page
            item.note = "CFWidget 返回的数据里没有可解析的文件条目（第三方源字段可能已变化）"
            return False
        item.manual_url = page
        return self._fill_candidates(item, versions, ranges, SOURCE_CURSEFORGE_CFWIDGET)

    def _curseforge_version(self, mod_id: str, file_obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """CurseForge 文件 -> 版本字典（``downloadUrl`` 为 null 时取临时下载链接）。"""
        url = str(file_obj.get("downloadUrl") or "").strip()
        if not url:
            file_id = str(file_obj.get("id") or "")
            try:
                url = self.curseforge.resolve_download_url(mod_id, file_id)
            except CurseForgeError as exc:
                # 该端点有速率限制：失败只记报告，不崩、不影响其它文件
                self._record_cf_error("CurseForge 文件 {} 取临时下载链接失败：{}".format(file_id, exc))
                self._log("warning", f"AutoSync deps fix: CurseForge 文件 {file_id} 取临时下载链接失败 {exc}")
                url = ""
        return file_version_dict(file_obj, url, source=SOURCE_CURSEFORGE_API)

    def _fill_candidates(
        self,
        item: FixPlanItem,
        versions: Sequence[Dict[str, Any]],
        ranges: Sequence[str],
        source: str,
    ) -> bool:
        """按依赖声明的版本范围过滤 + 按类型各取一个候选（a/b/c），成功返回 True。

        三种来源共用这一段（版本字典结构一致），因此选版规则完全一致。
        """
        candidates, filtered = self._eligible_versions(versions, ranges)
        if filtered and not candidates:
            # 所有版本都不满足依赖声明的范围：不静默下载，标注后仍给候选供人工判断
            item.note = "所有候选版本都不满足依赖要求 {}，已退回全部版本（请人工确认）".format(item.version_range)
            candidates = list(versions)
        elif filtered:
            item.note = "候选已按依赖声明的版本范围过滤"

        picked = self._pick_candidates(item, candidates, source)
        if not picked.candidates:
            item.status = picked.status
            item.note = picked.note
            return False

        first = picked.candidates[0]
        item.candidates = picked.candidates
        item.source = source
        item.version_id = first.version_id
        item.version_number = first.version_number
        item.version_type = first.version_type
        item.date_published = first.date_published
        item.filename = first.filename
        item.url = first.url
        item.size = first.size
        item.sha1 = first.sha1
        item.sha512 = first.sha512
        if all(cand.manual_only for cand in picked.candidates):
            # 第三方源没有可校验直链：列出来给人工下载，绝不自动下载
            item.status = STATUS_MANUAL
            item.manual_url = next(
                (cand.manual_url for cand in picked.candidates if cand.manual_url), item.manual_url
            )
            item.note = "第三方源（{}）没有提供可校验的直链/哈希，禁止自动下载".format(source)
            self._log(
                "warning",
                "AutoSync deps fix: {} 命中 {}，但只有人工下载链接：{}".format(
                    item.mod_id, source, item.manual_url
                ),
            )
            return True
        item.status = STATUS_PLANNED
        return True

    def _record_cf_error(self, message: str) -> None:
        """把 CurseForge 相关失败记入本次 plan 的报告（去重）。"""
        text = str(message or "").strip()
        if text and text not in self._cf_errors:
            self._cf_errors.append(text)


    def _pick_candidates(
        self, item: FixPlanItem, versions: Sequence[Dict[str, Any]], source: str = SOURCE_MODRINTH
    ) -> "_CandidatePick":
        """按版本类型各取一个候选：``a`` = 最新 release，``b`` = 最新 beta，``c`` = 最新 alpha。

        字母与类型**固定对应**，某一类没有就不显示该字母（不顺延）。
        先按依赖声明的 ``versionRange`` 过滤（上游已做），再各取发布时间最新的一个；
        超过 ``deps_fix_max_size_mb`` 的版本会被跳过（继续往下找同类型里能装下的）。
        """
        slots: Dict[str, Optional[FixCandidate]] = {name: None for name in CANDIDATE_TYPE_ORDER}
        allowed = self.allowed_candidate_types
        too_large: List[str] = []
        file_missing = 0
        seen: set = set()
        for version in self._sort_versions(versions):
            version_type = str(version.get("version_type") or "").strip().lower()
            if version_type not in slots or version_type not in allowed:
                continue
            if slots[version_type] is not None:
                continue  # 该类型已取到（排序保证是最新的那个），继续看别的类型
            version_id = str(version.get("id") or "")
            if version_id and version_id in seen:
                continue
            seen.add(version_id)
            candidate = self._candidate_from(version, CANDIDATE_BY_TYPE[version_type], source)
            if candidate is None:
                file_missing += 1
                continue
            if self.max_size_mb > 0 and candidate.size > self.max_size_mb * 1048576:
                too_large.append(candidate.version_number or "?")
                self._log(
                    "warning",
                    "AutoSync deps fix: {}({} {}) 超过 {} MB 上限，不作为候选".format(
                        item.mod_id, version_type, candidate.version_number, self.max_size_mb
                    ),
                )
                continue
            slots[version_type] = candidate

        chosen = [slots[name] for name in CANDIDATE_TYPE_ORDER if slots[name] is not None]
        if chosen:
            return _CandidatePick(candidates=chosen, status=STATUS_PLANNED, note="")
        if too_large:
            return _CandidatePick(
                status=STATUS_TOO_LARGE,
                note="所有候选（{}）都超过 deps_fix_max_size_mb={}，已拒绝下载".format(
                    "、".join(too_large), self.max_size_mb
                ),
            )
        if file_missing:
            return _CandidatePick(status=STATUS_NO_VERSION, note="选中的版本没有可下载文件")
        if not self.show_prerelease:
            return _CandidatePick(
                status=STATUS_NO_VERSION, note="没有正式版（deps_fix_show_prerelease=false，不列预发布版）"
            )
        return _CandidatePick(status=STATUS_NO_VERSION, note="没有可用候选版本")

    # -------------------------------------------------------------- 清单缓存
    def _deps_signature(self, report: Any) -> str:
        """当前 deps 结果的指纹（缺失前置 + 目标环境 + 排除名单）。"""
        payload = {
            "game_version": self.game_version,
            "loader": self.loader,
            "max_size_mb": self.max_size_mb,
            "exclude": sorted(self.exclude),
            "missing": [[str(entry.mod_id), str(entry.version_text)] for entry in (report.missing or [])],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:16]

    def cached_plan(self, report: Any = None) -> Tuple[Optional[FixPlan], str]:
        """读取 ``deps fix`` 生成的清单缓存；不存在/过期/与当前 deps 结果不一致时返回错误。"""
        path = self.plan_cache_path
        if not path.is_file():
            return None, "没有可用的缺失前置清单缓存，请先运行 deps fix 生成清单"
        try:
            with open(path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, ValueError) as exc:
            return None, "缺失前置清单缓存损坏（{}），请重新运行 deps fix".format(exc)
        plan = FixPlan.from_dict(data if isinstance(data, dict) else {})
        age = time.time() - float(plan.generated_ts or 0.0)
        ttl = float(plan.ttl_seconds or PLAN_CACHE_TTL_SECONDS)
        if plan.generated_ts <= 0 or age > ttl:
            return None, "缺失前置清单已过期（生成于 {:.1f} 分钟前，有效期 {} 分钟），请重新运行 deps fix".format(
                age / 60.0, int(ttl // 60) or 1
            )
        current = report if report is not None else self.deps.analyze()
        if not current.ok:
            return None, "依赖检查未完成，无法按编号安装：{}".format(current.message)
        if plan.signature != self._deps_signature(current):
            return None, "依赖结果已变化（当前缺失前置与生成清单时不一致），请重新运行 deps fix"
        return plan, ""

    # -------------------------------------------------------------- 下载
    def apply(self) -> FixOutcome:
        """``deps fix apply``：全选推荐候选（每项取 a），等价于 ``deps fix 1a 2a 3a …``。"""
        started = time.monotonic()
        outcome = FixOutcome(report_path=str(self.report_path))
        plan = self.plan()
        plan.dry_run = False
        self._write_json(self.report_path, plan.to_dict())
        if not plan.ok:
            outcome.ok = False
            outcome.message = plan.message
            outcome.errors.extend(plan.errors)
            outcome.elapsed = time.monotonic() - started
            return outcome
        chosen = [(item, item.candidates[0]) for item in plan.planned if item.candidates]
        chosen_indexes = {item.index for item, _ in chosen}
        outcome.selected = ["{}{}".format(item.index, cand.label) for item, cand in chosen]
        unselected = [item for item in plan.items if item.index not in chosen_indexes]
        return self._download_chosen(plan, chosen, unselected, started, outcome)

    def apply_selection(self, tokens: Sequence[str]) -> FixOutcome:
        """``deps fix <编号...>``：按编号只装选中的候选，未选中的一律不装。

        清单来自 ``deps fix`` 的缓存；缓存不存在/过期/与当前 deps 结果不一致时**拒绝安装**。
        任何无效编号都会导致**一次下载都不执行**。
        """
        started = time.monotonic()
        outcome = FixOutcome(report_path=str(self.report_path))
        report = self.deps.analyze()
        plan, cache_error = self.cached_plan(report)
        if plan is None:
            outcome.ok = False
            outcome.message = cache_error
            outcome.errors.append(cache_error)
            outcome.elapsed = time.monotonic() - started
            self._log("warning", f"AutoSync deps fix: {cache_error}")
            return outcome

        picked, errors = parse_selection_tokens(tokens)
        if not picked and not errors:
            errors.append("没有给出编号（不填编号时不会下载任何文件）")
        chosen: List[Tuple[FixPlanItem, FixCandidate]] = []
        chosen_indexes: set = set()
        for index, label in picked:
            if index in chosen_indexes:
                continue
            item = plan.item_by_index(index)
            if item is None:
                errors.append("无效编号：{}{}（清单里没有编号 {}）".format(index, label, index))
                continue
            candidate = next((cand for cand in item.candidates if cand.label == label), None)
            if candidate is None:
                labels = " ".join(cand.label for cand in item.candidates) or "无"
                errors.append(
                    "无效编号：{}{}（[{}] {} 的可用候选：{}）".format(index, label, index, item.mod_id, labels)
                )
                continue
            chosen_indexes.add(index)
            chosen.append((item, candidate))

        if errors:
            # 有任何一个编号无效就整体放弃：绝不「装一半」
            outcome.ok = False
            outcome.errors.extend(errors)
            outcome.errors.append("可用编号：" + available_selection_text(plan))
            outcome.message = "编号无效，未执行任何下载：{}".format("；".join(errors))
            outcome.elapsed = time.monotonic() - started
            for line in errors:
                self._log("warning", f"AutoSync deps fix: {line}")
            self._log("warning", "AutoSync deps fix: " + outcome.errors[-1])
            return outcome

        outcome.selected = ["{}{}".format(item.index, cand.label) for item, cand in chosen]
        unselected = [item for item in plan.items if item.index not in chosen_indexes]
        return self._download_chosen(plan, chosen, unselected, started, outcome)

    def _download_chosen(
        self,
        plan: FixPlan,
        chosen: Sequence[Tuple[FixPlanItem, FixCandidate]],
        unselected: Sequence[FixPlanItem],
        started: float,
        outcome: FixOutcome,
    ) -> FixOutcome:
        """真正下载选中的候选（哈希校验 + 同名不覆盖），并记录未选中的项。"""
        self.mods_dir.mkdir(parents=True, exist_ok=True)
        self._log(
            "info",
            "AutoSync deps fix：开始下载 {} 个缺失前置到 {}（选择：{}）".format(
                len(chosen), self.mods_dir, " ".join(outcome.selected) or "无"
            ),
        )
        for item, candidate in chosen:
            if candidate.manual_only:
                # 第三方源（CFWidget）没有可校验直链/哈希：绝不自动下载，只提示人工下载
                reason = "第三方源没有可校验的直链/哈希，已跳过自动下载"
                outcome.skipped.append(
                    {
                        "mod_id": item.mod_id,
                        "filename": candidate.filename,
                        "reason": reason,
                        "manual_url": candidate.manual_url or item.manual_url,
                    }
                )
                self._log(
                    "warning",
                    "AutoSync deps fix: [需人工下载] {}（{}）".format(
                        candidate.filename, candidate.manual_url or item.manual_url or "见报告"
                    ),
                )
                continue
            target = self.mods_dir / candidate.filename
            if target.is_file():
                # 已存在同名文件：先比哈希，相同则跳过；不同则绝不覆盖，列入冲突报告
                if self._file_matches(target, candidate):
                    outcome.skipped.append(
                        {"mod_id": item.mod_id, "filename": candidate.filename, "reason": "已存在且哈希一致"}
                    )
                    self._log("info", f"AutoSync deps fix: {candidate.filename} 已存在且哈希一致，跳过")
                else:
                    outcome.conflicts.append(
                        {
                            "mod_id": item.mod_id,
                            "filename": candidate.filename,
                            "target": str(target),
                            "reason": "同名文件已存在但内容不同，未覆盖",
                        }
                    )
                    self._log("warning", f"AutoSync deps fix: [!] {target} 已存在且内容不同，未覆盖（请人工处理）")
                continue
            record, error = self._download(item, candidate, target)
            if record is not None:
                outcome.downloaded.append(record)
                outcome.total_bytes += int(record.get("size") or 0)
            else:
                outcome.failed.append({"mod_id": item.mod_id, "filename": candidate.filename, "reason": error})
                outcome.errors.append(f"{candidate.filename}：{error}")
                self._log("warning", f"AutoSync deps fix: {candidate.filename} 下载失败：{error}")

        for item in unselected:
            outcome.unselected.append(
                {
                    "mod_id": item.mod_id,
                    "index": item.index,
                    "reason": "未选择，已跳过",
                    "note": item.note,
                }
            )
            self._log("info", "AutoSync deps fix: [未选择，已跳过] [{}] {}".format(item.index, item.mod_id))

        outcome.elapsed = time.monotonic() - started
        speed = (outcome.total_bytes / 1048576.0 / outcome.elapsed) if outcome.elapsed > 0 else 0.0
        unselected_text = "、".join("[{}] {}".format(r["index"], r["mod_id"]) for r in outcome.unselected)
        head = (
            "选择性下载完成：选中 {} 项（{}）".format(len(outcome.selected), " ".join(outcome.selected))
            if outcome.selected
            else "下载完成：未选中任何项"
        )
        outcome.message = (
            "{}：成功 {} 个（{:.2f} MB，耗时 {:.2f}s，平均 {:.2f} MB/s），已存在跳过 {} 个，"
            "同名冲突 {} 个，失败 {} 个；未选择，已跳过 {} 个{}".format(
                head,
                len(outcome.downloaded),
                outcome.total_bytes / 1048576.0,
                outcome.elapsed,
                speed,
                len(outcome.skipped),
                len(outcome.conflicts),
                len(outcome.failed),
                len(outcome.unselected),
                "（{}）".format(unselected_text) if unselected_text else "",
            )
        )
        self._log("info", f"AutoSync {outcome.message}")
        self._write_json(
            self.report_path,
            {"plan": plan.to_dict(), "outcome": outcome.to_dict(), "selected": list(outcome.selected)},
        )
        return outcome

    def _file_matches(self, path: Path, candidate: FixCandidate) -> bool:
        """已存在文件的哈希是否与候选一致（没有哈希信息时按「不同」处理，绝不覆盖）。

        优先级 sha512 > sha1 > md5；候选给了多个哈希时按优先级逐个比对。
        """
        try:
            raw = path.read_bytes()
        except OSError:
            return False
        if candidate.sha512:
            if hashlib.sha512(raw).hexdigest().lower() == candidate.sha512.lower():
                return True
            if not (candidate.sha1 or candidate.md5):
                return False
        if candidate.sha1:
            if hashlib.sha1(raw).hexdigest().lower() == candidate.sha1.lower():
                return True
            if not candidate.md5:
                return False
        if candidate.md5:
            return hashlib.md5(raw).hexdigest().lower() == candidate.md5.lower()
        return False

    def _download(self, item: FixPlanItem, candidate: FixCandidate, target: Path) -> Tuple[Optional[Dict[str, Any]], str]:
        """下载单个文件并校验哈希，成功返回 ``(记录, "")``，失败返回 ``(None, 原因)``。"""
        tmp = Path(str(target) + ".part")
        if candidate.manual_only:
            return None, "第三方源没有可校验直链，拒绝自动下载（请人工下载：{}）".format(
                candidate.manual_url or "见报告"
            )
        if candidate.sha512:
            expected, algorithm = candidate.sha512, "sha512"
        elif candidate.sha1:
            expected, algorithm = candidate.sha1, "sha1"
        else:
            expected, algorithm = candidate.md5, "md5"
        if not expected:
            return None, "上游未提供 sha512/sha1/md5 哈希，拒绝写入（无法校验）"
        request = urllib.request.Request(
            candidate.url, headers={"User-Agent": self.user_agent, "Accept": "application/octet-stream"}, method="GET"
        )
        last_error = ""
        for attempt in range(self.max_retries + 1):
            digest512 = hashlib.sha512()
            digest1 = hashlib.sha1()
            digest_md5 = hashlib.md5()
            written = 0
            started = time.monotonic()
            try:
                with self._opener.open(request, timeout=self.timeout) as response, open(tmp, "wb") as fp:
                    while True:
                        chunk = response.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        fp.write(chunk)
                        digest512.update(chunk)
                        digest1.update(chunk)
                        digest_md5.update(chunk)
                        written += len(chunk)
                if algorithm == "sha512":
                    actual = digest512.hexdigest()
                elif algorithm == "sha1":
                    actual = digest1.hexdigest()
                else:
                    actual = digest_md5.hexdigest()
                if actual.lower() != expected.lower():
                    self._safe_unlink(tmp)
                    return None, "哈希校验失败（{} 期望 {}，实际 {}），已删除下载的临时文件".format(
                        algorithm, expected[:16] + "…", actual[:16] + "…"
                    )
            except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, TimeoutError, OSError) as exc:
                last_error = repr(exc)
                self._safe_unlink(tmp)
                if attempt < self.max_retries:
                    delay = min(30.0, 1.5 ** attempt)
                    self._log("warning", f"AutoSync deps fix: {candidate.filename} 下载异常，{delay:.1f}s 后重试")
                    self._sleep(delay)
                continue
            seconds = max(1e-6, time.monotonic() - started)
            try:
                os.replace(tmp, target)
            except OSError as exc:
                self._safe_unlink(tmp)
                return None, f"写入失败：{exc!r}"
            speed = written / 1048576.0 / seconds
            self._log(
                "info",
                "AutoSync deps fix: 完成 {}（{}，{:.2f} MB，{:.2f}s，{:.2f} MB/s）".format(
                    candidate.filename, item.mod_id, written / 1048576.0, seconds, speed
                ),
            )
            return (
                {
                    "mod_id": item.mod_id,
                    "index": item.index,
                    "label": candidate.label,
                    "filename": candidate.filename,
                    "target": str(target),
                    "size": written,
                    "seconds": round(seconds, 3),
                    "speed_mbps": round(speed, 3),
                    "version_number": candidate.version_number,
                    "sha1": digest1.hexdigest(),
                    "source": candidate.source,
                    "verified_by": algorithm,
                },
                "",
            )
        return None, f"重试 {self.max_retries} 次后仍失败：{last_error}"

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    # -------------------------------------------------------------- 报告文本
    @staticmethod
    def _candidate_line(candidate: FixCandidate, version_width: int = 13) -> str:
        """一行候选：``    a) 2.0.5         release  12.4 MB``。

        **每个候选独占一行**（绝不把多个版本拼在一行），行宽尽量 ≤ 60 字符：
        版本号超长会截断（保留可辨识的前半段）；只有第三方源的人工候选才追加「需人工下载」。
        """
        manual = "  [需人工下载]" if candidate.manual_only else ""
        width = max(8, min(MAX_VERSION_CHARS, int(version_width)))
        return truncate_text(
            "    {}) {:<{width}} {:<8}{:>6} MB{}".format(
                candidate.label,
                truncate_text(candidate.version_number, width),
                candidate.version_type or "unknown",
                format_size_mb(candidate.size_mb),
                manual,
                width=width,
            ),
            MAX_LINE_CHARS + 16,
        )

    @staticmethod
    def _item_reason(item: FixPlanItem) -> str:
        """没有候选的项也要在清单里列出原因。"""
        note = item.note or "没有可用候选版本"
        if item.status == STATUS_EXCLUDED:
            return "无可选版本（{}）".format(note)
        if item.status == STATUS_TOO_LARGE:
            return "无可选版本（{}）".format(note)
        if item.status == STATUS_NOT_FOUND:
            return "无法定位（{}）".format(note)
        if item.status == STATUS_MANUAL:
            return "需人工下载（{}）".format(note)
        return "无可选版本（{}）".format(note)

    def _item_lines(self, item: FixPlanItem, annotations: Dict[int, List[str]]) -> List[str]:
        """清单里一个编号项的所有行（短行 + 每行一个候选，方便控制台/聊天框阅读）。"""
        source_tag = " [{}]".format(item.source) if item.source else ""
        if item.source == SOURCE_CURSEFORGE_API and "CFWidget" in item.locator:
            # 文件列表来自官方 API，但项目 ID 是 CFWidget 提供的（官方 search 被 403 禁用）
            source_tag += "（ID 来自 CFWidget）"
        if item.listed:
            lines = [truncate_text("[{}] {}{}".format(item.index, item.mod_id, source_tag))]
            lines.append("    需要方: {}".format(format_dependents(item.dependent_files)))
            if item.version_range and item.version_range != "任意版本":
                lines.append("    要求版本: {}".format(truncate_text(item.version_range, 40)))
            width = max([8] + [len(cand.version_number) for cand in item.candidates])
            for candidate in item.candidates:
                lines.append(self._candidate_line(candidate, width))
            if item.status == STATUS_MANUAL:
                lines.append("    [!] 第三方源无可校验直链，禁止自动下载")
        else:
            lines = [
                truncate_text(
                    "[{}] {}{} —— {}".format(item.index, item.mod_id, source_tag, self._item_reason(item))
                )
            ]
        if item.manual_url and (item.status == STATUS_MANUAL or not item.candidates):
            lines.append("    人工下载: {}".format(truncate_text(item.manual_url, MAX_LINE_CHARS + 20)))
        for warning in annotations.get(item.index, []):
            lines.append("    [!] " + truncate_text(warning, MAX_LINE_CHARS + 16))
        return lines

    @staticmethod
    def _items_block(lines: List[str], block: List[str]) -> None:
        """把一项的多行内容追加进清单，**项与项之间空一行**。"""
        if lines and block:
            lines.append("")
        lines.extend(block)

    def plan_lines(self, plan: FixPlan, detail_limit: int = 200) -> List[str]:
        """完整编号清单（控制台 / CLI 用）。"""
        annotations = plan.conflict_annotations()
        source_text = "、".join(
            "{} {} 个".format(name, count) for name, count in sorted(plan.source_counts.items())
        )
        lines = [
            theme.title("AutoSync 缺失前置清单"),
            theme.marked(theme.MARK_INFO, "目标目录", plan.client_mods_dir),
            theme.kv(
                "目标环境",
                "Minecraft §f{} §7/ 加载器 §f{} §7· 大小上限 §f{} MB §7· 候选 §f{}§7 · 清单缓存 §f{} 分钟".format(
                    plan.game_version,
                    plan.loader,
                    plan.max_size_mb,
                    "a/b/c 各一个" if self.show_prerelease else "只列正式版（a）",
                    int(float(plan.ttl_seconds) // 60) or 1,
                ),
            ),
            theme.kv(
                "缺失统计",
                "真缺失前置 {total} §7个：可下载 {ok}{planned}{reset} §7个（推荐候选合计约 §f{size:.2f} MB{reset}§7），"
                "排除 §f{excluded} §7个，超限 §f{too_large} §7个，无法定位/无版本 §f{not_found} §7个，"
                "需人工下载 {warn}{manual}{reset} §7个".format(
                    total=theme.color_number(plan.missing_total, zero_is_ok=True),
                    ok=theme.C_OK,
                    planned=len(plan.planned),
                    size=plan.planned_bytes / 1048576.0,
                    excluded=sum(1 for i in plan.items if i.status == STATUS_EXCLUDED),
                    too_large=sum(1 for i in plan.items if i.status == STATUS_TOO_LARGE),
                    not_found=sum(
                        1 for i in plan.items if i.status in (STATUS_NOT_FOUND, STATUS_NO_VERSION)
                    ),
                    warn=theme.C_WARN,
                    manual=sum(1 for i in plan.items if i.status == STATUS_MANUAL),
                    reset=theme.C_RESET,
                ),
            ),
            theme.kv("来源", source_text or "（无）"),
        ]
        if not plan.items:
            lines.append(theme.marked(theme.MARK_OK, "结论", "没有真缺失前置，无需下载"))
        shown = 0
        for item in plan.items:
            if shown >= detail_limit:
                break
            shown += 1
            self._items_block(lines, self._item_lines(item, annotations))
        if len(plan.items) > shown:
            lines.append("…（其余 {} 项见 JSON 报告）".format(len(plan.items) - shown))
        if plan.errors:
            lines.append("")
            lines.append(theme.bad("[!] CurseForge/请求失败 {} 条（详见 JSON 报告）：".format(len(plan.errors))))
            for error in plan.errors[:5]:
                lines.append("    " + truncate_text(error, MAX_LINE_CHARS + 20))
        if plan.conflicts:
            lines.append(theme.warn("[!] 互斥判定基于内置的少量已知组名单，覆盖有限，仅供参考，请人工确认"))
        lines.append(theme.hint(USAGE_LINE))
        lines.append(
            theme.warn("[!] 不填编号时什么都不下载；deps fix apply = 全选推荐候选（有正式版取 a，否则取最靠前的 b/c）")
        )
        lines.append(theme.warn("[!] 下载完成后需要手动执行 build 重新构建清单，程序不会自动构建"))
        lines.append(theme.separator())
        return lines

    def plan_chat_lines(self, plan: FixPlan, limit: int = 10) -> List[str]:
        """游戏内回显用的精简清单（控制台/JSON 里是完整版）。

        返回的是**逐行**列表：调用方必须一行一次 print / logger.info，不要拼成多行大字符串。
        """
        annotations = plan.conflict_annotations()
        lines = [
            theme.title("AutoSync 缺失前置清单"),
            theme.marked(
                theme.MARK_INFO,
                "可下载",
                "§f{} §7个 · 大小上限 §f{} MB §7· 清单缓存 §f30 分钟".format(
                    len(plan.planned), plan.max_size_mb
                ),
            ),
        ]
        shown = 0
        for item in plan.items:
            if shown >= limit:
                break
            shown += 1
            self._items_block(lines, self._item_lines(item, annotations))
        if len(plan.items) > shown:
            lines.append(theme.hint("…（其余 {} 项见控制台/JSON 报告）".format(len(plan.items) - shown)))
        if plan.errors:
            lines.append(
                theme.bad("[!] 有 {} 条下载源请求失败（含 CurseForge Key 问题），详见控制台/JSON".format(len(plan.errors)))
            )
        if plan.conflicts:
            lines.append(theme.hint("互斥判定基于内置少量已知组名单，覆盖有限，仅供参考"))
        lines.append(theme.warn(USAGE_LINE))
        lines.append(theme.hint("不填编号时什么都不下载；§fdeps fix apply §7= 全选推荐候选（每项取 a）"))
        lines.append(theme.warn("[!] 下载完成后请手动执行 §fbuild §e重新构建客户端清单"))
        lines.append(theme.separator())
        return lines

    def outcome_lines(self, outcome: FixOutcome) -> List[str]:
        lines = [
            theme.title("AutoSync 缺失前置下载结果（deps fix）"),
            theme.marked(
                theme.MARK_OK if outcome.ok else theme.MARK_BAD,
                "下载结果",
                theme.ok(outcome.message) if outcome.ok else theme.bad(outcome.message),
            ),
        ]
        for record in outcome.downloaded:
            lines.append(
                "  [下载] ({}) {} -> {}（{:.2f} MB，{:.2f}s，{:.2f} MB/s）".format(
                    record.get("label") or "a",
                    record.get("filename"),
                    record.get("target"),
                    (record.get("size") or 0) / 1048576.0,
                    record.get("seconds") or 0.0,
                    record.get("speed_mbps") or 0.0,
                )
            )
        for record in outcome.skipped:
            extra = "；人工下载：{}".format(record.get("manual_url")) if record.get("manual_url") else ""
            lines.append("  [已存在/跳过] {}（{}{}）".format(record.get("filename"), record.get("reason"), extra))
        for record in outcome.conflicts:
            lines.append("  [!] 冲突未覆盖：{}（{}）".format(record.get("target"), record.get("reason")))
        for record in outcome.failed:
            lines.append("  [!] 失败：{}（{}）".format(record.get("filename"), record.get("reason")))
        for record in outcome.unselected:
            lines.append(
                "  [未选择] [{}] {}（未选择，已跳过{})".format(
                    record.get("index"),
                    record.get("mod_id"),
                    "：" + str(record.get("note")) if record.get("note") else "",
                )
            )
        if outcome.conflicts:
            lines.append(theme.warn("[!] 同名冲突文件一律不覆盖，请人工决定保留哪一个"))
        lines.append(theme.warn("[!] 下载完成后请手动执行 build 重新构建客户端清单"))
        lines.append(theme.separator())
        return lines

    # -------------------------------------------------------------- 落盘
    def _write_json(self, path: Path, payload: Dict[str, Any]) -> None:
        tmp = Path(str(path) + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError as exc:
            self._log("warning", f"deps fix 报告写入失败 {path}：{exc!r}")
