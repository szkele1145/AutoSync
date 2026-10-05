"""依赖检查（``deps``）：解析 jar 元数据，找出缺失的前置 mod。

真实事故背景：NeoForge 客户端报 ``Missing or unsupported mandatory dependencies``，
缺 ``sable``（被 18 个 mod 依赖）等前置 —— 这类问题**光看文件名看不出来**，
必须读 jar 内的 ``META-INF/neoforge.mods.toml``（Forge 回退 ``META-INF/mods.toml``）。

本模块提供：

* :func:`read_mod_toml` / :func:`parse_mod_toml`：极简 TOML 解析，只取
  ``[[mods]]`` 的 modId / version 与 ``[[dependencies.<ownModId>]]`` 的必需依赖；
* :func:`scan_jar`：扫一个顶层 jar —— 自身 modId、依赖声明，**以及它通过 JiJ
  （Jar-in-Jar）嵌套自带的 modId**（``META-INF/jarjar/`` 与 ``META-INF/jars/``
  下递归读嵌套 jar 的 TOML，最多 :data:`JIJ_MAX_DEPTH` 层、防环、只按名读 entry）；
* :func:`build_dependency_graph`：扫描目录得到 modId → 文件 与「谁依赖它」反查表
  （:mod:`autosync.classify` 也用它保护被依赖的 mod 不被移走）；
* :class:`DependencyService`：报告生成，结论分**三类互不混淆** ——
  「真缺失」（所有层都没提供，**只有这类会打警告**）、
  「JiJ 已提供」（父 mod 自带，不需要单独装）、「无法解析」（没有元数据的 jar）。

为什么必须解析 JiJ：NeoForge 会把父 mod 内嵌的前置一并加载（日志
``Found N dependencies adding them to mods collection`` / ``locator: jarinjar``）。
只扫顶层 jar 时，90 个真实 mod 的数据集上会误报 7 个「缺失前置」，天天报警等于没报警。

解析注意（真踩过的坑）：``[[mods]]`` 段头后面可能跟行内注释
（例如 ``[[mods]] #mandatory``），段头正则必须容忍行尾注释，否则会漏掉该 mod 的 modId。

本模块只用标准库，可脱离主程序单测。
"""

from __future__ import annotations

import io
import json
import os
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import theme
from .config import AutoSyncConfig
from .manifest import now_iso
from .scanner import iter_files

__all__ = [
    "PLATFORM_MOD_IDS",
    "DEP_TYPE_REQUIRED",
    "DEP_TYPE_OPTIONAL",
    "DEP_TYPE_DISCOURAGED",
    "TOML_CANDIDATES",
    "DEPS_REPORT_NAME",
    "JIJ_DIR_PREFIXES",
    "JIJ_METADATA_ENTRY",
    "JIJ_MAX_DEPTH",
    "ModDependency",
    "ModTomlInfo",
    "ModFileRef",
    "NestedModRef",
    "JarScanResult",
    "DependencyGraph",
    "MissingDependency",
    "JijProvidedDependency",
    "VersionMismatch",
    "DependencyReport",
    "DependencyService",
    "read_mod_toml",
    "parse_mod_toml",
    "scan_jar",
    "build_dependency_graph",
    "scan_mod_ids",
    "resolve_server_mods_dir",
    "compare_versions",
    "version_in_range",
    "parse_version_range",
    "find_cycles",
]

#: 平台项：这些「依赖」由加载器/游戏本体提供，不该算作需要分发的 mod
PLATFORM_MOD_IDS: Tuple[str, ...] = ("minecraft", "neoforge", "forge", "fml", "javafml", "lowcodefml")
_PLATFORM_SET: Set[str] = {item.lower() for item in PLATFORM_MOD_IDS}

DEP_TYPE_REQUIRED = "required"
DEP_TYPE_OPTIONAL = "optional"
DEP_TYPE_DISCOURAGED = "discouraged"

#: jar 内可能的模组元数据文件（NeoForge 优先，Forge 兜底）
TOML_CANDIDATES = ("META-INF/neoforge.mods.toml", "META-INF/mods.toml")

#: JiJ（Jar-in-Jar）嵌套 jar 所在的目录前缀。
#: NeoForge 官方约定是 ``META-INF/jarjar/``，但实测相当多 mod（c2me、exposure 等）
#: 把嵌套 jar 放在 ``META-INF/jars/``，而 ``metadata.json`` 里的 ``path`` 才是指路牌 ——
#: 两个目录都要扫，才能不误报。
JIJ_DIR_PREFIXES: Tuple[str, ...] = ("META-INF/jarjar/", "META-INF/jars/")
#: JiJ 描述文件（列出每个嵌套 jar 的 group/artifact/version）
JIJ_METADATA_ENTRY = "META-INF/jarjar/metadata.json"
#: 嵌套递归的最大层数（防 zip 炸弹 / 异常数据）
JIJ_MAX_DEPTH = 3
#: 单个嵌套 jar 允许读入内存的上限（超过则跳过并记录，避免被超大嵌套拖死）
JIJ_MAX_ENTRY_BYTES = 64 * 1024 * 1024

#: 报告文件名（写在插件数据目录里）
DEPS_REPORT_NAME = "deps-report.json"

# --- TOML 极简解析用的正则 -------------------------------------------------
# 段头允许行尾注释：`[[mods]] #mandatory` / `[[dependencies.sable]]  # 前置`
_TOML_ARRAY_TABLE_RE = re.compile(r"^\s*\[\[\s*([^\[\]]+?)\s*\]\]\s*(?:#.*)?$")
_TOML_TABLE_RE = re.compile(r"^\s*\[\s*([^\[\]]+?)\s*\]\s*(?:#.*)?$")
_TOML_KV_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.\-]*)\s*=\s*(.*)$")

# --- 版本范围（Maven 风格）用的正则 ----------------------------------------
_RANGE_BLOCK_RE = re.compile(r"([\[\(])\s*([^,\[\]\(\)]*)\s*,\s*([^,\[\]\(\)]*)\s*([\]\)])")
_EXACT_BLOCK_RE = re.compile(r"\[\s*([^,\[\]\(\)]+?)\s*\]")


# ---------------------------------------------------------------- TOML 读取
def _read_archive_toml(archive: zipfile.ZipFile) -> Tuple[Optional[str], str]:
    """从**已打开**的 zip 里读模组元数据 TOML，返回 ``(文本, 来源 entry)``。

    只读取需要的那几个 entry（``ZipFile.read`` 按名读取），绝不整包解压 ——
    真实数据集单个 jar 可达上百 MB，全解压会慢到不可接受。
    """
    try:
        names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError):
        return None, ""
    for candidate in TOML_CANDIDATES:
        if candidate in names:
            try:
                return archive.read(candidate).decode("utf-8", "replace"), candidate
            except (zipfile.BadZipFile, OSError, KeyError, EOFError, RuntimeError, ValueError):
                return None, ""
    return None, ""


def read_mod_toml(jar_path: Path) -> Tuple[Optional[str], str]:
    """读出 jar 内的模组元数据 TOML，返回 ``(文本, 来源文件名)``；读不到则 ``(None, "")``。"""
    try:
        with zipfile.ZipFile(jar_path) as archive:
            return _read_archive_toml(archive)
    except (zipfile.BadZipFile, OSError, ValueError, KeyError, EOFError, RuntimeError):
        return None, ""


