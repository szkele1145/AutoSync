"""AutoSync 核心：构建清单 / 维护状态与缓存 / 托管 MSFP(裸 TCP) 服务 / 定时轮询。

本模块只用标准库，可脱离 shell/CLI 单独调用。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import AutoSyncConfig
from . import __version__, theme
from .tcp_server import PROTOCOL_NAME, AutoSyncTCPService
from .manifest import (
    FileRecord,
    build_manifest,
    content_digest,
    hosted_url,
    load_manifest,
    next_version,
    now_iso,
    write_manifest_atomic,
)
from .modrinth import ModrinthClient, ModrinthHit, dump_cached_hits, parse_cached_hits
from .scanner import ScannedFile, scan_dist_dir, snapshot_dir, snapshot_key
from .speedtest import ensure_speedtest_file

__all__ = ["BuildResult", "AutoSyncCore"]

STATE_VERSION = 1


@dataclass
class BuildResult:
    ok: bool
    message: str = ""
    version: str = ""
    generation: int = 0
    digest: str = ""
    changed: bool = False
    file_count: int = 0
    jar_count: int = 0
    total_size: int = 0
    modrinth_hits: int = 0
    self_hosted_only: int = 0
    manifest_size: int = 0
    deletes: List[str] = field(default_factory=list)
    elapsed: float = 0.0
    hash_seconds: float = 0.0
    modrinth_seconds: float = 0.0
    errors: List[str] = field(default_factory=list)
    busy: bool = False
    dist_dir: str = ""
    manifest_path: str = ""
    speedtest: Optional[Dict[str, Any]] = None
    #: 构建后自动跑依赖检查的结果（不阻断构建）
    deps_checked: bool = False
    #: **真缺失**前置数量（顶层与所有 JiJ 嵌套层都没提供；JiJ 自带的已排除）
    deps_missing: int = 0
    deps_missing_ids: List[str] = field(default_factory=list)
    deps_report_path: str = ""

    @property
    def modrinth_hit_rate(self) -> float:
        if not self.jar_count:
            return 0.0
        return self.modrinth_hits / self.jar_count * 100.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "version": self.version,
            "generation": self.generation,
            "digest": self.digest,
            "changed": self.changed,
            "file_count": self.file_count,
            "jar_count": self.jar_count,
            "total_size": self.total_size,
            "modrinth_hits": self.modrinth_hits,
            "modrinth_hit_rate": round(self.modrinth_hit_rate, 2),
            "self_hosted_only": self.self_hosted_only,
            "manifest_size": self.manifest_size,
            "deletes": list(self.deletes),
            "elapsed": round(self.elapsed, 3),
            "hash_seconds": round(self.hash_seconds, 3),
            "modrinth_seconds": round(self.modrinth_seconds, 3),
            "errors": list(self.errors),
            "busy": self.busy,
            "dist_dir": self.dist_dir,
            "manifest_path": self.manifest_path,
            "speedtest": self.speedtest,
            "deps_checked": self.deps_checked,
            "deps_missing": self.deps_missing,
            "deps_missing_ids": list(self.deps_missing_ids),
            "deps_report_path": self.deps_report_path,
        }


class AutoSyncCore:
    """构建 + 服务 + 轮询的聚合对象。线程安全（构建用非阻塞锁串行化）。"""

    def __init__(
        self,
        config: AutoSyncConfig,
        data_dir: Path,
        base_dir: Path,
        logger: Optional[logging.Logger] = None,
        say: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.config = config
        self.data_dir = Path(data_dir)
        self.base_dir = Path(base_dir)
        self.logger = logger or logging.getLogger("autosync")
        self.say = say

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "state.json"

        self._build_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self.state: Dict[str, Any] = self._load_state()
        self.last_result: Optional[BuildResult] = None
        self.last_snapshot_key: str = str(self.state.get("snapshot_key") or "")
        self._modrinth_cache: Dict[str, ModrinthHit] = parse_cached_hits(self.state.get("modrinth"))

        self._watch_thread: Optional[threading.Thread] = None
        self._watch_stop = threading.Event()
        self._tcp: Optional[AutoSyncTCPService] = None

    # ------------------------------------------------------------------ 路径
    @property
    def dist_dir(self) -> Path:
        raw = Path(os.path.expanduser(str(self.config.dist_dir)))
        return raw if raw.is_absolute() else (self.base_dir / raw)

    @property
    def manifest_path(self) -> Path:
        return self.dist_dir / self.config.manifest_name

    # ------------------------------------------------------------------ 状态
    def _load_state(self) -> Dict[str, Any]:
        try:
            with open(self.state_path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def _save_state(self) -> None:
        payload = dict(self.state)
        payload["state_version"] = STATE_VERSION
        payload["modrinth"] = dump_cached_hits(self._modrinth_cache.values())
        tmp = self.state_path.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_path)
        except OSError as exc:
            self.logger.warning("AutoSync 状态写入失败：%r", exc)

    @property
    def previous_files(self) -> Dict[str, Dict[str, Any]]:
        files = self.state.get("files")
        if isinstance(files, dict) and files:
            return files
        # 退路：直接读上一次落盘的清单
        manifest = load_manifest(self.manifest_path)
        result: Dict[str, Dict[str, Any]] = {}
        for entry in (manifest or {}).get("files", []) or []:
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                result[entry["path"]] = entry
        return result

    # ------------------------------------------------------------------ MSFP 服务
    def tcp_service(self) -> AutoSyncTCPService:
        if self._tcp is None:
            self._tcp = AutoSyncTCPService(
                dist_dir=self.dist_dir,
                host=self.config.tcp_host,
                port=self.config.tcp_port,
                logger=self.logger,
                path_prefix=self.config.hosted_url_prefix,
                idle_timeout=self.config.tcp_idle_timeout_seconds,
            )
        return self._tcp

    def start_tcp(self) -> bool:
        if not self.config.tcp_enabled:
            self.logger.info("MSFP 服务在配置中被禁用（tcp_enabled=false）")
            return False
        return self.tcp_service().start()

    def stop_tcp(self) -> None:
        if self._tcp is not None:
            self._tcp.stop()

    def restart_tcp(self) -> bool:
        return self.tcp_service().restart()

    @property
    def tcp_running(self) -> bool:
        return self._tcp is not None and self._tcp.running

    @property
    def tcp_port(self) -> int:
        return self._tcp.port if self._tcp is not None else self.config.tcp_port

    def endpoint(self) -> str:
        host = self.config.tcp_host
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        return f"{host}:{self.tcp_port}"

    def manifest_path_in_dist(self) -> str:
        return self.config.manifest_name

    def speedtest_path_in_dist(self) -> str:
        return self.config.speedtest_name

    # ------------------------------------------------------------------ 构建
    def build(self, refresh_modrinth: bool = False) -> BuildResult:
        if not self._build_lock.acquire(blocking=False):
            self.logger.warning("已有构建任务在运行，忽略本次请求")
            self.last_result = BuildResult(ok=False, busy=True, message="已有构建任务在运行")
            return self.last_result
        try:
            result = self._build_locked(refresh_modrinth)
        except Exception as exc:  # noqa: BLE001 - 构建失败不能把插件/服务器带崩
            self.logger.error("AutoSync 构建异常：%r", exc, exc_info=True)
            result = BuildResult(ok=False, message=f"构建异常：{exc!r}", dist_dir=str(self.dist_dir))
        finally:
            self._build_lock.release()
        self.last_result = result
        return result

    def _build_locked(self, refresh_modrinth: bool) -> BuildResult:
        started = time.monotonic()
        cfg = self.config
        dist_dir = self.dist_dir
        result = BuildResult(ok=False, dist_dir=str(dist_dir), manifest_path=str(self.manifest_path))

        if not dist_dir.is_dir():
            dist_dir.mkdir(parents=True, exist_ok=True)
            self.logger.warning("分发目录不存在，已创建空目录：%s", dist_dir)

        # 测速文件必须在扫描之前准备好，这样它会作为普通文件进入清单
        if cfg.speedtest_enabled:
            speedtest = ensure_speedtest_file(
                dist_dir, cfg.speedtest_name, cfg.speedtest_size, logger=self.logger
            )
            result.speedtest = speedtest.to_dict() if speedtest is not None else None
            if speedtest is not None:
                self.logger.info(
                    "AutoSync 测速文件：%s（%d 字节，%s）",
                    speedtest.name,
                    speedtest.size,
                    "本次新生成" if speedtest.written else "已存在，原样复用",
                )

        self.logger.info("AutoSync 开始扫描分发目录：%s", dist_dir)
        files: List[ScannedFile] = []

        def _progress(done: int, total: int, rel: str) -> None:
            if done == total or done % 10 == 0:
                self.logger.info("AutoSync 哈希进度 %d/%d：%s", done, total, rel)

        hash_started = time.monotonic()
        files = scan_dist_dir(dist_dir, cfg.exclude_globs, progress=_progress)
        result.hash_seconds = time.monotonic() - hash_started
        result.file_count = len(files)
        result.total_size = sum(item.size for item in files)
        self.logger.info(
            "AutoSync 扫描完成：%d 个文件，%.1f MB，耗时 %.2fs",
            result.file_count,
            result.total_size / 1024 / 1024,
            result.hash_seconds,
        )

        if result.file_count == 0 and not cfg.allow_empty_dist:
            result.message = (
                f"分发目录 {dist_dir} 中没有任何可分发文件，已中止构建（"
                f"防止生成空清单把客户端 mods 清空；确需如此请把 allow_empty_dist 设为 true）"
            )
            self.logger.error("AutoSync %s", result.message)
            result.elapsed = time.monotonic() - started
            return result

        # 空目录保护要把测速文件排除在外，否则只有 speedtest.bin 时会被误判为"有内容"
        speedtest_rel = cfg.speedtest_name if (cfg.speedtest_enabled and cfg.speedtest_name) else None
        content_count = sum(1 for item in files if item.rel_path != speedtest_rel)
        if content_count == 0 and not cfg.allow_empty_dist:
            result.message = (
                f"分发目录 {dist_dir} 中除测速文件外没有任何可分发文件，已中止构建（"
                f"防止生成空清单把客户端 mods 清空；确需如此请把 allow_empty_dist 设为 true）"
            )
            self.logger.error("AutoSync %s", result.message)
            result.elapsed = time.monotonic() - started
            return result

        # ---------------------------------------------------------- Modrinth
        candidates: List[str] = []
        for item in files:
            if not cfg.wants_modrinth(item.rel_path):
                continue
            cached = self._modrinth_cache.get(item.sha1)
            if refresh_modrinth or cached is None or self._cache_expired(cached):
                candidates.append(item.sha1)
        result.jar_count = sum(1 for item in files if cfg.wants_modrinth(item.rel_path))

        errors: List[str] = []
        if candidates:
            self.logger.info(
                "AutoSync 查询 Modrinth：%d 个待查 hash（缓存命中 %d / 共 %d 个 jar）",
                len(candidates),
                result.jar_count - len(candidates),
                result.jar_count,
            )
            client = ModrinthClient(
                user_agent=cfg.modrinth_user_agent,
                api_base=cfg.modrinth_api_base,
                batch_size=cfg.modrinth_batch_size,
                timeout=cfg.modrinth_timeout_seconds,
                max_retries=cfg.modrinth_max_retries,
                logger=self.logger,
                proxy=getattr(cfg, "http_proxy", ""),
            )
            lookup = client.lookup_sha1(candidates)
            self._modrinth_cache.update(lookup.hits)
            errors.extend(lookup.errors)
            result.modrinth_seconds = lookup.elapsed
            self.logger.info(
                "Modrinth 查询结束：%d 个新命中，%d 批失败，耗时 %.2fs",
                len(lookup.hits),
                lookup.failed_batches,
                lookup.elapsed,
            )
        else:
            self.logger.info("AutoSync 所有 jar 的 Modrinth 结果均来自缓存，跳过网络查询")

        # ---------------------------------------------------------- 组装记录
        records: List[FileRecord] = []
        for item in files:
            hit = self._modrinth_cache.get(item.sha1) if cfg.wants_modrinth(item.rel_path) else None
            self_url = hosted_url(item.rel_path, cfg.hosted_url_prefix, cfg.url_encode_paths)
            urls: List[str] = []
            if hit is not None and hit.url:
                urls = [hit.url, self_url] if cfg.prefer_third_party else [self_url, hit.url]
            else:
                urls = [self_url]
            records.append(
                FileRecord(
                    path=item.rel_path,
                    size=item.size,
                    sha256=item.sha256,
                    sha1=item.sha1,
                    urls=urls,
                    modrinth_url=hit.url if hit else "",
                    modrinth_version=hit.version_number if hit else "",
                )
            )

        result.modrinth_hits = sum(1 for record in records if record.modrinth_url)
        result.self_hosted_only = result.file_count - result.modrinth_hits

        # ---------------------------------------------------------- 版本
        digest = content_digest(records)
        version, generation, changed = next_version(
            digest,
            self.state.get("content_digest"),
            self.state.get("version"),
            int(self.state.get("generation") or 0),
        )
        previous_paths = set(self.previous_files.keys())
        current_paths = {record.path for record in records}
        deletes = sorted(previous_paths - current_paths)

        manifest = build_manifest(records, version=version, deletes=deletes)
        manifest_size = write_manifest_atomic(self.manifest_path, manifest)

        # ---------------------------------------------------------- 落状态
        snapshot = snapshot_dir(dist_dir, cfg.exclude_globs)
        with self._state_lock:
            self.state["files"] = {
                record.path: {
                    "size": record.size,
                    "sha256": record.sha256,
                    "sha1": record.sha1,
                    "urls": record.urls,
                }
                for record in records
            }
            self.state["content_digest"] = digest
            self.state["version"] = version
            self.state["generation"] = generation
            self.state["snapshot_key"] = snapshot_key(snapshot)
            self.state["total_builds"] = int(self.state.get("total_builds") or 0) + 1
            self.state["last_build"] = {
                "at": now_iso(),
                "version": version,
                "file_count": result.file_count,
                "jar_count": result.jar_count,
                # 总大小：status 要展示「87 个文件（277.8 MB）」，重启后也得能读到
                "total_size": result.total_size,
                "hash_seconds": round(result.hash_seconds, 3),
                "modrinth_hits": result.modrinth_hits,
                "self_hosted_only": result.self_hosted_only,
                "manifest_size": manifest_size,
                "deletes": deletes,
                "errors": errors,
                "elapsed": round(time.monotonic() - started, 3),
                "dist_dir": str(dist_dir),
                "speedtest": result.speedtest,
            }
            self.last_snapshot_key = self.state["snapshot_key"]
            self._save_state()

        result.ok = True
        result.changed = changed
        result.version = version
        result.generation = generation
        result.digest = digest
        result.manifest_size = manifest_size
        result.deletes = deletes
        result.errors = errors
        result.elapsed = time.monotonic() - started
        result.message = (
            f"构建完成：{result.file_count} 个文件（{result.jar_count} 个 jar），"
            f"Modrinth 命中 {result.modrinth_hits}（{result.modrinth_hit_rate:.1f}%），"
            f"版本 {version}，清单 {manifest_size / 1024:.1f} KB，耗时 {result.elapsed:.2f}s"
            + ("（内容有变化）" if changed else "（内容未变化，版本号保持）")
        )
        self.logger.info("AutoSync %s", result.message)
        if errors:
            self.logger.warning("AutoSync 本轮有 %d 条错误（未命中的文件降级为服务端托管）", len(errors))

        # 构建完成后自动跑一次依赖检查：只告警，绝不阻断构建
        if cfg.deps_check_after_build:
            self._check_dependencies(result)
        return result

    # ------------------------------------------------------------------ 依赖检查
    def _check_dependencies(self, result: BuildResult) -> None:
        """构建后自动跑一次依赖检查（``deps`` 的同一套逻辑）。

        只读扫描 + 打醒目警告，**不阻断构建**、不改动任何分发文件。
        缺失前置是客户端 ``Missing or unsupported mandatory dependencies`` 崩溃的直接原因，
        所以这里用 warning 级别把缺失项直接摆到服主眼前。
        """
        from .deps import DependencyService  # 局部导入：避免模块级循环依赖

        try:
            service = DependencyService(self.config, self.dist_dir, self.data_dir, logger=self.logger)
            report = service.analyze()
        except Exception as exc:  # noqa: BLE001 - 依赖检查失败绝不能影响构建结果
            self.logger.warning("AutoSync 依赖检查异常（不影响构建结果）：%r", exc)
            return
        result.deps_checked = True
        result.deps_missing = report.missing_count
        result.deps_missing_ids = [item.mod_id for item in report.missing]
        result.deps_report_path = str(service.report_path)
        if report.jij_provided_count:
            self.logger.info(
                "AutoSync 依赖检查：%d 个依赖由父 mod 的 JiJ 嵌套 jar 自带（NeoForge 自动加载），不算缺失",
                report.jij_provided_count,
            )
        if not report.missing:
            self.logger.info("AutoSync 依赖检查通过：未发现真缺失前置（JiJ 已提供 %d 个）", report.jij_provided_count)
            return
        self.logger.warning("=" * 72)
        self.logger.warning(
            "AutoSync [!] 依赖检查发现 %d 个真缺失前置（顶层与所有 JiJ 嵌套层都没有），客户端可能报 "
            "Missing or unsupported mandatory dependencies 直接崩游戏：",
            report.missing_count,
        )
        for item in report.missing[:20]:
            ref = item.server_sources[0] if item.server_sources else None
            if ref is None:
                tail = "；服务端 mods 里也没有，需自行补上"
            elif ref.via_jij:
                tail = f"；由服务端 {ref.jij_parent or ref.rel_path} 的 JiJ 提供（一般无需复制）"
            else:
                tail = f"；可从 {ref.abs_path} 复制"
            self.logger.warning(
                "  - %s（要求 %s）被 %d 个 mod 依赖：%s%s",
                item.mod_id,
                item.version_text,
                len(item.dependent_files),
                ", ".join(item.dependent_files[:5]),
                tail,
            )
        if report.missing_count > 20:
            self.logger.warning("  …（其余 %d 个见报告）", report.missing_count - 20)
        self.logger.warning("AutoSync 详见 deps 或 %s", service.report_path)
        self.logger.warning("=" * 72)

    def _cache_expired(self, hit: ModrinthHit) -> bool:
        hours = self.config.modrinth_cache_hours
        if hours <= 0 or not hit.queried_at:
            return False
        age = time.time() - hit.queried_at
        return age > hours * 3600

    # ------------------------------------------------------------------ 轮询
    def start_watcher(self) -> bool:
        interval = self.config.poll_interval_seconds
        self.stop_watcher()
        if interval <= 0:
            return False
        self._watch_stop = threading.Event()
        self._watch_thread = threading.Thread(
            target=self._watch_loop, args=(interval,), name="AutoSync-Watcher", daemon=True
        )
        self._watch_thread.start()
        self.logger.info("AutoSync 定时轮询已启动：每 %d 秒检查一次分发目录", interval)
        return True

    def stop_watcher(self) -> None:
        self._watch_stop.set()
        thread = self._watch_thread
        self._watch_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def _watch_loop(self, interval: int) -> None:
        while not self._watch_stop.wait(interval):
            try:
                dist_dir = self.dist_dir
                if not dist_dir.is_dir():
                    continue
                key = snapshot_key(snapshot_dir(dist_dir, self.config.exclude_globs))
                if key == self.last_snapshot_key:
                    continue
                self.logger.info("AutoSync 检测到分发目录变化，触发重建")
                self.build()
            except Exception as exc:  # noqa: BLE001
                self.logger.error("AutoSync 轮询异常：%r", exc)

    # ------------------------------------------------------------------ 展示
    def status_dict(self) -> Dict[str, Any]:
        last_build = self.state.get("last_build") if isinstance(self.state.get("last_build"), dict) else {}
        result = self.last_result
        speedtest_path = self.dist_dir / self.config.speedtest_name
        speedtest = {
            "enabled": self.config.speedtest_enabled,
            "name": self.config.speedtest_name,
            "size": self.config.speedtest_size,
            "exists": speedtest_path.is_file(),
            "actual_size": speedtest_path.stat().st_size if speedtest_path.is_file() else 0,
            "dist_path": self.config.speedtest_name,
        }
        return {
            "dist_dir": str(self.dist_dir),
            "manifest_path": str(self.manifest_path),
            "manifest_exists": self.manifest_path.is_file(),
            "version": self.state.get("version") or "",
            "generation": int(self.state.get("generation") or 0),
            "content_digest": self.state.get("content_digest") or "",
            "total_builds": int(self.state.get("total_builds") or 0),
            # 分发文件总字节数：优先用上次构建落盘的记录，其次内存里的结果，最后回退到 state 明细求和
            "total_size": self._total_size(last_build, result),
            "last_build": last_build,
            "cached_modrinth_hits": len(self._modrinth_cache),
            "last_result": result.to_dict() if result is not None else None,
            "tcp": {
                "protocol": PROTOCOL_NAME,
                "enabled": self.config.tcp_enabled,
                "running": self.tcp_running,
                "host": self.config.tcp_host,
                "port": self.tcp_port,
                "endpoint": self.endpoint(),
                "idle_timeout": self.config.tcp_idle_timeout_seconds,
                "path_prefix": self.config.hosted_url_prefix,
                "stats": self._tcp.stats if self._tcp is not None else {},
            },
            "watcher": {
                "interval": self.config.poll_interval_seconds,
                "running": bool(self._watch_thread and self._watch_thread.is_alive()),
            },
            "speedtest": speedtest,
            "prefer_third_party": self.config.prefer_third_party,
        }

    def _total_size(self, last_build: Dict[str, Any], result: Optional[BuildResult]) -> int:
        """分发文件总字节数：落盘记录 -> 内存结果 -> state 明细求和。"""
        for candidate in (last_build.get("total_size"), result.total_size if result is not None else None):
            if isinstance(candidate, int) and candidate >= 0:
                return candidate
        files = self.state.get("files")
        if isinstance(files, dict):
            return sum(
                int(entry.get("size") or 0)
                for entry in files.values()
                if isinstance(entry, dict)
            )
        return 0

    def status_lines(self) -> List[str]:
        """``status`` 的 QBM 风格状态框（**逐行**返回，调用方一行一次输出）。

        颜色是 ``§`` 标记：终端由调用方走 :func:`autosync.theme.to_ansi`，日志走 :func:`autosync.theme.to_console`。
        """
        info = self.status_dict()
        last = info["last_build"] or {}
        tcp = info["tcp"]
        stats = tcp.get("stats") or {}
        speedtest = info.get("speedtest") or {}

        version = info["version"]
        built = bool(version)
        jar_count = int(last.get("jar_count") or 0)
        hits = int(last.get("modrinth_hits") or 0)
        self_hosted = int(last.get("self_hosted_only") or 0)
        hit_rate = (hits / jar_count * 100.0) if jar_count else 0.0
        rate_color = theme.C_OK if hit_rate >= 90 else (theme.C_WARN if hit_rate >= 50 else theme.C_BAD)
        deletes = len(last.get("deletes") or [])
        errors = len(last.get("errors") or [])
        manifest_size = theme.human_size(last.get("manifest_size") or 0)
        total_size = theme.human_size(info.get("total_size") or 0)

        lines = [
            theme.title("AutoSync 状态"),
            theme.marked(theme.MARK_INFO, "程序版本", __version__),
            theme.marked(theme.MARK_OK, "分发目录", info["dist_dir"]),
            theme.marked(
                theme.MARK_OK if built else theme.MARK_BAD,
                "清单版本",
                "{version} §7（第 {generation} 代 · 累计构建 {builds} 次）".format(
                    version=version or "尚未构建",
                    generation=info["generation"],
                    builds=info["total_builds"],
                ),
            ),
            theme.marked(theme.MARK_INFO, "生成时间", last.get("at") or "尚未构建"),
            theme.kv(
                "文件数量",
                "{count} 个 §7/ §f{size} §7（jar §f{jar}§7 个）".format(
                    count=int(last.get("file_count") or 0), size=total_size, jar=jar_count
                ),
            ),
            theme.kv(
                "Modrinth",
                "{color}{hits} 命中{reset} §7/ §f{self_hosted} 自托管 §7（命中率 {color}{rate:.1f}%{reset}§7）".format(
                    color=rate_color,
                    hits=hits,
                    reset=theme.C_RESET,
                    self_hosted=self_hosted,
                    rate=hit_rate,
                ),
            ),
            theme.kv(
                "MSFP",
                "{endpoint} §7· {state} §7· 当前连接 {color}{active}{reset} §7（累计 {count}）".format(
                    endpoint=tcp["endpoint"],
                    state=theme.ok("运行中") if tcp["running"] else theme.bad("未运行"),
                    color=theme.C_OK if stats.get("active_connections") else theme.C_LABEL,
                    active=int(stats.get("active_connections") or 0),
                    reset=theme.C_RESET,
                    count=int(stats.get("connections") or 0),
                ),
            ),
            theme.kv(
                "流量统计",
                "PING §f{pings} §7/ SIZE §f{sizes} §7/ GET §f{gets}§7（分块 {ranged}）§7 · 已发送 §f{bytes}".format(
                    pings=int(stats.get("pings") or 0),
                    sizes=int(stats.get("sizes") or 0),
                    gets=int(stats.get("gets") or 0),
                    ranged=int(stats.get("ranged_gets") or 0),
                    bytes=theme.human_size(stats.get("bytes_sent") or 0),
                ),
            ),
            theme.kv(
                "清单文件",
                "{name} §7（{size} · 待删除 {deletes} 条）".format(
                    name=self.config.manifest_name,
                    size=manifest_size if built else "尚未生成",
                    deletes=deletes,
                ),
            ),
        ]

        if speedtest.get("enabled"):
            healthy = bool(speedtest.get("exists")) and speedtest.get("actual_size") == speedtest.get("size")
            lines.append(
                theme.kv(
                    "测速文件",
                    "{color}{name}{reset} §7（{size} · {state}）".format(
                        color=theme.C_OK if healthy else theme.C_BAD,
                        name=speedtest.get("name", "speedtest.bin"),
                        reset=theme.C_RESET,
                        size=theme.human_size(speedtest.get("actual_size") or speedtest.get("size") or 0),
                        state=theme.ok("正常") if healthy else theme.bad("缺失或大小异常"),
                    ),
                )
            )
        else:
            lines.append(theme.kv("测速文件", theme.hint("已禁用")))

        lines.append(theme.kv("缓存", "Modrinth {} 条".format(info["cached_modrinth_hits"])))
        lines.append(
            theme.kv(
                "自动轮询",
                "{state} §7（每 {interval} 秒）".format(
                    state=theme.ok("运行中") if info["watcher"]["running"] else theme.warn("未运行"),
                    interval=info["watcher"]["interval"],
                ),
            )
        )
        if errors:
            lines.append(
                theme.kv(
                    "上次错误",
                    "{count} 条（详见 {cmd} 与{where}）".format(
                        count=theme.color_number(errors, zero_is_ok=True),
                        cmd=theme.warn("deps"),
                        where=theme.hint("控制台日志"),
                    ),
                )
            )
        lines.append(theme.separator())
        return lines
