"""分发目录扫描 + SHA-256 / SHA-1 计算。

同样只用标准库，方便脱离主程序单测。
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

__all__ = ["ScannedFile", "scan_dist_dir", "hash_file", "snapshot_dir", "is_excluded"]

_CHUNK = 1024 * 1024  # 1 MiB


@dataclass
class ScannedFile:
    """分发目录中的一个文件。"""

    rel_path: str  # 正斜杠相对路径，例如 "mods/[机械动力] create-1.21.1-6.0.10.jar"
    abs_path: Path
    size: int
    sha256: str = ""
    sha1: str = ""
    mtime_ns: int = 0


def is_excluded(rel_path: str, patterns: List[str]) -> bool:
    """相对路径或文件名命中任一 glob 即排除。"""
    name = rel_path.rsplit("/", 1)[-1]
    for pattern in patterns:
        if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(name, pattern):
            return True
    return False


def iter_files(
    dist_dir: Path,
    exclude_globs: Optional[List[str]] = None,
    follow_symlinks: bool = False,
) -> List[Tuple[str, Path, int, int]]:
    """递归列出 (rel_path, abs_path, size, mtime_ns)。

    只返回普通文件；目录符号链接默认不跟随，避免扫描跑到分发目录外。
    """
    patterns = list(exclude_globs or [])
    result: List[Tuple[str, Path, int, int]] = []
    root = Path(dist_dir)
    if not root.is_dir():
        return result
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        # 就地排序，保证输出稳定（便于测试与差分）
        dirnames.sort()
        filenames.sort()
        if not follow_symlinks:
            # os.walk 对 junction 的处理在 Windows 上不一致，这里显式剔除目录链接
            dirnames[:] = [
                d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))
            ]
        for name in filenames:
            abs_path = Path(dirpath) / name
            try:
                st = os.stat(abs_path)  # 跟随文件软链接
            except OSError:
                continue
            if not os.path.isfile(abs_path):
                continue
            rel = abs_path.relative_to(root).as_posix()
            if is_excluded(rel, patterns):
                continue
            result.append((rel, abs_path, st.st_size, st.st_mtime_ns))
    result.sort(key=lambda item: item[0])
    return result


def hash_file(path: Path, chunk_size: int = _CHUNK) -> Tuple[str, str]:
    """一次读取同时算出 (sha256, sha1)。"""
    sha256 = hashlib.sha256()
    sha1 = hashlib.sha1()
    with open(path, "rb") as fp:
        while True:
            chunk = fp.read(chunk_size)
            if not chunk:
                break
            sha256.update(chunk)
            sha1.update(chunk)
    return sha256.hexdigest(), sha1.hexdigest()


def scan_dist_dir(
    dist_dir: Path,
    exclude_globs: Optional[List[str]] = None,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> List[ScannedFile]:
    """扫描并哈希全部文件。``progress`` 回调参数为 (已完成, 总数, 当前相对路径)。"""
    entries = iter_files(dist_dir, exclude_globs)
    total = len(entries)
    scanned: List[ScannedFile] = []
    for index, (rel, abs_path, size, mtime_ns) in enumerate(entries, start=1):
        sha256, sha1 = hash_file(abs_path)
        scanned.append(
            ScannedFile(
                rel_path=rel,
                abs_path=abs_path,
                size=size,
                sha256=sha256,
                sha1=sha1,
                mtime_ns=mtime_ns,
            )
        )
        if progress is not None:
            progress(index, total, rel)
    return scanned


def snapshot_dir(
    dist_dir: Path, exclude_globs: Optional[List[str]] = None
) -> Dict[str, List[int]]:
    """廉价的文件快照 {rel_path: [size, mtime_ns]}，用于定时轮询时避免重复哈希。"""
    snap: Dict[str, List[int]] = {}
    for rel, _abs_path, size, mtime_ns in iter_files(dist_dir, exclude_globs):
        snap[rel] = [size, mtime_ns]
    return snap


def snapshot_key(snap: Dict[str, List[int]]) -> str:
    """把快照压成一个字符串指纹。"""
    h = hashlib.sha256()
    for rel in sorted(snap):
        h.update(rel.encode("utf-8", "surrogatepass"))
        h.update(b"\0")
        h.update(str(snap[rel][0]).encode())
        h.update(b"\0")
        h.update(str(snap[rel][1]).encode())
        h.update(b"\n")
    return h.hexdigest()


def dir_total_size(entries: List[ScannedFile]) -> int:
    return sum(entry.size for entry in entries)


def iter_any(entries: Iterator[ScannedFile]) -> Iterator[ScannedFile]:  # pragma: no cover
    return entries