# ---------------------------------------------------------------- TOML 解析
@dataclass
class ModDependency:
    """一条 ``[[dependencies.<owner>]]`` 声明。"""

    mod_id: str
    version_range: str = ""
    dep_type: str = DEP_TYPE_REQUIRED
    owner: str = ""

    @property
    def is_required(self) -> bool:
        """是否必需依赖。

        NeoForge 规定 ``type`` 只能取 required / optional / discouraged，
        缺失或写了别的值时**保守地当作必需**（漏掉一个前置会直接崩客户端，
        多报一个只是噪音，代价不对等）。
        """
        value = str(self.dep_type or "").strip().lower()
        return value not in (DEP_TYPE_OPTIONAL, DEP_TYPE_DISCOURAGED)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "version_range": self.version_range,
            "type": self.dep_type,
            "required": self.is_required,
            "owner": self.owner,
        }


@dataclass
class ModTomlInfo:
    """一个 jar 的元数据解析结果。"""

    source: str = ""
    mod_ids: List[str] = field(default_factory=list)
    versions: Dict[str, str] = field(default_factory=dict)
    dependencies: List[ModDependency] = field(default_factory=list)

    @property
    def required_dependencies(self) -> List[ModDependency]:
        return [dep for dep in self.dependencies if dep.is_required]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "mod_ids": list(self.mod_ids),
            "versions": dict(self.versions),
            "dependencies": [dep.to_dict() for dep in self.dependencies],
        }


