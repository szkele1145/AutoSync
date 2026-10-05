"""CurseForge 兜底下载源（仅标准库）—— ``deps fix`` 在 Modrinth 定位失败/无匹配版本时使用。

两条路径，按优先级：

* **官方 API v1**（:class:`CurseForgeClient`）—— 需要配置 ``curseforge_api_key``。
  官方 API **强制** ``x-api-key`` 请求头（PCL 之类内置的是它自己申请的 key），
  所以没有 key 时这条路走不通。好处是数据最全：文件带 ``hashes``（sha1/md5），
  可以自动下载并校验哈希。
* **免 key 第三方数据源 CFWidget**（:class:`CFWidgetClient`）—— ``api.cfwidget.com`` 抓的
  CurseForge 项目数据，不需要 key。**第三方数据可能滞后或不完整**，而且通常只给
  「文件页链接」，不给直链与哈希；因此这条路径只用于**定位 + 选版 + 给人工下载链接**，
  不参与自动下载（拿不到哈希就不允许写盘，见 :mod:`autosync.deps_fix` 的哈希校验规则）。

两条都失败时，调用方会在报告里给出人工下载链接建议：
``https://www.curseforge.com/minecraft/mc-mods/<slug>/files``。

本模块只用标准库，可脱离主程序单测（假 CurseForge / 假 CFWidget 服务端即可覆盖）。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .modrinth import build_opener, normalize_proxy

__all__ = [
    "AUTH_HINT",
    "FORBIDDEN_HINT",
    "SEARCH_DISABLED_HINT",
    "CURSEFORGE_GAME_ID",
    "CurseForgeAuthError",
    "CurseForgeClient",
    "CurseForgeError",
    "CurseForgeForbiddenError",
    "CurseForgeSearchDisabled",
    "CFWidgetClient",
    "LOADER_TYPE_IDS",
    "MANUAL_PAGE_TEMPLATE",
    "RELEASE_TYPE_NAMES",
    "SOURCE_CURSEFORGE_API",
    "SOURCE_CURSEFORGE_CFWIDGET",
    "file_version_dict",
    "is_direct_file_url",
    "manual_page_url",
    "normalize_cfwidget_entry",
    "normalize_hashes",
]

#: CurseForge 里 Minecraft 的 gameId 固定是 432
CURSEFORGE_GAME_ID = 432

#: 官方 API ``releaseType`` -> 与 Modrinth 对齐的版本类型名（a/b/c 映射由此决定）
RELEASE_TYPE_NAMES: Dict[int, str] = {1: "release", 2: "beta", 3: "alpha"}

#: CurseForge 官方 hash 算法的 ``algo`` 取值
HASH_ALGO_SHA1 = 1
HASH_ALGO_MD5 = 2

#: 官方 API 的 ``modLoader`` **只认数字** loaderType；配置里写加载器名字时自动映射，
#: 认不出来的值原样传递（方便直接填数字 ID）。
LOADER_TYPE_IDS: Dict[str, int] = {
    "forge": 1,
    "cauldron": 2,
    "liteloader": 3,
    "fabric": 4,
    "quilt": 5,
    "neoforge": 6,
}

#: 来源标记（报告/清单里显示）
SOURCE_CURSEFORGE_API = "CurseForge/官方API"
SOURCE_CURSEFORGE_CFWIDGET = "CurseForge/CFWidget"

#: 兜底失败时给用户的人工下载入口
MANUAL_PAGE_TEMPLATE = "https://www.curseforge.com/minecraft/mc-mods/{slug}/files"

#: 401/403 的提示语（要求：明确说明 Key 无效/未配置，并给出申请地址）
AUTH_HINT = (
    "CurseForge API Key 无效或未配置（HTTP {code}），"
    "请到 https://console.curseforge.com/ 申请后填入 curseforge_api_key；"
    "也可以留空改用免 key 的第三方源 CFWidget（数据可能滞后）"
)

#: 403 专属提示：官方对 ``/v1/mods/search`` 有额外限制，很多合法 key 在这个端点也会 403
FORBIDDEN_HINT = (
    "CurseForge 接口返回 403：该项目或端点不允许通过 API 访问"
    "（作者可以禁止第三方 API 分发，也可能是 Key 没有对应端点权限）；"
    "已回退到免 key 的第三方源 CFWidget（数据可能滞后），也可以手动下载：见报告里的链接"
)

#: search 端点被禁用时的说明（这种情况**静默回退**，不当错误刷日志/报告）
SEARCH_DISABLED_HINT = (
    "CurseForge /v1/mods/search 返回 403：官方对新 key 默认禁用 search 端点"
    "（key 本身有效，其它端点正常），已改用 CFWidget 取项目 ID"
)

#: 直链判定：只有这种域名的链接才是可以真正下载的 CDN 文件
_DIRECT_HOSTS = ("mediafilez.forgecdn.net", "edge.forgecdn.net", "forgecdn.net")


class CurseForgeError(RuntimeError):
    """CurseForge 相关请求失败（网络 / 5xx / 坏 JSON / 其它 4xx）。"""


class CurseForgeAuthError(CurseForgeError):
    """401/403：API Key 无效或未配置。"""


class CurseForgeSearchDisabled(CurseForgeError):
    """403：``/v1/mods/search`` 被禁用（**key 本身有效**）。

    CurseForge 对新申请的 key 默认禁用 search 端点（实测同一把 key：``/v1/games/432``、
    ``/v1/mods/{id}``、``/v1/mods/{id}/files`` 都 200，只有 search 403）。
    这种情况必须**静默回退**：改用 CFWidget 按 slug 拿数字 projectId，再回到官方 API 取文件，
    所以它既不算"Key 无效"，也不该往报告里刷错误。
    """


class CurseForgeForbiddenError(CurseForgeError):
    """403：**项目级**没权限（不是 Key 无效）。

    实测 ``geckoanimfix``（numeric id 959388）在 ``/v1/mods/{id}`` 直接 403，而 JEI
    （238222）同 key 200 —— CurseForge 允许作者禁止第三方 API 分发。这种 403 只影响**这一项**，
    既不能当成 "Key 无效"（不全局禁用官方 API），也不能丢报告。
    """


def manual_page_url(slug: str) -> str:
    """人工下载入口（项目文件列表页）。"""
    return MANUAL_PAGE_TEMPLATE.format(slug=urllib.parse.quote(str(slug or "").strip() or "unknown"))


def is_direct_file_url(url: str) -> bool:
    """该 URL 是否像「可直接下载的文件」而不是项目的网页。

    CFWidget 的 ``url`` 字段多数是 ``curseforge.com/.../download/<id>`` 这样的页面链接，
    直接 GET 会拿到 HTML；只有 CDN 域名（或明确以 .jar 结尾）才认为可以下载。
    """
    text = str(url or "").strip()
    if not text:
        return False
    parsed = urllib.parse.urlparse(text)
    host = (parsed.netloc or "").lower()
    if any(host.endswith(item) for item in _DIRECT_HOSTS):
        return True
    path = (parsed.path or "").lower()
    if host.endswith("curseforge.com"):
        return False  # 网页链接，绝不当直链用
    return path.endswith(".jar") or path.endswith(".zip")


def normalize_hashes(raw: Any) -> Dict[str, str]:
    """把官方 API 的 ``hashes`` 数组/字典统一成 ``{"sha1": ..., "md5": ...}``。"""
    hashes: Dict[str, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            name = str(key).strip().lower()
            if name in ("sha1", "md5", "sha512") and str(value or "").strip():
                hashes[name] = str(value).strip().lower()
        return hashes
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            value = str(item.get("value") or "").strip().lower()
            if not value:
                continue
            try:
                algo = int(item.get("algo") or 0)
            except (TypeError, ValueError):
                algo = 0
            if algo == HASH_ALGO_SHA1:
                hashes["sha1"] = value
            elif algo == HASH_ALGO_MD5:
                hashes["md5"] = value
            elif algo == 3:  # pragma: no cover - 官方目前不使用 3（预留 sha512）
                hashes["sha512"] = value
    return hashes


def file_version_dict(
    file_obj: Dict[str, Any],
    download_url: str,
    *,
    source: str,
    manual_only: bool = False,
    manual_url: str = "",
) -> Dict[str, Any]:
    """把 CurseForge 文件对象转成与 Modrinth 版本**同形**的字典，复用现有选版逻辑。

    结构与 ``GET /v2/project/{id}/version`` 的元素一致（``id`` / ``version_number`` /
    ``version_type`` / ``date_published`` / ``files``），因此 ``_eligible_versions``、
    ``_pick_candidates``、``_candidate_from`` 都不用改。
    """
    filename = str(file_obj.get("fileName") or file_obj.get("name") or "").strip()
    version_number = str(file_obj.get("displayName") or filename).strip() or filename
    hashes = normalize_hashes(file_obj.get("hashes"))
    return {
        "id": str(file_obj.get("id") or ""),
        "version_number": version_number,
        "version_type": release_type_name(file_obj.get("releaseType") or file_obj.get("type")),
        "date_published": _date_text(file_obj.get("fileDate") or file_obj.get("uploaded_at")),
        "source": source,
        "manual_only": bool(manual_only),
        "manual_url": str(manual_url or ""),
        "files": [
            {
                "url": str(download_url or ""),
                "filename": filename,
                "primary": True,
                "size": _as_int(file_obj.get("fileLength") or file_obj.get("size")),
                "hashes": hashes,
            }
        ],
    }


def release_type_name(value: Any) -> str:
    """``releaseType``/``type`` 归一：1/2/3 或 "release"/"beta"/"alpha" 都接受。"""
    text = str(value if value is not None else "").strip().lower()
    if text.isdigit():
        return RELEASE_TYPE_NAMES.get(int(text), "")
    return text if text in ("release", "beta", "alpha") else ""


def normalize_cfwidget_entry(entry: Any, project_id: str = "") -> Optional[Dict[str, Any]]:
    """CFWidget 的单个文件条目 -> 版本字典（**防御性解析**：字段缺失就跳过）。

    CFWidget 是第三方抓取源，字段名/结构可能变化，所以这里只认「拿得到文件名」的条目，
    直链缺失时退回人工下载页；哈希缺失时标记 ``manual_only``（不允许自动下载）。
    """
    if not isinstance(entry, dict):
        return None
    filename = str(
        entry.get("name") or entry.get("fileName") or entry.get("display") or entry.get("file") or ""
    ).strip()
    raw_url = str(entry.get("url") or entry.get("downloadUrl") or "").strip()
    direct = is_direct_file_url(raw_url)
    if not filename and direct:
        filename = urllib.parse.unquote(urllib.parse.urlparse(raw_url).path.rsplit("/", 1)[-1])
    if not filename:
        return None
    if not filename.lower().endswith((".jar", ".zip")) and direct:
        pass  # 名字与后缀不一致时仍按 CFWidget 给的名字用
    version_number = str(entry.get("display") or entry.get("title") or filename).strip() or filename
    hashes = normalize_hashes(
        {
            "sha1": entry.get("sha1") or entry.get("sha1_hash"),
            "md5": entry.get("md5") or entry.get("md5_hash"),
        }
    )
    manual_url = raw_url if not direct and raw_url else manual_page_url(str(entry.get("slug") or project_id or ""))
    manual_only = not (direct and hashes)
    return {
        "id": str(entry.get("id") or entry.get("fileId") or filename),
        "version_number": version_number,
        "version_type": release_type_name(entry.get("type") or entry.get("releaseType")),
        "date_published": _date_text(
            entry.get("uploaded_at") or entry.get("date") or entry.get("timestamp") or entry.get("fileDate")
        ),
        "source": SOURCE_CURSEFORGE_CFWIDGET,
        "manual_only": manual_only,
        "manual_url": manual_url,
        "files": [
            {
                "url": raw_url if direct else "",
                "filename": filename,
                "primary": True,
                "size": _as_int(entry.get("size") or entry.get("fileLength") or entry.get("filesize")),
                "hashes": hashes,
            }
        ],
    }


def _date_text(value: Any) -> str:
    """时间戳/ISO 字符串统一成可比较的文本（排序用；拿不到就空串）。"""
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        try:
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(value)))
        except (OverflowError, OSError, ValueError):
            return ""
    text = str(value).strip()
    return text.replace("+00:00", "Z")


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- HTTP
def _retry_delay(attempt: int, retry_after: Optional[str]) -> float:
    if retry_after:
        try:
            return max(0.0, min(60.0, float(retry_after)))
        except (TypeError, ValueError):
            pass
    return min(30.0, 1.5 ** attempt)


def _log(logger: Any, level: str, message: str) -> None:
    if logger is None:
        return
    getattr(logger, level, logger.info)(message)


def request_json(
    url: str,
    *,
    headers: Dict[str, str],
    api_name: str,
    timeout: float,
    max_retries: int,
    logger: Any = None,
    sleeper=time.sleep,
    auth_hint: str = "",
    forbidden_hint: str = "",
    forbidden_error: Any = None,
    opener: Any = None,
) -> Any:
    """带 429 退避 / 重试的 GET JSON（CurseForge 官方 API 与 CFWidget 共用）。

    * 404 -> ``None``（项目/文件不存在是正常分支，交给调用方兜底）；
    * 401 且给了 ``auth_hint`` -> 抛 :class:`CurseForgeAuthError`（Key 无效/未配置）；
    * 403 且给了 ``forbidden_error`` -> 抛该类异常（例如 search 端点被禁用，需静默回退）；
    * 403 且只给了 ``auth_hint`` -> 抛 :class:`CurseForgeAuthError`；
    * 其它 4xx -> 抛 :class:`CurseForgeError`（重试没有意义）；
    * 429/5xx/网络异常 -> 指数退避重试。

    ``opener`` 由调用方按 ``http_proxy`` 配置构造（见 :func:`autosync.modrinth.build_opener`）。
    """
    request = urllib.request.Request(url, headers=headers, method="GET")
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last_error: Optional[BaseException] = None
    for attempt in range(max(0, int(max_retries)) + 1):
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 404:
                return None
            if exc.code == 403 and forbidden_error is not None:
                hint = forbidden_hint or auth_hint
                raise forbidden_error(hint.format(code=exc.code)) from exc
            if exc.code == 403 and forbidden_hint:
                raise CurseForgeForbiddenError(forbidden_hint.format(code=exc.code)) from exc
            if exc.code in (401, 403) and auth_hint:
                raise CurseForgeAuthError(auth_hint.format(code=exc.code)) from exc
            if exc.code != 429 and 400 <= exc.code < 500:
                raise CurseForgeError(f"{api_name} HTTP {exc.code} {exc.reason}") from exc
            delay = _retry_delay(attempt, exc.headers.get("Retry-After") if exc.headers else None)
            _log(logger, "warning", f"{api_name} HTTP {exc.code}，{delay:.1f}s 后重试（第 {attempt + 1} 次）")
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, ValueError) as exc:
            last_error = exc
            delay = _retry_delay(attempt, None)
            _log(logger, "warning", f"{api_name} 请求异常 {exc!r}，{delay:.1f}s 后重试（第 {attempt + 1} 次）")
        if attempt < max(0, int(max_retries)):
            sleeper(delay)
    raise CurseForgeError(f"{api_name} 重试 {max(0, int(max_retries))} 次后仍失败：{last_error!r}")


def loader_param(loader: str) -> str:
    """``modLoader`` 查询参数：加载器名字映射成官方 loaderType 数字，未知值原样传递。"""
    text = str(loader or "").strip().lower()
    if not text:
        return ""
    if text.isdigit():
        return text
    return str(LOADER_TYPE_IDS.get(text, text))


class CurseForgeClient:
    """CurseForge 官方 API v1 客户端（需要 ``curseforge_api_key``）。"""

    def __init__(
        self,
        api_key: str,
        user_agent: str = "AutoSync",
        api_base: str = "https://api.curseforge.com",
        timeout: float = 20.0,
        max_retries: int = 3,
        logger: Any = None,
        sleeper=time.sleep,
        proxy: str = "",
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.user_agent = str(user_agent or "AutoSync")
        self.api_base = str(api_base or "https://api.curseforge.com").rstrip("/")
        self.timeout = float(timeout or 20)
        self.max_retries = max(0, int(max_retries or 0))
        self.logger = logger
        self._sleep = sleeper
        self.proxy = str(proxy or "").strip()
        self._opener = build_opener(self.proxy)
        _address, warning = normalize_proxy(self.proxy)
        if warning:
            _log(logger, "warning", "AutoSync " + warning)

    @property
    def enabled(self) -> bool:
        """没有 key 就不启用官方 API（此时由 CFWidget 兜底）。"""
        return bool(self.api_key)

    def _headers(self) -> Dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }

    def search_mod(self, slug: str) -> Optional[Dict[str, Any]]:
        """按 slug 搜项目，只接受 **slug 精确匹配** 的那一个（大小写不敏感）。

        ⚠️ 很多新 key 在这个端点被 403（:class:`CurseForgeSearchDisabled`），
        调用方要静默回退到 CFWidget 取数字 projectId。
        """
        target = str(slug or "").strip()
        if not target:
            return None
        url = "{}/v1/mods/search?gameId={}&slug={}&pageSize=10".format(
            self.api_base, CURSEFORGE_GAME_ID, urllib.parse.quote(target)
        )
        data = self._get_json(url, forbidden_hint=SEARCH_DISABLED_HINT, forbidden_error=CurseForgeSearchDisabled)
        for mod in _data_list(data):
            if str(mod.get("slug") or "").strip().lower() == target.lower():
                return mod
        return None

    def get_mod(self, mod_id: Any) -> Optional[Dict[str, Any]]:
        """``GET /v1/mods/{modId}``：拿项目信息（CFWidget 只用来提供这个数字 ID）。"""
        if str(mod_id or "").strip() == "":
            return None
        url = "{}/v1/mods/{}".format(self.api_base, urllib.parse.quote(str(mod_id)))
        data = self._get_json(url)
        payload = data.get("data") if isinstance(data, dict) else None
        return payload if isinstance(payload, dict) else None

    def list_files(self, mod_id: Any, game_version: str, mod_loader: str) -> List[Dict[str, Any]]:
        """按游戏版本 + 加载器取文件列表（``pageSize=50``，与需求一致）。"""
        params = [("pageSize", "50")]
        if str(game_version or "").strip():
            params.append(("gameVersion", str(game_version).strip()))
        loader = loader_param(mod_loader)
        if loader:
            params.append(("modLoader", loader))
        url = "{}/v1/mods/{}/files?{}".format(
            self.api_base, urllib.parse.quote(str(mod_id)), urllib.parse.urlencode(params)
        )
        return _data_list(self._get_json(url))

    def resolve_download_url(self, mod_id: Any, file_id: Any) -> str:
        """``downloadUrl`` 为 null 时取临时下载链接（该端点有速率限制，失败由调用方兜住）。"""
        url = "{}/v1/mods/{}/files/{}/download-url".format(
            self.api_base, urllib.parse.quote(str(mod_id)), urllib.parse.quote(str(file_id))
        )
        data = self._get_json(url)
        if isinstance(data, dict):
            return str(data.get("data") or "").strip()
        return str(data or "").strip()

    def _get_json(self, url: str, forbidden_hint: str = FORBIDDEN_HINT, forbidden_error: Any = None) -> Any:
        return request_json(
            url,
            headers=self._headers(),
            api_name="CurseForge",
            timeout=self.timeout,
            max_retries=self.max_retries,
            logger=self.logger,
            sleeper=self._sleep,
            auth_hint=AUTH_HINT,
            forbidden_hint=forbidden_hint,
            forbidden_error=forbidden_error or CurseForgeForbiddenError,
            opener=self._opener,
        )


class CFWidgetClient:
    """免 key 第三方数据源 CFWidget（``https://api.cfwidget.com``）。

    注意：第三方抓取源，**数据可能滞后或不完整**；只用来定位/选版/给人工下载链接。
    """

    def __init__(
        self,
        user_agent: str = "AutoSync",
        api_base: str = "https://api.cfwidget.com",
        timeout: float = 20.0,
        max_retries: int = 3,
        logger: Any = None,
        sleeper=time.sleep,
        proxy: str = "",
    ) -> None:
        self.user_agent = str(user_agent or "AutoSync")
        self.api_base = str(api_base or "https://api.cfwidget.com").rstrip("/")
        self.timeout = float(timeout or 20)
        self.max_retries = max(0, int(max_retries or 0))
        self.logger = logger
        self._sleep = sleeper
        self.proxy = str(proxy or "").strip()
        self._opener = build_opener(self.proxy)
        _address, warning = normalize_proxy(self.proxy)
        if warning:
            _log(logger, "warning", "AutoSync " + warning)

    def fetch_project(self, slug: str) -> Optional[Dict[str, Any]]:
        """取项目 JSON；结构与字段以实际请求为准，解析侧全部做防御性处理。"""
        target = str(slug or "").strip()
        if not target:
            return None
        url = "{}/minecraft/mc-mods/{}".format(self.api_base, urllib.parse.quote(target))
        data = self._get_json(url)
        if not isinstance(data, dict):
            return None
        if data.get("error") or data.get("message") and not (data.get("files") or data.get("download")):
            # CFWidget 用 {"error": "..."} / {"message": "..."} 表示查不到
            return None
        return data

    def _get_json(self, url: str) -> Any:
        return request_json(
            url,
            headers={"Accept": "application/json", "User-Agent": self.user_agent},
            api_name="CFWidget",
            timeout=self.timeout,
            max_retries=self.max_retries,
            logger=self.logger,
            sleeper=self._sleep,
            opener=self._opener,
        )


def _data_list(data: Any) -> List[Dict[str, Any]]:
    """官方 API 统一包一层 ``{"data": ...}``；这里容忍裸列表/裸字典。"""
    if isinstance(data, dict):
        payload = data.get("data", data)
    else:
        payload = data
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def cfwidget_versions(project: Dict[str, Any], slug: str) -> Tuple[List[Dict[str, Any]], str]:
    """把 CFWidget 项目 JSON 转成版本字典列表，返回 ``(版本列表, 人工下载页)``。

    防御性解析：``download``（单个最新文件）与 ``files``（列表）都尝试，
    字段缺失/结构变化的条目直接跳过，绝不抛异常。
    """
    entries: List[Any] = []
    single = project.get("download")
    if isinstance(single, dict):
        entries.append(single)
    listed = project.get("files")
    if isinstance(listed, list):
        entries.extend(listed)
    elif isinstance(listed, dict):  # 少数情况下按版本号分组
        for value in listed.values():
            if isinstance(value, list):
                entries.extend(value)
            elif isinstance(value, dict):
                entries.append(value)

    project_id = str(project.get("id") or slug)
    versions: List[Dict[str, Any]] = []
    seen = set()
    for entry in entries:
        version = normalize_cfwidget_entry(entry, project_id)
        if version is None:
            continue
        key = (version["id"], version["version_number"])
        if key in seen:
            continue
        seen.add(key)
        versions.append(version)
    page = manual_page_url(str(project.get("slug") or slug))
    for version in versions:
        if not version.get("manual_url"):
            version["manual_url"] = page
    return versions, page
