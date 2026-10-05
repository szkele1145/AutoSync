"""测速文件 ``speedtest.bin`` 的生成与复用。

客户端（Mcpatch 等）会在多个更新源之间并发测速，请求 ``<源根>/speedtest.bin``。

设计约束：

* **内容必须固定**：用 ``sha256(seed + 序号)`` 拼接，纯确定性、跨 Python 版本一致，
  且接近不可压缩（避免中间代理 gzip 压缩导致测速失真）；
* **已存在且大小正确就原样复用**：绝不重写，否则 mtime/内容变化会让清单指纹无意义地抖动、
  破坏客户端的 HTTP 缓存；
* 大小默认 512 KiB。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

__all__ = ["SpeedtestInfo", "speedtest_bytes", "ensure_speedtest_file"]

_BLOCK = 32  # sha256 摘要长度
_SEED = b"autosync-speedtest-v1"


@dataclass
class SpeedtestInfo:
    name: str
    size: int
    sha256: str = ""
    written: bool = False
    path: str = ""

    @property
    def url(self) -> str:
        return self.name

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "size": self.size,
            "sha256": self.sha256,
            "written": self.written,
            "path": self.path,
            "url": self.url,
        }


def speedtest_bytes(size: int) -> bytes:
    """确定性地生成 ``size`` 字节。"""
    blocks = size // _BLOCK
    buffer = bytearray()
    for index in range(blocks):
        buffer += hashlib.sha256(_SEED + index.to_bytes(8, "big")).digest()
    remainder = size - blocks * _BLOCK
    if remainder:
        buffer += hashlib.sha256(_SEED + b"tail").digest()[:remainder]
    return bytes(buffer)


def ensure_speedtest_file(
    dist_dir: Path,
    name: str = "speedtest.bin",
    size: int = 524288,
    logger: Any = None,
) -> Optional[SpeedtestInfo]:
    """确保分发目录根部存在内容固定的测速文件。

    :return: :class:`SpeedtestInfo`；``size <= 0`` 或 ``name`` 为空时返回 None。
    """
    if not name or size <= 0:
        return None
    target = Path(dist_dir) / name
    expected = speedtest_bytes(size)
    expected_sha256 = hashlib.sha256(expected).hexdigest()

    if target.is_file():
        try:
            actual_size = target.stat().st_size
        except OSError:
            actual_size = -1
        if actual_size == size:
            if logger is not None:
                logger.debug("测速文件已存在且大小正确，复用：%s（%d 字节）", target, size)
            return SpeedtestInfo(
                name=name, size=size, sha256=expected_sha256, written=False, path=str(target)
            )
        if logger is not None:
            logger.info("测速文件大小不符（%s != %s），重新生成 %s", actual_size, size, target)

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "wb") as fp:
        fp.write(expected)
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(tmp, target)
    if logger is not None:
        logger.info("测速文件已生成：%s（%d 字节，sha256=%s）", target, size, expected_sha256[:12])
    return SpeedtestInfo(name=name, size=size, sha256=expected_sha256, written=True, path=str(target))
