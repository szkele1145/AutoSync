"""清单数据结构、内容指纹、原子写入。

清单格式（format = 1）：

.. code-block:: json

    {
      "format": 1,
      "version": "3f2a91c7b4d2-7",
      "generated": "2026-10-04T15:30:00+08:00",
      "files": [
        {"path": "mods/xxx.jar", "size": 123456, "sha256": "...", "urls": ["files/mods/xxx.jar", "https://cdn.modrinth.com/..."]}
      ],
      "deletes": ["mods/removed.jar"]
    }
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

__all__ = [
    "MANIFEST_FORMAT",
    "MANIFEST_VERSION_PREFIX",
    "FileRecord",
    "content_digest",
    "next_version",
    "now_iso",
    "build_manifest",
    "write_manifest_atomic",
    "load_manifest",
    "manifest_file_paths",
    "hosted_url",
]

MANIFEST_FORMAT = 1
MANIFEST_VERSION_PREFIX = "v1"


@dataclass
class FileRecord:
    """一个分发文件在清单里的完整信息。"""

    path: str  # 正斜杠相对路径（原始，未编码）
    size: int
    sha256: str
    sha1: str = ""
    urls: List[str] = field(default_factory=list)
    modrinth_url: str = ""
    modrinth_version: str = ""

    def to_manifest_entry(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "urls": list(self.urls),
        }


def hosted_url(rel_path: str, prefix: str, encode: bool = True) -> str:
    """自托管相对 URL，例如 files/mods/xxx.jar（可 percent-encode 以兼容中文名）。"""
    encoded = urllib.parse.quote(rel_path, safe="/") if encode else rel_path
    prefix = (prefix or "").strip("/")
    return f"{prefix}/{encoded}" if prefix else encoded


def now_iso() -> str:
    """带本地时区偏移的 ISO 8601 时间，例如 2026-10-04T15:30:00+08:00。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def content_digest(records: Sequence[FileRecord]) -> str:
    """对 (path, sha256, urls) 排序后取 SHA-256。

    只要有任何文件增删改，指纹必然变化；反之内容一致则指纹一致。
    """
    h = hashlib.sha256()
    for record in sorted(records, key=lambda r: r.path):
        h.update(record.path.encode("utf-8", "surrogatepass"))
        h.update(b"\0")
        h.update(record.sha256.encode("ascii", "replace"))
        h.update(b"\0")
        h.update(str(record.size).encode("ascii"))
        h.update(b"\0")
        h.update("\x1f".join(record.urls).encode("utf-8", "surrogatepass"))
        h.update(b"\n")
    return h.hexdigest()


def next_version(
    digest: str,
    previous_digest: Optional[str],
    previous_version: Optional[str],
    generation: int,
) -> tuple:
    """返回 (version, generation, changed)。

    * 内容指纹与上次一致 -> 原样返回上次的 version（保证内容不变时版本号绝对稳定）；
    * 内容变化 -> 代数 +1，版本号 = ``v1-<指纹前12位>-<代数>``。
    """
    generation = max(0, int(generation or 0))
    if previous_digest and previous_version and digest == previous_digest:
        return previous_version, generation, False
    generation += 1
    return f"{MANIFEST_VERSION_PREFIX}-{digest[:12]}-{generation}", generation, True


def build_manifest(
    records: Iterable[FileRecord],
    version: str,
    deletes: Optional[Iterable[str]] = None,
    generated: Optional[str] = None,
) -> Dict[str, Any]:
    entries = [record.to_manifest_entry() for record in records]
    entries.sort(key=lambda entry: entry["path"])
    return {
        "format": MANIFEST_FORMAT,
        "version": version,
        "generated": generated or now_iso(),
        "files": entries,
        "deletes": sorted(set(deletes or [])),
    }


def write_manifest_atomic(path: Path, manifest: Dict[str, Any]) -> int:
    """先写临时文件再 os.replace，避免客户端读到半截清单。返回字节数。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False)
    data = text.encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fp:
            fp.write(data)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return len(data)


def load_manifest(path: Path) -> Optional[Dict[str, Any]]:
    """读取已有清单，失败（不存在 / 坏 JSON）返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def manifest_file_paths(manifest: Optional[Dict[str, Any]]) -> List[str]:
    if not isinstance(manifest, dict):
        return []
    files = manifest.get("files")
    if not isinstance(files, list):
        return []
    paths: List[str] = []
    for item in files:
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            paths.append(item["path"])
    return paths
