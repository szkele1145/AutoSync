"""Modrinth 批量查询客户端（仅标准库）。

两类查询共用同一套重试 / 429 退避 / 批次隔离机制：

* ``POST /v2/version_files``（:meth:`ModrinthClient.lookup_sha1`）：sha1 -> 版本与下载直链；
* ``GET /v2/projects?ids=[...]``（:meth:`ModrinthClient.lookup_projects`）：project_id ->
  ``client_side`` / ``server_side``（模组分类判定用）。

设计要点：
* 批量请求，单批默认 100 个 hash（Modrinth 上限随请求体大小变化，100 很安全）；
* 单个批次失败（网络错误 / 超时 / 429 / 5xx / 坏 JSON）只记录错误并继续，
  绝不让整个构建失败——查询不到的文件一律降级为"仅服务端托管"；
* 解析时优先挑选哈希与查询 sha1 一致的文件，其次 primary，再次第一个。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "ModrinthHit",
    "ModrinthProject",
    "ModrinthClient",
    "LookupResult",
    "ProjectLookupResult",
    "parse_cached_hits",
    "dump_cached_hits",
    "parse_cached_projects",
    "dump_cached_projects",
    "build_opener",
    "normalize_proxy",
    "SIDE_REQUIRED",
    "SIDE_OPTIONAL",
    "SIDE_UNSUPPORTED",
    "SIDE_UNKNOWN",
    "is_unsupported",
]

#: Modrinth 项目 ``client_side`` / ``server_side`` 的取值
SIDE_REQUIRED = "required"
SIDE_OPTIONAL = "optional"
SIDE_UNSUPPORTED = "unsupported"
SIDE_UNKNOWN = "unknown"

#: 只有这两种前缀能交给 urllib 的 ProxyHandler（标准库**不支持 SOCKS**）
_HTTP_PROXY_SCHEMES = ("http://", "https://")


def normalize_proxy(value: Any) -> Tuple[str, str]:
    """校验 ``http_proxy`` 配置，返回 ``(可用的代理地址, 警告)``。

    * 空 -> ``("", "")``；
    * ``http://`` / ``https://`` -> 原样返回；
    * ``socks5h://`` / ``socks5://``（或其它）-> 返回空代理 + 中文警告：
      urllib 标准库不支持 SOCKS，需要用户自己用本地转换工具（如 privoxy）转成 HTTP 代理。
    """
    text = str(value or "").strip()
    if not text:
        return "", ""
    lowered = text.lower()
    if lowered.startswith(_HTTP_PROXY_SCHEMES):
        return text, ""
    if lowered.startswith(("socks5h://", "socks5://", "socks4://", "socks://")):
        return "", (
            "http_proxy={} 是 SOCKS 代理，Python 标准库不支持（urllib 无 SOCKS 支持）；"
            "已忽略，请改用 http://host:port 的本地转换工具（如 privoxy / gost）".format(text)
        )
    return "", "http_proxy={} 格式无法识别（只支持 http://host:port），已忽略".format(text)


def build_opener(proxy: Any = "") -> urllib.request.OpenerDirector:
    """按 ``http_proxy`` 配置构造 opener（Modrinth / CurseForge 的所有出站请求共用）。

    没配代理时返回一个**不使用系统环境变量代理**的 opener：服务器上残留的
    ``HTTP_PROXY`` 环境变量不会意外影响插件行为。
    """
    address, _warning = normalize_proxy(proxy)
    handlers: List[Any] = []
    if address:
        handlers.append(urllib.request.ProxyHandler({"http": address, "https": address}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)



def is_unsupported(side: Any) -> bool:
    """该侧是否为 ``unsupported``（即这一侧完全跑不了）。"""
    return str(side or "").strip().lower() == SIDE_UNSUPPORTED


@dataclass
class ModrinthHit:
    sha1: str
    url: str
    filename: str
    version_number: str = ""
    project_id: str = ""
    version_id: str = ""
    queried_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "filename": self.filename,
            "version_number": self.version_number,
            "project_id": self.project_id,
            "version_id": self.version_id,
            "queried_at": self.queried_at,
        }

    @classmethod
    def from_dict(cls, sha1: str, data: Dict[str, Any]) -> Optional["ModrinthHit"]:
        url = str(data.get("url") or "")
        if not url:
            return None
        return cls(
            sha1=sha1.lower(),
            url=url,
            filename=str(data.get("filename") or ""),
            version_number=str(data.get("version_number") or ""),
            project_id=str(data.get("project_id") or ""),
            version_id=str(data.get("version_id") or ""),
            queried_at=float(data.get("queried_at") or 0.0),
        )


@dataclass
class ModrinthProject:
    """Modrinth 项目（只取分类判定需要的字段）。"""

    project_id: str
    slug: str = ""
    title: str = ""
    client_side: str = SIDE_UNKNOWN
    server_side: str = SIDE_UNKNOWN
    project_type: str = ""
    downloads: int = 0
    queried_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "slug": self.slug,
            "title": self.title,
            "client_side": self.client_side,
            "server_side": self.server_side,
            "project_type": self.project_type,
            "downloads": self.downloads,
            "queried_at": self.queried_at,
        }

    @classmethod
    def from_dict(cls, project_id: str, data: Dict[str, Any]) -> Optional["ModrinthProject"]:
        if not isinstance(data, dict):
            return None
        return cls(
            project_id=str(project_id),
            slug=str(data.get("slug") or ""),
            title=str(data.get("title") or ""),
            client_side=_normalize_side(data.get("client_side")),
            server_side=_normalize_side(data.get("server_side")),
            project_type=str(data.get("project_type") or ""),
            downloads=int(data.get("downloads") or 0),
            queried_at=float(data.get("queried_at") or 0.0),
        )


def _normalize_side(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in (SIDE_REQUIRED, SIDE_OPTIONAL, SIDE_UNSUPPORTED):
        return text
    return SIDE_UNKNOWN


@dataclass
class LookupResult:
    hits: Dict[str, ModrinthHit] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    requested: int = 0
    batches: int = 0
    failed_batches: int = 0
    elapsed: float = 0.0


@dataclass
class ProjectLookupResult:
    projects: Dict[str, ModrinthProject] = field(default_factory=dict)
    #: 请求了但 Modrinth 没有返回的 project_id（多半是被删除/私有项目）
    missing: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    requested: int = 0
    batches: int = 0
    failed_batches: int = 0
    elapsed: float = 0.0


class ModrinthClient:
    def __init__(
        self,
        user_agent: str,
        api_base: str = "https://api.modrinth.com/v2",
        batch_size: int = 100,
        timeout: float = 20.0,
        max_retries: int = 3,
        logger: Any = None,
        sleeper=time.sleep,
        proxy: str = "",
    ) -> None:
        self.user_agent = user_agent
        self.api_base = api_base.rstrip("/")
        self.batch_size = max(1, int(batch_size))
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self.logger = logger
        self._sleep = sleeper
        #: 出站代理（``http_proxy`` 配置；空 = 直连且忽略系统环境变量代理）
        self.proxy = str(proxy or "").strip()
        self._opener = build_opener(self.proxy)
        _address, warning = normalize_proxy(self.proxy)
        if warning:
            self._log("warning", "AutoSync " + warning)

    # ------------------------------------------------------------------ 日志
    def _log(self, level: str, message: str) -> None:
        if self.logger is None:
            return
        getattr(self.logger, level, self.logger.info)(message)

    # ------------------------------------------------------------------ 主入口
    def lookup_sha1(self, sha1_list: Sequence[str]) -> LookupResult:
        """批量查询。sha1 大小写不敏感，返回的 key 为小写。"""
        uniq: List[str] = []
        seen = set()
        for value in sha1_list:
            key = str(value).strip().lower()
            if len(key) != 40 or key in seen:
                continue
            seen.add(key)
            uniq.append(key)

        result = LookupResult(requested=len(uniq))
        started = time.monotonic()
        if not uniq:
            result.elapsed = 0.0
            return result

        for offset in range(0, len(uniq), self.batch_size):
            batch = uniq[offset : offset + self.batch_size]
            result.batches += 1
            try:
                payload = self._post_version_files(batch)
            except Exception as exc:  # noqa: BLE001 - 单批失败必须被吞掉
                result.failed_batches += 1
                message = f"Modrinth 批次 #{result.batches}（{len(batch)} 个 hash）查询失败：{exc}"
                result.errors.append(message)
                self._log("warning", message)
                continue
            for sha1, version in payload.items():
                hit = self._parse_version(str(sha1).lower(), version)
                if hit is not None:
                    result.hits[hit.sha1] = hit
            self._log(
                "info",
                "Modrinth 批次 #{}/{} 完成：{}/{} 命中".format(
                    result.batches,
                    (len(uniq) + self.batch_size - 1) // self.batch_size,
                    len([s for s in batch if s in result.hits]),
                    len(batch),
                ),
            )
        result.elapsed = time.monotonic() - started
        return result

    def lookup_projects(self, project_ids: Sequence[str]) -> ProjectLookupResult:
        """批量查询项目（``GET /v2/projects?ids=[...]``），拿 client_side / server_side。

        与 :meth:`lookup_sha1` 一样：单批失败只记录不抛出。
        """
        uniq: List[str] = []
        seen = set()
        for value in project_ids:
            key = str(value).strip()
            if not key or key in seen:
                continue
            seen.add(key)
            uniq.append(key)

        result = ProjectLookupResult(requested=len(uniq))
        started = time.monotonic()
        if not uniq:
            result.elapsed = 0.0
            return result

        # project_id 会全部塞进 URL query，批次比 hash 小一些更稳妥
        batch_size = max(1, min(self.batch_size, 50))
        for offset in range(0, len(uniq), batch_size):
            batch = uniq[offset : offset + batch_size]
            result.batches += 1
            try:
                payload = self._get_projects(batch)
            except Exception as exc:  # noqa: BLE001 - 单批失败必须被吞掉
                result.failed_batches += 1
                message = f"Modrinth 项目批次 #{result.batches}（{len(batch)} 个 id）查询失败：{exc}"
                result.errors.append(message)
                self._log("warning", message)
                continue
            returned = set()
            for item in payload:
                if not isinstance(item, dict):
                    continue
                project_id = str(item.get("id") or "")
                if not project_id:
                    continue
                project = ModrinthProject.from_dict(project_id, item)
                if project is None:
                    continue
                project.queried_at = time.time()
                result.projects[project_id] = project
                returned.add(project_id)
            result.missing.extend([pid for pid in batch if pid not in returned])
            self._log(
                "info",
                "Modrinth 项目批次 #{}/{} 完成：{}/{} 命中".format(
                    result.batches,
                    (len(uniq) + batch_size - 1) // batch_size,
                    len(returned),
                    len(batch),
                ),
            )
        result.elapsed = time.monotonic() - started
        return result

    # ------------------------------------------------------------------ HTTP
    def _post_version_files(self, hashes: Sequence[str]) -> Dict[str, Any]:
        url = f"{self.api_base}/version_files"
        body = json.dumps({"hashes": list(hashes), "algorithm": "sha1"}).encode("utf-8")
        data = self._request_json(url, body=body, method="POST")
        if data is None:  # 空响应体：按"本批无命中"处理（与旧行为一致）
            return {}
        if not isinstance(data, dict):
            raise ValueError(f"响应不是 JSON 对象：{type(data).__name__}")
        return data

    def _get_projects(self, project_ids: Sequence[str]) -> List[Any]:
        # Modrinth 要求 ids 是 JSON 数组字符串（URL 里要 percent-encoding）
        ids = urllib.parse.quote(json.dumps(list(project_ids), separators=(",", ":")), safe="")
        url = f"{self.api_base}/projects?ids={ids}"
        data = self._request_json(url, body=None, method="GET")
        if data is None:
            return []
        if not isinstance(data, list):
            raise ValueError(f"响应不是 JSON 数组：{type(data).__name__}")
        return data

    def _request_json(self, url: str, body: Optional[bytes] = None, method: str = "GET") -> Any:
        """带重试 / 429 退避的 JSON 请求。所有查询共用这一条路径。"""
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)

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
                # 4xx（429 除外）重试没有意义
                if exc.code != 429 and 400 <= exc.code < 500:
                    detail = ""
                    try:
                        detail = exc.read()[:200].decode("utf-8", "replace")
                    except Exception:  # noqa: BLE001
                        pass
                    raise RuntimeError(f"HTTP {exc.code} {exc.reason} {detail}".strip()) from exc
                delay = self._retry_delay(attempt, exc.headers.get("Retry-After"))
                self._log("warning", f"Modrinth HTTP {exc.code}，{delay:.1f}s 后重试（第 {attempt + 1} 次）")
            except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, ValueError) as exc:
                last_error = exc
                delay = self._retry_delay(attempt, None)
                self._log("warning", f"Modrinth 请求异常 {exc!r}，{delay:.1f}s 后重试（第 {attempt + 1} 次）")
            if attempt < self.max_retries:
                self._sleep(delay)
        raise RuntimeError(f"重试 {self.max_retries} 次后仍失败：{last_error!r}")

    def _retry_delay(self, attempt: int, retry_after: Optional[str]) -> float:
        if retry_after:
            try:
                return max(0.0, min(60.0, float(retry_after)))
            except (TypeError, ValueError):
                pass
        return min(30.0, 1.5 ** attempt)

    # ------------------------------------------------------------------ 解析
    @staticmethod
    def _parse_version(sha1: str, version: Any) -> Optional[ModrinthHit]:
        if not isinstance(version, dict):
            return None
        files = version.get("files")
        if not isinstance(files, list) or not files:
            return None
        chosen: Optional[Dict[str, Any]] = None
        for candidate in files:
            if not isinstance(candidate, dict):
                continue
            hashes = candidate.get("hashes") or {}
            if isinstance(hashes, dict) and str(hashes.get("sha1", "")).lower() == sha1:
                chosen = candidate
                break
        if chosen is None:
            for candidate in files:
                if isinstance(candidate, dict) and candidate.get("primary"):
                    chosen = candidate
                    break
        if chosen is None:
            chosen = next((c for c in files if isinstance(c, dict)), None)
        if chosen is None:
            return None
        url = str(chosen.get("url") or "")
        if not url:
            return None
        return ModrinthHit(
            sha1=sha1,
            url=url,
            filename=str(chosen.get("filename") or ""),
            version_number=str(version.get("version_number") or ""),
            project_id=str(version.get("project_id") or ""),
            version_id=str(version.get("id") or ""),
            queried_at=time.time(),
        )


def parse_cached_hits(raw: Any) -> Dict[str, ModrinthHit]:
    """把 state.json 里的 modrinth 缓存还原成 ModrinthHit 字典。"""
    hits: Dict[str, ModrinthHit] = {}
    if not isinstance(raw, dict):
        return hits
    for sha1, data in raw.items():
        if isinstance(data, dict):
            hit = ModrinthHit.from_dict(str(sha1), data)
            if hit is not None:
                hits[hit.sha1] = hit
    return hits


def dump_cached_hits(hits: Iterable[ModrinthHit]) -> Dict[str, Any]:
    return {hit.sha1: hit.to_dict() for hit in hits}


def parse_cached_projects(raw: Any) -> Dict[str, ModrinthProject]:
    """把缓存 JSON 还原成 ModrinthProject 字典（键为 project_id）。"""
    projects: Dict[str, ModrinthProject] = {}
    if not isinstance(raw, dict):
        return projects
    for project_id, data in raw.items():
        project = ModrinthProject.from_dict(str(project_id), data)
        if project is not None:
            projects[project.project_id] = project
    return projects


def dump_cached_projects(projects: Iterable[ModrinthProject]) -> Dict[str, Any]:
    return {project.project_id: project.to_dict() for project in projects}