def _unquote(text: str) -> str:
    value = str(text or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _toml_value(raw: str) -> str:
    """取等号右边的值，去引号、去行尾注释（``modId="sable" #注释``）。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    if text[0] in "\"'":
        quote = text[0]
        out: List[str] = []
        index = 1
        while index < len(text):
            char = text[index]
            if char == "\\" and index + 1 < len(text):
                nxt = text[index + 1]
                out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
                index += 2
                continue
            if char == quote:
                break
            out.append(char)
            index += 1
        return "".join(out)
    hash_index = text.find("#")
    if hash_index >= 0:
        text = text[:hash_index]
    return text.strip()


def _normalize_section(name: str) -> Tuple[str, str]:
    """把段名拆成 ``(头部, 尾部)``：``dependencies."sable"`` -> ``("dependencies", "sable")``。"""
    parts = str(name or "").split(".", 1)
    head = _unquote(parts[0]).strip().lower()
    tail = _unquote(parts[1]).strip() if len(parts) > 1 else ""
    return head, tail


def parse_mod_toml(text: Optional[str], source: str = "") -> ModTomlInfo:
    """解析 neoforge.mods.toml / mods.toml，只取判定依赖需要的字段。

    * ``[[mods]]``（段头可带行内注释）里的 ``modId`` / ``version``；
    * ``[[dependencies.<modid>]]`` 里的 ``modId`` / ``type`` / ``versionRange``。

    只做逐行解析，不实现完整 TOML —— 这两个文件的结构非常固定，
    引入第三方库反而违背「纯标准库」的约束。
    """
    info = ModTomlInfo(source=source)
    if not text:
        return info

    section = ""
    owner = ""
    pending_mod: Optional[Dict[str, str]] = None
    pending_dep: Optional[Dict[str, str]] = None

    def flush_mod() -> None:
        nonlocal pending_mod
        if pending_mod is not None:
            mod_id = str(pending_mod.get("modId") or "").strip()
            if mod_id and mod_id not in info.mod_ids:
                info.mod_ids.append(mod_id)
                version = str(pending_mod.get("version") or "").strip()
                if version:
                    info.versions.setdefault(mod_id, version)
        pending_mod = None

    def flush_dep() -> None:
        nonlocal pending_dep
        if pending_dep is not None:
            mod_id = str(pending_dep.get("modId") or "").strip()
            if mod_id:
                info.dependencies.append(
                    ModDependency(
                        mod_id=mod_id,
                        version_range=str(pending_dep.get("versionRange") or "").strip(),
                        dep_type=str(pending_dep.get("type") or DEP_TYPE_REQUIRED).strip() or DEP_TYPE_REQUIRED,
                        owner=str(pending_dep.get("owner") or owner),
                    )
                )
        pending_dep = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        match = _TOML_ARRAY_TABLE_RE.match(raw_line)
        if match is not None:
            flush_dep()
            flush_mod()
            head, tail = _normalize_section(match.group(1))
            if head == "mods":
                section = "mods"
                pending_mod = {}
            elif head == "dependencies":
                section = "dependencies"
                owner = tail
                pending_dep = {"owner": tail}
            else:
                section = "other"
            continue

        match = _TOML_TABLE_RE.match(raw_line)
        if match is not None:
            # 单层表会结束「数组表」上下文
            flush_dep()
            flush_mod()
            section = "other"
            continue

        match = _TOML_KV_RE.match(raw_line)
        if match is None:
            continue
        key = match.group(1)
        value = _toml_value(match.group(2))
        if section == "mods" and pending_mod is not None:
            pending_mod[key] = value
        elif section == "dependencies" and pending_dep is not None:
            pending_dep[key] = value

    flush_dep()
    flush_mod()
    return info


# ---------------------------------------------------------------- JiJ（嵌套 jar）
@dataclass
class NestedModRef:
    """一个由 JiJ 嵌套 jar 提供的 modId。

    NeoForge 会把父 mod 内 ``META-INF/jarjar/*.jar``（或 ``META-INF/jars/*.jar``）
    里的 mod 一并加载进 mods 集合（日志里的 ``Found N dependencies adding them to
    mods collection`` / ``locator: jarinjar``），所以这些 modId **不需要单独安装**，
    依赖检查必须把它们算作「本地已有」，否则会大面积误报。
    """

    mod_id: str
    version: str = ""
    #: 提供它的**顶层** mod 文件（相对路径）
    parent_rel: str = ""
    #: 顶层父 jar 的主 modId（用于判断「依赖方自己就带了这个前置」）
    parent_mod_id: str = ""
    #: 嵌套 jar 在（直接）父归档内的 entry 路径
    jar_entry: str = ""
    #: 嵌套层级，1 = 顶层 jar 直接嵌套
    depth: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "version": self.version,
            "parent_file": self.parent_rel,
            "parent_mod_id": self.parent_mod_id,
            "jar_entry": self.jar_entry,
            "depth": self.depth,
        }


@dataclass
class JarScanResult:
    """单个 jar 的扫描结果（自身元数据 + 它通过 JiJ 提供的 modId）。"""

    rel: str = ""
    info: Optional[ModTomlInfo] = None
    source: str = ""
    nested: List[NestedModRef] = field(default_factory=list)
    #: ``父jar!/嵌套entry`` -> 失败原因
    problems: Dict[str, str] = field(default_factory=dict)
    #: 无模组元数据的嵌套 jar 个数（普通库 jar，属正常现象，不算「无法解析」）
    plain_libs: int = 0
    #: 顶层读不出 modId 的原因（有则说明该 jar 自身无法参与分析）
    reason: str = ""

    @property
    def mod_ids(self) -> List[str]:
        return list(self.info.mod_ids) if self.info is not None else []

    @property
    def jij_mod_ids(self) -> List[str]:
        return _unique([ref.mod_id for ref in self.nested])


def _jij_entries(archive: zipfile.ZipFile, names: List[str]) -> Tuple[List[str], List[str]]:
    """找出归档里的嵌套 jar entry，返回 ``(entry 列表, 说明性备注)``。

    优先信 ``META-INF/jarjar/metadata.json``（它明确列出每个嵌套 jar 的 path），
    该文件缺失或损坏时退回「扫描 JiJ 目录下所有 .jar」的兜底策略。
    两类 entry（metadata 列出的 + 两个 JiJ 目录下的 .jar）取**并集**：实测存在
    metadata.json 缺失或列不全的包，漏掉一个嵌套 jar 就会把它提供的 modId 误报成
    「缺失前置」，而误报正是本模块要消灭的问题。
    """
    notes: List[str] = []
    found: List[str] = []
    seen: Set[str] = set()
    name_set = set(names)

    if JIJ_METADATA_ENTRY in name_set:
        try:
            payload = json.loads(archive.read(JIJ_METADATA_ENTRY).decode("utf-8", "replace"))
        except (zipfile.BadZipFile, OSError, KeyError, EOFError, RuntimeError, ValueError) as exc:
            notes.append(f"{JIJ_METADATA_ENTRY} 读取/解析失败（{exc!r}），已按目录扫描兜底")
            payload = None
        if isinstance(payload, dict):
            entries = payload.get("jars")
            if isinstance(entries, list):
                for item in entries:
                    if not isinstance(item, dict):
                        continue
                    path = str(item.get("path") or "").strip().lstrip("/")
                    if not path:
                        continue
                    if path not in name_set:
                        notes.append(f"metadata.json 指向的 {path} 在 jar 内不存在，已跳过")
                        continue
                    if path not in seen:
                        seen.add(path)
                        found.append(path)
            else:
                notes.append("metadata.json 里没有 jars 列表，已按目录扫描兜底")
        elif payload is not None:
            notes.append("metadata.json 顶层不是 JSON 对象，已按目录扫描兜底")

    for name in names:
        if not name.lower().endswith(".jar"):
            continue
        if not any(name.startswith(prefix) for prefix in JIJ_DIR_PREFIXES):
            continue
        if name not in seen:
            seen.add(name)
            found.append(name)
    return found, notes


def _collect_nested(
    archive: zipfile.ZipFile,
    parent_rel: str,
    parent_mod_id: str,
    depth: int,
    visited: Set[str],
    out: List[NestedModRef],
    problems: Dict[str, str],
    counters: Dict[str, int],
) -> None:
    """递归收集 ``archive`` 内 JiJ 嵌套 jar 提供的 modId（有限深度 + 防环）。"""
    if depth > JIJ_MAX_DEPTH:
        return
    try:
        names = list(archive.namelist())
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError):
        return

    entries, notes = _jij_entries(archive, names)
    for note in notes:
        problems.setdefault(f"{parent_rel}!/{JIJ_METADATA_ENTRY}", note)

    for entry in entries:
        if entry in visited:
            continue  # 防环：同一个 entry 只处理一次
        visited.add(entry)
        label = f"{parent_rel}!/{entry}"
        try:
            info_obj = archive.getinfo(entry)
            if info_obj.file_size > JIJ_MAX_ENTRY_BYTES:
                problems[label] = f"嵌套 jar 过大（{info_obj.file_size} 字节），已跳过"
                continue
            raw = archive.read(entry)
        except (zipfile.BadZipFile, OSError, KeyError, EOFError, RuntimeError, ValueError) as exc:
            problems[label] = f"嵌套 jar 读取失败：{exc!r}"
            continue
        try:
            inner = zipfile.ZipFile(io.BytesIO(raw))
        except (zipfile.BadZipFile, OSError, ValueError, EOFError) as exc:
            problems[label] = f"嵌套 jar 损坏：{exc!r}"
            continue
        with inner:
            text, source = _read_archive_toml(inner)
            if text is None:
                # 普通库 jar（caffeine、exp4j 之流）本来就没有 mods.toml，属正常现象，
                # 不能算「无法解析」，否则报告会被噪音淹掉。
                counters["plain_libs"] = counters.get("plain_libs", 0) + 1
            else:
                info = parse_mod_toml(text, source)
                if not info.mod_ids:
                    problems.setdefault(label, f"{source} 里没有解析出 [[mods]] modId")
                for mod_id in info.mod_ids:
                    out.append(
                        NestedModRef(
                            mod_id=mod_id,
                            version=info.versions.get(mod_id, ""),
                            parent_rel=parent_rel,
                            parent_mod_id=parent_mod_id,
                            jar_entry=entry,
                            depth=depth,
                        )
                    )
            _collect_nested(inner, parent_rel, parent_mod_id, depth + 1, visited, out, problems, counters)


def scan_jar(abs_path: Path, rel: str = "") -> JarScanResult:
    """扫描一个顶层 jar：自身 modId + 依赖声明 + 它通过 JiJ 提供的所有 modId。

    嵌套损坏、``metadata.json`` 缺失或格式异常都只记进 ``problems``，不抛异常。
    """
    result = JarScanResult(rel=rel or Path(abs_path).name)
    try:
        with zipfile.ZipFile(abs_path) as archive:
            text, source = _read_archive_toml(archive)
            if text is None:
                result.reason = "读不到 META-INF/neoforge.mods.toml 或 META-INF/mods.toml"
            else:
                result.source = source
                result.info = parse_mod_toml(text, source)
                if not result.info.mod_ids:
                    result.reason = f"{source} 里没有解析出 [[mods]] modId"
            parent_mod_id = result.mod_ids[0] if result.mod_ids else ""
            counters: Dict[str, int] = {}
            _collect_nested(
                archive, result.rel, parent_mod_id, 1, set(), result.nested, result.problems, counters
            )
            result.plain_libs = counters.get("plain_libs", 0)
    except (zipfile.BadZipFile, OSError, ValueError, KeyError, EOFError, RuntimeError) as exc:
        result.reason = f"jar 无法读取：{exc!r}"
    return result


# ---------------------------------------------------------------- 版本比较
def _version_tokens(text: str) -> List[Tuple[int, int, str]]:
    tokens: List[Tuple[int, int, str]] = []
    for part in re.findall(r"\d+|[A-Za-z]+", str(text or "")):
        if part.isdigit():
            tokens.append((0, int(part), ""))
        else:
            tokens.append((1, 0, part.lower()))
    return tokens


def compare_versions(left: str, right: str) -> int:
    """宽松版本比较（数字段按数值比，数字段排在字母段之前）。返回 -1 / 0 / 1。"""
    a = _version_tokens(left)
    b = _version_tokens(right)
    for x, y in zip(a, b):
        if x != y:
            return -1 if x < y else 1
    if len(a) == len(b):
        return 0
    return -1 if len(a) < len(b) else 1


def parse_version_range(spec: str) -> Tuple[List[Tuple[str, bool, str, bool]], str]:
    """解析 Maven 风格版本范围。

    返回 ``(约束列表, 说明)``；约束为 ``(下界, 含下界, 上界, 含上界)``，空字符串表示该侧无界。
    裸版本（如 ``1.0``）在 Maven 语义里只是「软要求」，任何版本都算满足，因此返回空约束。
    """
    text = str(spec or "").strip()
    if not text or text in ("*", "+", "any"):
        return [], "任意版本"

    constraints: List[Tuple[str, bool, str, bool]] = []
    for match in _RANGE_BLOCK_RE.finditer(text):
        constraints.append(
            (
                match.group(2).strip(),
                match.group(1) == "[",
                match.group(3).strip(),
                match.group(4) == "]",
            )
        )
    remainder = _RANGE_BLOCK_RE.sub(" ", text)
    for match in _EXACT_BLOCK_RE.finditer(remainder):
        value = match.group(1).strip()
        if value:
            constraints.append((value, True, value, True))
    if not constraints:
        return [], f"软要求 {text}（Maven 语义：任何版本都满足）"
    return constraints, text


def version_in_range(version: str, spec: str) -> Tuple[Optional[bool], str]:
    """判断版本是否落在范围内。返回 ``(True/False/None, 说明)``，``None`` = 判不了。"""
    constraints, note = parse_version_range(spec)
    if not constraints:
        return True, note
    if not str(version or "").strip():
        return None, f"本地版本未知，无法与 {note} 比较"
    for low, low_inclusive, high, high_inclusive in constraints:
        ok_low = True
        if low:
            cmp_low = compare_versions(version, low)
            ok_low = cmp_low >= 0 if low_inclusive else cmp_low > 0
        ok_high = True
        if high:
            cmp_high = compare_versions(version, high)
            ok_high = cmp_high <= 0 if high_inclusive else cmp_high < 0
        if ok_low and ok_high:
            return True, f"{version} 满足 {note}"
    return False, f"{version} 不满足 {note}"


# ---------------------------------------------------------------- 依赖图
@dataclass
class ModFileRef:
    """某个 modId 在磁盘上的落点。"""

    mod_id: str
    rel_path: str
    abs_path: str
    version: str = ""
    source: str = ""
    #: True 表示该 modId 是这个 jar 通过 JiJ 嵌套自带的（不是独立文件）
    via_jij: bool = False
    #: ``via_jij`` 为 True 时的顶层父 jar 相对路径
    jij_parent: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "rel_path": self.rel_path,
            "abs_path": self.abs_path,
            "version": self.version,
            "source": self.source,
            "via_jij": self.via_jij,
            "jij_parent": self.jij_parent,
        }


@dataclass
class DependencyGraph:
    """目录级依赖图（modId 一律用小写做键）。"""

    #: modId -> 提供它的文件（同一 modId 可能有多个文件，例如重复投放）
    mod_files: Dict[str, List[ModFileRef]] = field(default_factory=dict)
    #: 文件相对路径 -> 该文件声明的 modId 列表
    file_mod_ids: Dict[str, List[str]] = field(default_factory=dict)
    #: 必需依赖的模块 id -> [(依赖方文件, 依赖方 modId)]
    required_depended_by: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)
    #: 每个必需依赖被要求的版本范围（可能多条）
    dep_ranges: Dict[str, List[str]] = field(default_factory=dict)
    #: owner modId -> {被依赖 modId: 版本范围}（只含必需依赖）
    edges: Dict[str, Dict[str, str]] = field(default_factory=dict)
    #: modId -> 本地版本
    local_versions: Dict[str, str] = field(default_factory=dict)
    #: 文件相对路径 -> 解析失败/无元数据的原因
    unparsed: Dict[str, str] = field(default_factory=dict)
    #: 由 JiJ 嵌套 jar 提供的 modId（小写）-> 提供者列表
    jij_mods: Dict[str, List[NestedModRef]] = field(default_factory=dict)
    #: 顶层文件相对路径 -> 它通过 JiJ 提供的 modId（小写）
    file_jij_mod_ids: Dict[str, List[str]] = field(default_factory=dict)
    #: ``父jar!/嵌套entry`` -> 嵌套解析失败原因
    jij_problems: Dict[str, str] = field(default_factory=dict)
    #: 顶层文件相对路径 -> 必须留在客户端的理由（依赖它的其他文件）
    #: 已按「JiJ 自给不算依赖」修正，:mod:`autosync.classify` 直接消费
    protection: Dict[str, List[str]] = field(default_factory=dict)

    scanned: int = 0
    parsed: int = 0
    #: 扫描到的嵌套 jar 个数
    jij_jars: int = 0
    #: 无模组元数据的嵌套 jar 个数（普通库，正常现象）
    jij_plain_libs: int = 0

    def dependents_of(self, mod_id: str) -> List[Tuple[str, str]]:
        """谁（必需地）依赖了这个 modId。"""
        return list(self.required_depended_by.get(str(mod_id or "").lower(), []))

    def file_count_for(self, mod_id: str) -> int:
        return len(self.mod_files.get(str(mod_id or "").lower(), []))

    def is_provided(self, mod_id: str) -> bool:
        """该 modId 是否本地已提供（顶层文件或任意层 JiJ 嵌套 jar）。"""
        key = str(mod_id or "").lower()
        return key in self.mod_files or key in self.jij_mods

    def jij_providers(self, mod_id: str) -> List[NestedModRef]:
        return list(self.jij_mods.get(str(mod_id or "").lower(), []))


def _iter_jars(directory: Path) -> List[Tuple[str, Path]]:
    result: List[Tuple[str, Path]] = []
    for rel, abs_path, _size, _mtime in iter_files(directory, None):
        if rel.lower().endswith(".jar"):
            result.append((rel, abs_path))
    return result


def _compute_protection(graph: DependencyGraph) -> Dict[str, List[str]]:
    """计算「哪些顶层文件必须留在客户端」以及理由（依赖它的文件）。

    语义（这是本模块最容易搞错的地方，写清楚）：

    * 一条必需依赖 ``A -> B`` 只有在 **A 自己没带 B** 时才产生「需要 B 存在」的需求。
      A 自己通过 JiJ 嵌套带了 B 时（``B`` 出现在 ``graph.file_jij_mod_ids[A]``），
      这条依赖是**自给自足**的 —— 此时就算别处还有一份 B 的顶层文件，那份文件
      也不是 A 需要的，不该因此被强制保留（否则「被依赖保护」会把本可移走的顶层 mod 钉死）。
    * 需要 B 时，优先保护**顶层提供者**（既有行为）；若 B 只由某个父 mod 的 JiJ 提供，
      则保护那个**父 mod 文件** —— 它被移走，依赖 B 的 mod 一样会崩。
    * B 完全没人提供 -> 属于「真缺失」，由 :class:`DependencyService` 报警，
      这里不产生任何保护对象。
    """
    protection: Dict[str, List[str]] = {}

    def add(target: str, consumer: str) -> None:
        if not target or not consumer or target == consumer:
            return
        bucket = protection.setdefault(target, [])
        if consumer not in bucket:
            bucket.append(consumer)

    for dep_id, dependents in graph.required_depended_by.items():
        consumers: List[str] = []
        for rel, _owner in dependents:
            if dep_id in graph.file_jij_mod_ids.get(rel, ()):
                continue  # 依赖方自带这个前置，不算对顶层文件的需求
            if rel not in consumers:
                consumers.append(rel)
        if not consumers:
            continue
        targets = [ref.rel_path for ref in graph.mod_files.get(dep_id, [])]
        if not targets:
            targets = [ref.parent_rel for ref in graph.jij_mods.get(dep_id, [])]
        for target in targets:
            for consumer in consumers:
                add(target, consumer)

    for target in protection:
        protection[target].sort()
    return protection


def build_dependency_graph(mods_dir: Path, logger: Any = None) -> DependencyGraph:
    """扫描目录下所有 jar（含 JiJ 嵌套层），构建依赖图。

    单个 jar 解析失败只记录，不影响整体；嵌套 jar 的损坏/metadata 异常同样只记录。
    """
    graph = DependencyGraph()
    root = Path(mods_dir)
    if not root.is_dir():
        return graph
    for rel, abs_path in _iter_jars(root):
        graph.scanned += 1
        scan = scan_jar(abs_path, rel)
        graph.jij_problems.update(scan.problems)
        graph.jij_plain_libs += scan.plain_libs
        # JiJ 提供的 modId 先登记：即使该 jar 自身没有元数据，嵌套层依然有效
        if scan.nested:
            graph.jij_jars += len({(ref.parent_rel, ref.jar_entry) for ref in scan.nested})
            for ref in scan.nested:
                key = ref.mod_id.lower()
                graph.local_versions.setdefault(key, ref.version)
                graph.jij_mods.setdefault(key, []).append(ref)
            graph.file_jij_mod_ids[rel] = sorted({ref.mod_id.lower() for ref in scan.nested})
        if scan.reason:
            reason = scan.reason
            if scan.jij_mod_ids:
                reason += "（但它通过 JiJ 提供了 {} 个 modId：{}）".format(
                    len(scan.jij_mod_ids), ", ".join(scan.jij_mod_ids)
                )
            graph.unparsed[rel] = reason
            continue
        info = scan.info
        assert info is not None  # reason 为空即代表 info 已解析成功
        graph.parsed += 1
        graph.file_mod_ids[rel] = list(info.mod_ids)
        for mod_id in info.mod_ids:
            key = mod_id.lower()
            # 顶层文件优先：同名 modId 若顶层和某个 JiJ 都有，版本比较应以顶层为准
            version = info.versions.get(mod_id, "")
            if version:
                graph.local_versions[key] = version
            else:
                graph.local_versions.setdefault(key, "")
            graph.mod_files.setdefault(key, []).append(
                ModFileRef(
                    mod_id=mod_id,
                    rel_path=rel,
                    abs_path=str(abs_path),
                    version=info.versions.get(mod_id, ""),
                    source=scan.source,
                )
            )
        default_owner = info.mod_ids[0]
        for dep in info.dependencies:
            key = dep.mod_id.lower()
            if key in _PLATFORM_SET:
                continue  # 平台项不需要分发
            if not dep.is_required:
                continue
            owner = dep.owner or default_owner
            graph.edges.setdefault(owner.lower(), {})[key] = dep.version_range
            graph.required_depended_by.setdefault(key, []).append((rel, owner))
            graph.dep_ranges.setdefault(key, []).append(dep.version_range)

    graph.protection = _compute_protection(graph)
    if logger is not None:
        logger.info(
            "AutoSync 依赖图：扫描 %d 个 jar，解析出 modId 的 %d 个，已知 modId %d 个"
            "（其中 JiJ 嵌套提供 %d 个，嵌套 jar %d 个），依赖关系 %d 条，需保护文件 %d 个",
            graph.scanned,
            graph.parsed,
            len(graph.mod_files),
            len(graph.jij_mods),
            graph.jij_jars,
            sum(len(v) for v in graph.edges.values()),
            len(graph.protection),
        )
    return graph


def scan_mod_ids(directory: Path, logger: Any = None) -> Dict[str, List[ModFileRef]]:
    """扫描目录下所有 jar，返回 ``modId（小写） -> [ModFileRef]``（含 JiJ 嵌套提供的）。"""
    index: Dict[str, List[ModFileRef]] = {}
    root = Path(directory)
    if not root.is_dir():
        return index
    for rel, abs_path in _iter_jars(root):
        scan = scan_jar(abs_path, rel)
        if scan.info is not None:
            for mod_id in scan.info.mod_ids:
                index.setdefault(mod_id.lower(), []).append(
                    ModFileRef(
                        mod_id=mod_id,
                        rel_path=rel,
                        abs_path=str(abs_path),
                        version=scan.info.versions.get(mod_id, ""),
                        source=scan.source,
                    )
                )
        for ref in scan.nested:
            index.setdefault(ref.mod_id.lower(), []).append(
                ModFileRef(
                    mod_id=ref.mod_id,
                    rel_path=rel,
                    abs_path=str(abs_path),
                    version=ref.version,
                    source=f"JiJ:{ref.jar_entry}",
                    via_jij=True,
                    jij_parent=rel,
                )
            )
    if logger is not None:
        jij_count = sum(1 for refs in index.values() for ref in refs if ref.via_jij)
        logger.info(
            "AutoSync 依赖检查：在 %s 里找到 %d 个 modId（其中 JiJ 提供 %d 个）",
            root,
            len(index),
            jij_count,
        )
    return index


def find_cycles(edges: Dict[str, Dict[str, str]]) -> List[List[str]]:
    """用 Tarjan 找出规模 > 1 的强连通分量（以及自环）。"""
    index_counter = [0]
    stack: List[str] = []
    lowlink: Dict[str, int] = {}
    index: Dict[str, int] = {}
    on_stack: Dict[str, bool] = {}
    result: List[List[str]] = []

    def strongconnect(node: str) -> None:
        index[node] = index_counter[0]
        lowlink[node] = index_counter[0]
        index_counter[0] += 1
        stack.append(node)
        on_stack[node] = True
        for succ in edges.get(node, {}):
            if succ not in index:
                strongconnect(succ)
                lowlink[node] = min(lowlink[node], lowlink[succ])
            elif on_stack.get(succ):
                lowlink[node] = min(lowlink[node], index[succ])
        if lowlink[node] == index[node]:
            component: List[str] = []
            while True:
                current = stack.pop()
                on_stack[current] = False
                component.append(current)
                if current == node:
                    break
            if len(component) > 1:
                result.append(sorted(component))
            elif component and component[0] in edges.get(component[0], {}):
                result.append(component)  # 自环

    for node in list(edges):
        if node not in index:
            try:
                strongconnect(node)
            except RecursionError:  # 依赖链异常深：放弃精细结果，返回空表
                return result
    return sorted(result)


def resolve_server_mods_dir(config: AutoSyncConfig, dist_dir: Path) -> Path:
    """服务端 mods 目录：``classify_server_mods_dir`` 相对 ``dist_dir`` 解析。"""
    raw = Path(os.path.expanduser(str(getattr(config, "classify_server_mods_dir", "../mods") or "../mods")))
    if raw.is_absolute():
        return Path(os.path.normpath(str(raw)))
    return Path(os.path.normpath(str(Path(dist_dir) / raw)))


# ---------------------------------------------------------------- 报告结构
@dataclass
class MissingDependency:
    """一个「被依赖但分发目录里没有」的 modId。"""

    mod_id: str
    version_ranges: List[str] = field(default_factory=list)
    required_by: List[Dict[str, str]] = field(default_factory=list)
    server_sources: List[ModFileRef] = field(default_factory=list)

    @property
    def version_text(self) -> str:
        values = [item for item in self.version_ranges if str(item or "").strip()]
        if not values:
            return "任意版本"
        unique: List[str] = []
        for item in values:
            if item not in unique:
                unique.append(item)
        return " / ".join(unique)

    @property
    def dependent_files(self) -> List[str]:
        files: List[str] = []
        for item in self.required_by:
            name = item.get("file", "")
            if name and name not in files:
                files.append(name)
        return files

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "version_range": self.version_text,
            "version_ranges": list(self.version_ranges),
            "required_by": [dict(item) for item in self.required_by],
            "dependent_files": self.dependent_files,
            "server_sources": [ref.to_dict() for ref in self.server_sources],
            "copy_from": self.server_sources[0].abs_path if self.server_sources else "",
        }


@dataclass
class JijProvidedDependency:
    """一个「被依赖、但由某个父 mod 的 JiJ 嵌套 jar 自带」的依赖。

    这类**不是问题**：NeoForge 会把嵌套 jar 一并加载进 mods 集合，日志里表现为
    ``Found N dependencies adding them to mods collection`` / ``locator: jarinjar``。
    单独归类展示，是为了让人明白「为什么不用单独装这个前置」。
    """

    mod_id: str
    version_ranges: List[str] = field(default_factory=list)
    providers: List[Dict[str, Any]] = field(default_factory=list)
    required_by: List[Dict[str, str]] = field(default_factory=list)

    @property
    def version_text(self) -> str:
        return " / ".join(_unique([v for v in self.version_ranges if str(v or "").strip()])) or "任意版本"

    @property
    def parent_files(self) -> List[str]:
        names: List[str] = []
        for item in self.providers:
            name = str(item.get("parent_file") or "")
            if name and name not in names:
                names.append(name)
        return names

    @property
    def provided_version(self) -> str:
        for item in self.providers:
            version = str(item.get("version") or "")
            if version:
                return version
        return ""

    @property
    def dependent_files(self) -> List[str]:
        files: List[str] = []
        for item in self.required_by:
            name = item.get("file", "")
            if name and name not in files:
                files.append(name)
        return files

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "version_range": self.version_text,
            "version_ranges": list(self.version_ranges),
            "provided_version": self.provided_version,
            "providers": [dict(item) for item in self.providers],
            "parent_files": self.parent_files,
            "required_by": [dict(item) for item in self.required_by],
            "dependent_files": self.dependent_files,
        }


@dataclass
class VersionMismatch:
    """本地有该 modId，但版本不满足（或无法比较）某条要求。"""

    mod_id: str
    required_range: str
    local_version: str
    detail: str
    required_by: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "required_range": self.required_range,
            "local_version": self.local_version,
            "detail": self.detail,
            "required_by": [dict(item) for item in self.required_by],
        }


@dataclass
class DependencyReport:
    ok: bool = True
    message: str = ""
    generated_at: str = ""
    generated_ts: float = 0.0
    source_dir: str = ""
    source_mods_dir: str = ""
    server_mods_dir: str = ""
    server_mods_exists: bool = False
    jar_count: int = 0
    parsed_files: int = 0
    known_mod_ids: int = 0
    jij_mod_ids: int = 0
    jij_jars: int = 0
    jij_plain_libs: int = 0
    unparsed_files: Dict[str, str] = field(default_factory=dict)
    #: ``父jar!/嵌套entry`` -> 嵌套层解析失败原因（也属于「无法解析」）
    unparsed_jij: Dict[str, str] = field(default_factory=dict)
    missing: List[MissingDependency] = field(default_factory=list)
    jij_provided: List[JijProvidedDependency] = field(default_factory=list)
    cycles: List[List[str]] = field(default_factory=list)
    version_mismatches: List[VersionMismatch] = field(default_factory=list)
    version_unverified: List[VersionMismatch] = field(default_factory=list)
    platform_ignored: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def missing_count(self) -> int:
        return len(self.missing)

    @property
    def jij_provided_count(self) -> int:
        return len(self.jij_provided)

    @property
    def unparsed_count(self) -> int:
        return len(self.unparsed_files) + len(self.unparsed_jij)

    @property
    def copyable_count(self) -> int:
        return sum(1 for item in self.missing if item.server_sources)

    @property
    def total_dependents(self) -> int:
        return sum(len(item.dependent_files) for item in self.missing)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "generated_at": self.generated_at,
            "generated_ts": self.generated_ts,
            "source_dir": self.source_dir,
            "source_mods_dir": self.source_mods_dir,
            "server_mods_dir": self.server_mods_dir,
            "server_mods_exists": self.server_mods_exists,
            "jar_count": self.jar_count,
            "parsed_files": self.parsed_files,
            "known_mod_ids": self.known_mod_ids,
            "jij_mod_ids": self.jij_mod_ids,
            "jij_jars": self.jij_jars,
            "jij_plain_libs": self.jij_plain_libs,
            "unparsed_files": dict(self.unparsed_files),
            "unparsed_jij": dict(self.unparsed_jij),
            "unparsed_count": self.unparsed_count,
            "missing_count": self.missing_count,
            "copyable_count": self.copyable_count,
            "missing": [item.to_dict() for item in self.missing],
            "jij_provided_count": self.jij_provided_count,
            "jij_provided": [item.to_dict() for item in self.jij_provided],
            "cycles": [list(item) for item in self.cycles],
            "version_mismatches": [item.to_dict() for item in self.version_mismatches],
            "version_unverified": [item.to_dict() for item in self.version_unverified],
            "platform_ignored": list(self.platform_ignored),
            "errors": list(self.errors),
            "elapsed": round(self.elapsed, 3),
        }


# ---------------------------------------------------------------- 服务
class DependencyService:
    """``deps``：只读扫描 + 出报告（不改动任何文件）。"""

    def __init__(
        self,
        config: AutoSyncConfig,
        dist_dir: Path,
        data_dir: Path,
        logger: Any = None,
    ) -> None:
        self.config = config
        self.dist_dir = Path(dist_dir)
        self.data_dir = Path(data_dir)
        self.logger = logger
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.report_path = self.data_dir / DEPS_REPORT_NAME

    # -------------------------------------------------------------- 路径
    @property
    def client_mods_dir(self) -> Path:
        return self.dist_dir / "mods"

    @property
    def server_mods_dir(self) -> Path:
        return resolve_server_mods_dir(self.config, self.dist_dir)

    # -------------------------------------------------------------- 日志
    def _log(self, level: str, message: str) -> None:
        if self.logger is None:
            return
        getattr(self.logger, level, self.logger.info)(message)

    # -------------------------------------------------------------- 分析
    def analyze(self) -> DependencyReport:
        started = time.monotonic()
        mods_dir = self.client_mods_dir
        server_dir = self.server_mods_dir
        report = DependencyReport(
            generated_at=now_iso(),
            generated_ts=time.time(),
            source_dir=str(self.dist_dir),
            source_mods_dir=str(mods_dir),
            server_mods_dir=str(server_dir),
            server_mods_exists=server_dir.is_dir(),
            platform_ignored=list(PLATFORM_MOD_IDS),
        )

        self._log("info", f"AutoSync 依赖检查：客户端 mods = {mods_dir}")
        self._log("info", f"AutoSync 依赖检查：服务端 mods = {server_dir}（存在={report.server_mods_exists}）")

        if not mods_dir.is_dir():
            report.ok = False
            report.message = f"客户端 mods 目录不存在：{mods_dir}"
            report.errors.append(report.message)
            self._log("error", f"AutoSync {report.message}")
            report.elapsed = time.monotonic() - started
            return report

        graph = build_dependency_graph(mods_dir, logger=self.logger)
        report.jar_count = graph.scanned
        report.parsed_files = graph.parsed
        report.known_mod_ids = len(graph.mod_files)
        report.jij_mod_ids = len(graph.jij_mods)
        report.jij_jars = graph.jij_jars
        report.jij_plain_libs = graph.jij_plain_libs
        report.unparsed_files = dict(graph.unparsed)
        report.unparsed_jij = dict(graph.jij_problems)
        if report.unparsed_files:
            self._log(
                "info",
                "AutoSync 依赖检查：{} 个 jar 没有可用元数据，无法参与依赖分析".format(len(report.unparsed_files)),
            )
        if report.jij_jars:
            self._log(
                "info",
                "AutoSync 依赖检查：解析了 {} 个 JiJ 嵌套 jar，额外提供 {} 个 modId".format(
                    report.jij_jars, report.jij_mod_ids
                ),
            )

        # ---------------------------------------------------------- 三类划分
        # 1) 真缺失：顶层和所有 JiJ 层都没有提供 —— 这才是要警告的
        # 2) JiJ 已提供：由某个父 mod 的嵌套 jar 自带，不算问题
        # 3) 无法解析：见 unparsed_files / unparsed_jij
        server_index: Dict[str, List[ModFileRef]] = {}
        if report.server_mods_exists:
            server_index = scan_mod_ids(server_dir, logger=self.logger)

        for dep_id, dependents in graph.required_depended_by.items():
            if dep_id in graph.mod_files:
                continue  # 分发目录的顶层已经有
            required_by: List[Dict[str, str]] = []
            seen: Set[Tuple[str, str]] = set()
            for rel, owner in dependents:
                key = (rel, owner)
                if key in seen:
                    continue
                seen.add(key)
                required_by.append({"file": rel, "mod_id": owner})
            required_by.sort(key=lambda entry: (entry.get("file", ""), entry.get("mod_id", "")))

            if dep_id in graph.jij_mods:
                # JiJ 自带：NeoForge 会自动加载，单独归类，不进警告
                report.jij_provided.append(
                    JijProvidedDependency(
                        mod_id=dep_id,
                        version_ranges=list(graph.dep_ranges.get(dep_id, [])),
                        providers=[ref.to_dict() for ref in graph.jij_mods[dep_id]],
                        required_by=required_by,
                    )
                )
                continue

            item = MissingDependency(
                mod_id=dep_id,
                version_ranges=list(graph.dep_ranges.get(dep_id, [])),
                required_by=required_by,
            )
            item.server_sources = list(server_index.get(dep_id, []))
            report.missing.append(item)
        # 被越多 mod 依赖的排越前（最危险）
        report.missing.sort(key=lambda entry: (-len(entry.dependent_files), entry.mod_id))
        report.jij_provided.sort(key=lambda entry: (-len(entry.dependent_files), entry.mod_id))

        # ---------------------------------------------------------- 循环依赖
        report.cycles = find_cycles(graph.edges)

        # ---------------------------------------------------------- 版本范围
        for dep_id, dependents in sorted(graph.required_depended_by.items()):
            if not graph.is_provided(dep_id):
                continue  # 真缺失项已在上面报过
            local_version = graph.local_versions.get(dep_id, "")
            for spec in _unique(graph.dep_ranges.get(dep_id, [])):
                ok, note = version_in_range(local_version, spec)
                if ok is False:
                    report.version_mismatches.append(
                        VersionMismatch(
                            mod_id=dep_id,
                            required_range=spec,
                            local_version=local_version,
                            detail=note,
                            required_by=[{"file": rel, "mod_id": owner} for rel, owner in dependents],
                        )
                    )
                elif ok is None:
                    report.version_unverified.append(
                        VersionMismatch(
                            mod_id=dep_id,
                            required_range=spec,
                            local_version=local_version,
                            detail=note,
                            required_by=[{"file": rel, "mod_id": owner} for rel, owner in dependents],
                        )
                    )

        report.elapsed = time.monotonic() - started
        report.message = (
            "依赖检查完成：扫描 {jar} 个 jar（解析出 modId {parsed} 个，另有 JiJ 嵌套 jar {jij_jars} 个"
            "提供 modId {jij_ids} 个），真缺失前置 {missing} 个（其中 {copyable} 个可在服务端 mods 里找到源文件），"
            "JiJ 已提供 {jij} 个，无法解析 {unparsed} 个，"
            "循环依赖 {cycles} 组，版本不匹配 {mismatch} 条，耗时 {elapsed:.2f}s".format(
                jar=report.jar_count,
                parsed=report.parsed_files,
                jij_jars=report.jij_jars,
                jij_ids=report.jij_mod_ids,
                missing=report.missing_count,
                copyable=report.copyable_count,
                jij=report.jij_provided_count,
                unparsed=report.unparsed_count,
                cycles=len(report.cycles),
                mismatch=len(report.version_mismatches),
                elapsed=report.elapsed,
            )
        )
        self._write_json(self.report_path, report.to_dict())
        self._log("info", f"AutoSync {report.message}")
        if report.missing:
            self._log(
                "warning",
                "AutoSync [!] 发现 {} 个真缺失前置，客户端可能因此崩溃（Missing or unsupported mandatory dependencies）：{}".format(
                    report.missing_count,
                    ", ".join(item.mod_id for item in report.missing[:5]),
                ),
            )
        if report.jij_provided_count:
            self._log(
                "info",
                "AutoSync 依赖检查：{} 个依赖由 JiJ 嵌套自带（无需单独安装），已从缺失警告里排除".format(
                    report.jij_provided_count
                ),
            )
        self._log("info", f"AutoSync 依赖检查报告已写入：{self.report_path}")
        return report

    # -------------------------------------------------------------- 落盘
    def _write_json(self, path: Path, payload: Dict[str, Any]) -> None:
        tmp = Path(str(path) + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError as exc:
            self._log("warning", f"依赖检查报告写入失败 {path}：{exc!r}")

    # -------------------------------------------------------------- 报告文本
    def report_lines(self, report: DependencyReport, detail_limit: int = 200) -> List[str]:
        lines = [
            theme.title("AutoSync 依赖检查"),
            theme.marked(theme.MARK_INFO, "分发目录", report.source_dir),
            theme.marked(theme.MARK_INFO, "客户端 mods", report.source_mods_dir),
            theme.marked(
                theme.MARK_OK if report.server_mods_exists else theme.MARK_WARN,
                "服务端 mods",
                report.server_mods_dir
                + (
                    ""
                    if report.server_mods_exists
                    else "  " + theme.warn("[!] 目录不存在，无法在此查找可复制的源文件")
                ),
            ),
            theme.kv(
                "扫描",
                "jar {jars} 个 §7· 解析出 modId §f{parsed} §7个 · 已知 modId §f{known} §7个{unparsed}".format(
                    jars=report.jar_count,
                    parsed=report.parsed_files,
                    known=report.known_mod_ids,
                    unparsed="§7 · 无元数据/解析失败 {} 个".format(len(report.unparsed_files))
                    if report.unparsed_files
                    else "",
                ),
            ),
            theme.kv(
                "JiJ 嵌套",
                "嵌套 jar {jars} 个（纯库 {libs} 个，无 mods.toml 属正常）§7 · "
                "额外提供 modId §f{ids} §7个（NeoForge 自动加载）".format(
                    jars=report.jij_jars, libs=report.jij_plain_libs, ids=report.jij_mod_ids
                ),
            ),
            theme.kv("忽略平台项", ", ".join(report.platform_ignored) or "无"),
        ]

        if not report.ok:
            lines.append(theme.marked(theme.MARK_BAD, "检查未完成", report.message))
            lines.append(theme.separator())
            return lines

        if report.missing:
            lines.append("")
            lines.append(
                theme.bad(
                    "[!] 真缺失前置 {} 个（顶层与所有 JiJ 嵌套层都没有；客户端会报 "
                    "Missing or unsupported mandatory dependencies）：".format(report.missing_count)
                )
            )
            lines.append("  modId | 版本要求 | 被哪些 mod 依赖")
            for item in report.missing[:detail_limit]:
                lines.append(
                    "  {} | {} | {} 个：{}".format(
                        item.mod_id,
                        item.version_text,
                        len(item.dependent_files),
                        ", ".join(item.dependent_files) if item.dependent_files else "(未知)",
                    )
                )
                for ref in item.server_sources:
                    if ref.via_jij:
                        lines.append(
                            "      → 由服务端 {} 的 JiJ 提供{}（一般无需复制，父 mod 已在客户端）".format(
                                ref.jij_parent or ref.rel_path, f"（版本 {ref.version}）" if ref.version else ""
                            )
                        )
                    else:
                        lines.append(
                            "      → 可从 {} 复制{}".format(
                                ref.abs_path, f"（版本 {ref.version}）" if ref.version else ""
                            )
                        )
                if not item.server_sources and report.server_mods_exists:
                    lines.append("      → 服务端 mods 里也没有，需要自行补上")
                elif not item.server_sources:
                    lines.append("      → 服务端 mods 目录不存在，无法查找可复制的源文件")
            if len(report.missing) > detail_limit:
                lines.append(f"  …（其余 {len(report.missing) - detail_limit} 项见 JSON 报告）")
        else:
            lines.append(theme.ok("[OK] 未发现缺失前置（真缺失 0 个）：所有必需依赖都由顶层文件或 JiJ 嵌套层提供"))

        if report.jij_provided:
            lines.append("")
            lines.append(
                theme.ok(
                    "[OK] JiJ 已提供 {} 个（由父 mod 的嵌套 jar 自带，NeoForge 自动加载，"
                    "**不算缺失、无需单独安装**）：".format(report.jij_provided_count)
                )
            )
            for item in report.jij_provided[:detail_limit]:
                parents = "、".join(item.parent_files) or "(未知父 mod)"
                version = f" {item.provided_version}" if item.provided_version else ""
                lines.append(
                    "  由 {} 通过 JiJ 提供: {}{}".format(parents, item.mod_id, version)
                )
                entry = item.providers[0].get("jar_entry", "") if item.providers else ""
                lines.append(
                    "      要求 {}；被 {} 个 mod 依赖：{}".format(
                        item.version_text,
                        len(item.dependent_files),
                        ", ".join(item.dependent_files) if item.dependent_files else "(未知)",
                    )
                )
                if entry:
                    lines.append(f"      嵌套路径：{entry}（嵌套层级 {item.providers[0].get('depth', 1)}）")
            if len(report.jij_provided) > detail_limit:
                lines.append(f"  …（其余 {len(report.jij_provided) - detail_limit} 项见 JSON 报告）")

        if report.cycles:
            lines.append(theme.warn(f"[!] 循环依赖 {len(report.cycles)} 组（加载顺序可能出问题）："))
            for group in report.cycles:
                lines.append("  " + " <-> ".join(group))
        else:
            lines.append(theme.hint("  循环依赖：无"))

        if report.version_mismatches:
            lines.append(theme.warn(f"[!] 版本范围不匹配 {len(report.version_mismatches)} 条："))
            for item in report.version_mismatches:
                lines.append(
                    "  {} 本地版本 {}，但要求 {}（{}）".format(
                        item.mod_id,
                        item.local_version or "(未知)",
                        item.required_range or "任意版本",
                        item.detail,
                    )
                )
        else:
            lines.append(theme.hint("  版本范围不匹配：无"))

        if report.version_unverified:
            lines.append(theme.hint("无法比较版本（本地版本未知或写法无法解析，仅提示）："))
            for item in report.version_unverified[:20]:
                lines.append(f"  {item.mod_id} 要求 {item.required_range}（{item.detail}）")
            if len(report.version_unverified) > 20:
                lines.append(f"  …（其余 {len(report.version_unverified) - 20} 条见 JSON 报告）")

        if report.unparsed_files or report.unparsed_jij:
            lines.append(
                theme.hint("未参与分析的 jar（没有可用元数据；共 {} 个）：".format(report.unparsed_count))
            )
            for rel, reason in list(report.unparsed_files.items())[:20]:
                lines.append(f"  {rel}：{reason}")
            if len(report.unparsed_files) > 20:
                lines.append(f"  …（其余 {len(report.unparsed_files) - 20} 项见 JSON 报告）")
            for label, reason in list(report.unparsed_jij.items())[:20]:
                lines.append(f"  [嵌套] {label}：{reason}")
            if len(report.unparsed_jij) > 20:
                lines.append(f"  …（其余嵌套 {len(report.unparsed_jij) - 20} 项见 JSON 报告）")
        lines.append(theme.separator())
        return lines

    def report_chat_lines(self, report: DependencyReport, limit: int = 10) -> List[str]:
        """游戏内回显用的精简依赖报告（框 + 关键统计 + 缺失清单，**逐行**）。

        控制台/JSON 里是完整明细；游戏内只保留最需要立刻看到的部分。
        """
        lines = [
            theme.title("AutoSync 依赖检查"),
            theme.marked(
                theme.MARK_OK if report.ok else theme.MARK_BAD,
                "扫描结果",
                "jar {jars} 个 §7· 真缺失 §f{missing} §7个 · JiJ 已提供 §f{jij} §7个".format(
                    jars=report.jar_count, missing=report.missing_count, jij=report.jij_provided_count
                ),
            ),
            theme.kv(
                "其它问题",
                "循环依赖 §f{cycles} §7组 · 版本不匹配 §f{mismatch} §7条 · 无法解析 §f{unparsed} §7个".format(
                    cycles=len(report.cycles),
                    mismatch=len(report.version_mismatches),
                    unparsed=report.unparsed_count,
                ),
            ),
        ]
        if not report.ok:
            lines.append(theme.marked(theme.MARK_BAD, "检查未完成", report.message))
        elif report.missing:
            lines.append("")
            lines.append(theme.bad("[!] 真缺失前置（客户端会崩，请尽快补齐）："))
            for item in report.missing[:limit]:
                location = item.server_sources[0].abs_path if item.server_sources else "服务端 mods 里也没有"
                lines.append(
                    theme.bad(
                        "  缺 {mod}（要求 {version}，被 {count} 个 mod 依赖）§7→ {where}".format(
                            mod=item.mod_id,
                            version=item.version_text,
                            count=len(item.dependent_files),
                            where=location,
                        )
                    )
                )
            if report.missing_count > limit:
                lines.append(theme.hint("  …（其余 {} 个见控制台/JSON 报告）".format(report.missing_count - limit)))
        else:
            lines.append(theme.marked(theme.MARK_OK, "结论", "未发现真缺失前置"))
        lines.append(theme.separator())
        return lines


def _unique(values: Sequence[str]) -> List[str]:
    result: List[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result
