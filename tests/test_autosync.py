"""AutoSync 离线测试套件（标准库 unittest，不需要 pytest，不需要 MCDR）。

覆盖：
* MSFP 路径规范化（``..`` / 绝对路径 / 盘符 / 反斜杠 / 前缀 / 百分号解码回退）
* MSFP 服务：PING / SIZE / GET（含 `end=-1`、越界、空文件）/ keep-alive / 并发连接 /
  空闲超时 / 错误码 / 端口占用
* 扫描与 SHA-256/SHA-1 正确性、中文/括号/空格文件名、排除规则
* 清单内容指纹与版本号稳定性、deletes 差分、原子写入
* Modrinth 客户端：批量、重试、429、失败批次隔离、文件选择
* 构建器端到端：urls 顺序、prefer_third_party、测速文件、空目录保护
* 独立 shell：启动即拉起 MSFP、命令分发（不带前缀）、配置自动生成与 reload
* theme 的 ``§`` -> ANSI 转换与自动去色

运行：``python tests/run_tests.py``
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
#: 独立 Python 版服务端的包目录（<仓库根>/server/python/autosync）
PLUGIN_DIR = HERE.parent / "server" / "python"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from autosync.builder import AutoSyncCore  # noqa: E402
from autosync.classify import (  # noqa: E402
    CATEGORY_BOTH,
    CATEGORY_CLIENT_ONLY,
    CATEGORY_SERVER_ONLY,
    CATEGORY_UNKNOWN,
    ClassifyService,
    judge_toml,
)
from autosync.config import AutoSyncConfig, ensure_config_file, load_config_file  # noqa: E402
from autosync.deps import (  # noqa: E402
    JIJ_MAX_DEPTH,
    DependencyService,
    build_dependency_graph,
    compare_versions,
    parse_mod_toml,
    read_mod_toml,
    scan_jar,
    version_in_range,
)
from autosync.deps_fix import (  # noqa: E402
    STATUS_MANUAL,
    STATUS_PLANNED,
    DepsFixService,
    FixCandidate,
    FixPlan,
    FixPlanItem,
)
from autosync.manifest import (  # noqa: E402
    FileRecord,
    build_manifest,
    content_digest,
    hosted_url,
    load_manifest,
    next_version,
    write_manifest_atomic,
)
from autosync.modrinth import ModrinthClient, ModrinthHit, is_unsupported  # noqa: E402
from autosync.scanner import (  # noqa: E402
    hash_file,
    is_excluded,
    scan_dist_dir,
    snapshot_dir,
    snapshot_key,
)
from autosync.shell import AutoSyncShell, resolve_base_dir  # noqa: E402
from autosync.speedtest import ensure_speedtest_file, speedtest_bytes  # noqa: E402
from autosync.tcp_client import MsfpClient, MsfpError, download_multithreaded, sha256_file  # noqa: E402
from autosync.tcp_server import (  # noqa: E402
    RequestError,
    AutoSyncTCPService,
    resolve_candidates,
)

LOGGER = logging.getLogger("autosync.tests")
if not LOGGER.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("      | %(levelname)s %(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)

CN_JAR = "mods/[机械动力：航空学] create-aeronautics (1).jar"
CN_ONLY_JAR = "mods/机械动力-纯中文名.jar"
BRACKET_ONLY_JAR = "mods/[PureBrackets]-1.0.jar"
CN_SUBDIR_FILE = "config/设置 (v2) [1].json"


# --------------------------------------------------------------------------- 工具
def write_file(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def make_dist(root: Path) -> bytes:
    """造一个含中文 / 方括号 / 空格 / 纯中文 / 纯方括号文件名的分发目录，返回主 jar 内容。"""
    payload = bytes(range(256)) * 20  # 5120 bytes，便于校验分片内容
    write_file(root / CN_JAR, payload)
    write_file(root / "mods/plain-mod.jar", b"plain" * 100)
    write_file(root / CN_ONLY_JAR, b"\x00pure-chinese-name\xff" * 64)
    write_file(root / BRACKET_ONLY_JAR, b"\x01pure-bracket-name\xfe" * 64)
    write_file(root / CN_SUBDIR_FILE, b'{"hello": "\xe4\xb8\xad\xe6\x96\x87"}')
    write_file(root / "mods/empty.marker", b"")
    return payload


def raw_request(host: str, port: int, payload: bytes, expect_bytes: int = 65536) -> bytes:
    """裸 socket 发原始字节并读一次响应（协议级测试用）。"""
    with socket.create_connection((host, port), timeout=10) as sock:
        sock.sendall(payload)
        return sock.recv(expect_bytes)


# --------------------------------------------------------------------------- 1. 路径规范化
class TestProtocolPathResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        make_dist(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def resolved(self, raw, prefix=""):
        return resolve_candidates(self.root, raw, prefix)[0]

    def test_normal_paths(self):
        self.assertEqual(self.resolved("mods/plain-mod.jar"), self.root / "mods" / "plain-mod.jar")
        self.assertEqual(self.resolved("./mods/plain-mod.jar"), self.root / "mods" / "plain-mod.jar")
        self.assertEqual(self.resolved("mods//plain-mod.jar"), self.root / "mods" / "plain-mod.jar")

    def test_unicode_bracket_space_names(self):
        self.assertEqual(self.resolved(CN_JAR), self.root / CN_JAR)
        self.assertEqual(self.resolved(CN_SUBDIR_FILE), self.root / CN_SUBDIR_FILE)
        self.assertEqual(self.resolved(CN_ONLY_JAR), self.root / CN_ONLY_JAR)
        self.assertEqual(self.resolved(BRACKET_ONLY_JAR), self.root / BRACKET_ONLY_JAR)
        self.assertTrue(self.resolved(CN_JAR).is_file())

    def test_prefix_stripping(self):
        self.assertEqual(
            resolve_candidates(self.root, "files/mods/plain-mod.jar", "files")[0],
            self.root / "mods" / "plain-mod.jar",
        )
        # 没有配置前缀时 files/ 就是普通目录
        self.assertEqual(
            resolve_candidates(self.root, "files/mods/plain-mod.jar", "")[0],
            self.root / "files" / "mods" / "plain-mod.jar",
        )

    def test_escape_rejected(self):
        for raw in (
            "..",
            "../secret.txt",
            "mods/../../secret.txt",
            "/etc/passwd",
            "C:/Windows/win.ini",
            "c:secret",
            "mods\\..\\..\\secret.txt",
            "\\\\server\\share\\x",
            "",
            ".",
            "mods/./../x",
        ):
            with self.assertRaises(RequestError, msg=raw) as ctx:
                resolve_candidates(self.root, raw, "")
            self.assertIn(ctx.exception.message, ("forbidden", "bad request"), raw)

    def test_nul_and_colon(self):
        with self.assertRaises(RequestError):
            resolve_candidates(self.root, "mods/a\x00b.jar", "")
        with self.assertRaises(RequestError):
            resolve_candidates(self.root, "mods/a:b.jar", "")

    def test_percent_decoding_fallback(self):
        candidates = resolve_candidates(self.root, urllib.parse.quote(CN_JAR, safe="/"), "")
        self.assertEqual(len(candidates), 2)
        self.assertFalse(candidates[0].is_file(), "字面量路径不存在")
        self.assertTrue(candidates[1].is_file(), "解码后的路径应命中")

    def test_symlink_escape_rejected(self):
        link = self.root / "link"
        try:
            os.symlink(tempfile.gettempdir(), link, target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("当前环境不允许创建符号链接")
        with self.assertRaises(RequestError):
            resolve_candidates(self.root, "link/anything.txt", "")


# --------------------------------------------------------------------------- 2. Scanner
class TestScanner(unittest.TestCase):
    def test_hash_matches_hashlib(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = os.urandom(4096) * 3
            target = write_file(Path(tmp) / "a.bin", data)
            sha256, sha1 = hash_file(target)
            self.assertEqual(sha256, hashlib.sha256(data).hexdigest())
            self.assertEqual(sha1, hashlib.sha1(data).hexdigest())

    def test_scan_unicode_and_excludes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_dist(root)
            write_file(root / "manifest.json", b"{}")
            write_file(root / "junk.tmp", b"x")
            entries = scan_dist_dir(root, ["manifest.json", "*.tmp"])
            paths = [e.rel_path for e in entries]
            self.assertIn(CN_JAR, paths)
            self.assertIn(CN_SUBDIR_FILE, paths)
            self.assertIn("mods/plain-mod.jar", paths)
            self.assertNotIn("manifest.json", paths)
            self.assertNotIn("junk.tmp", paths)
            self.assertTrue(all("\\" not in p for p in paths), "相对路径必须使用正斜杠")
            jar = next(e for e in entries if e.rel_path == CN_JAR)
            self.assertEqual(jar.sha256, hashlib.sha256((root / CN_JAR).read_bytes()).hexdigest())
            self.assertEqual(jar.size, 5120)

    def test_exclude_glob(self):
        self.assertTrue(is_excluded("mods/a.tmp", ["*.tmp"]))
        self.assertTrue(is_excluded("manifest.json", ["manifest.json"]))
        self.assertFalse(is_excluded("mods/a.jar", ["*.tmp"]))

    def test_snapshot_detects_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_file(root / "a.jar", b"a")
            key1 = snapshot_key(snapshot_dir(root, []))
            write_file(root / "b.jar", b"b")
            key2 = snapshot_key(snapshot_dir(root, []))
            self.assertNotEqual(key1, key2)
            self.assertEqual(key2, snapshot_key(snapshot_dir(root, [])))


# --------------------------------------------------------------------------- 3. Manifest
class TestManifestVersion(unittest.TestCase):
    @staticmethod
    def records():
        return [
            FileRecord(path="mods/a.jar", size=1, sha256="aa", urls=["mods/a.jar"]),
            FileRecord(path="mods/b.jar", size=2, sha256="bb", urls=["mods/b.jar", "https://cdn/x"]),
        ]

    def test_digest_stable_and_order_independent(self):
        first = content_digest(self.records())
        second = content_digest(list(reversed(self.records())))
        self.assertEqual(first, second)

    def test_version_unchanged_when_content_unchanged(self):
        digest = content_digest(self.records())
        version, generation, changed = next_version(digest, digest, "v1-abc-3", 3)
        self.assertEqual(version, "v1-abc-3")
        self.assertEqual(generation, 3)
        self.assertFalse(changed)

    def test_version_changes_when_content_changes(self):
        digest = content_digest(self.records())
        version, generation, changed = next_version(digest, "other", "v1-abc-3", 3)
        self.assertNotEqual(version, "v1-abc-3")
        self.assertEqual(generation, 4)
        self.assertTrue(changed)
        self.assertTrue(version.startswith("v1-" + digest[:12]))

    def test_digest_changes_on_url_change(self):
        records = self.records()
        before = content_digest(records)
        records[0].urls.append("https://cdn.example/a.jar")
        self.assertFalse(before == content_digest(records))

    def test_write_manifest_is_utf8_and_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            manifest = build_manifest(
                [FileRecord(CN_JAR, 5, "aa", urls=["mods/x"])], "v1-x-1", deletes=["mods/gone.jar"]
            )
            size = write_manifest_atomic(path, manifest)
            self.assertEqual(size, path.stat().st_size)
            raw = path.read_bytes()
            self.assertIn("机械动力".encode("utf-8"), raw, "中文路径不能被转义成 \\uXXXX")
            self.assertFalse(list(path.parent.glob(".manifest-*")), "临时文件要清理掉")
            loaded = load_manifest(path)
            self.assertEqual(loaded["format"], 1)
            self.assertEqual(loaded["files"][0]["path"], CN_JAR)
            self.assertEqual(loaded["deletes"], ["mods/gone.jar"])
            self.assertEqual(loaded["version"], "v1-x-1")

    def test_hosted_url_forms(self):
        # MSFP 默认：相对分发目录根的原始路径（客户端直接当 GET 的 path 用）
        self.assertEqual(hosted_url("mods/a.jar", ""), "mods/a.jar")
        self.assertEqual(hosted_url("mods/[中文] a (1).jar", "", False), "mods/[中文] a (1).jar")
        # 可选 HTTP 风格：前缀 + 百分号编码
        self.assertEqual(hosted_url("mods/a.jar", "files"), "files/mods/a.jar")
        self.assertEqual(
            hosted_url("mods/[中文] a (1).jar", "files", True),
            "files/mods/%5B%E4%B8%AD%E6%96%87%5D%20a%20%281%29.jar",
        )


# --------------------------------------------------------------------------- 4. MSFP 服务
class TestTcpServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "client-dist"
        cls.payload = make_dist(cls.root)
        ensure_speedtest_file(cls.root, "speedtest.bin", 512 * 1024)
        write_manifest_atomic(
            cls.root / "manifest.json",
            build_manifest(
                [FileRecord(CN_JAR, len(cls.payload), hashlib.sha256(cls.payload).hexdigest(), urls=[CN_JAR])],
                "v1-test-1",
            ),
        )
        cls.service = AutoSyncTCPService(cls.root, host="127.0.0.1", port=0, logger=LOGGER)
        assert cls.service.start(), cls.service.last_error
        cls.host, cls.port = "127.0.0.1", cls.service.port

    @classmethod
    def tearDownClass(cls):
        cls.service.stop()
        cls.tmp.cleanup()

    def client(self, timeout=20.0):
        return MsfpClient(self.host, self.port, timeout=timeout)

    # ---------------------------------------------------------------- 基础
    def test_ping(self):
        with self.client() as client:
            self.assertGreaterEqual(client.ping(), 0.0)

    def test_size(self):
        with self.client() as client:
            self.assertEqual(client.size(CN_JAR), len(self.payload))
            self.assertEqual(client.size("speedtest.bin"), 512 * 1024)
            self.assertEqual(client.size("mods/empty.marker"), 0)

    def test_get_full_file(self):
        with self.client() as client:
            self.assertEqual(client.get(CN_JAR), self.payload)
            self.assertEqual(client.get(CN_JAR, 0, -1), self.payload)

    def test_get_ranges(self):
        size = len(self.payload)
        with self.client() as client:
            self.assertEqual(client.get(CN_JAR, 0, 1023), self.payload[:1024])
            self.assertEqual(client.get(CN_JAR, 1024, 2047), self.payload[1024:2048])
            self.assertEqual(client.get(CN_JAR, 4096, -1), self.payload[4096:])
            self.assertEqual(client.get(CN_JAR, 100, 100), self.payload[100:101])
            # end 超过文件末尾 -> 按实际长度返回
            self.assertEqual(client.get(CN_JAR, size - 10, size + 9999), self.payload[-10:])
            # start 超出末尾 -> OK 0
            self.assertEqual(client.get(CN_JAR, size + 100, -1), b"")
            self.assertEqual(client.get("mods/empty.marker", 0, -1), b"")

    def test_unicode_and_bracket_path_over_wire(self):
        """三类文件名各自单独过一遍：纯中文名、纯方括号名、中文+方括号+空格名。"""
        with self.client() as client:
            for rel in (CN_ONLY_JAR, BRACKET_ONLY_JAR, CN_JAR, CN_SUBDIR_FILE):
                local = (self.root / rel).read_bytes()
                self.assertEqual(client.size(rel), len(local), rel)
                self.assertEqual(client.get(rel), local, rel)
                if len(local) >= 64:
                    self.assertEqual(client.get(rel, 16, 47), local[16:48], rel)
            self.assertEqual(client.size(CN_JAR), len(self.payload))
            self.assertEqual(client.get(CN_JAR, 0, 255), self.payload[:256])
            self.assertTrue(client.get(CN_SUBDIR_FILE).startswith(b'{"hello"'))
            # 服务端也接受百分号编码（兼容 HTTP 风格客户端）
            self.assertEqual(client.size(urllib.parse.quote(CN_JAR, safe="/")), len(self.payload))

    def test_keep_alive_many_requests(self):
        with self.client() as client:
            for index in range(10):
                self.assertEqual(int(client._request("PING").strip()), 0)
                self.assertEqual(client.size("manifest.json") > 0, True)
                self.assertEqual(client.get(CN_JAR, index * 10, index * 10 + 9), self.payload[index * 10 : index * 10 + 10])

    def test_crlf_tolerated(self):
        response = raw_request(self.host, self.port, b"PING\r\n")
        self.assertEqual(response, b"OK 0\n")

    def test_concurrent_connections(self):
        """客户端每块一个连接：8 并发连接分块下载 512KB 测速文件并校验 sha256。"""
        size = (self.root / "speedtest.bin").stat().st_size
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out.bin"
            before = self.service.stats.get("connections", 0)
            report = download_multithreaded(self.host, self.port, "speedtest.bin", dest, size, threads=8)
            self.assertEqual(sha256_file(dest), sha256_file(self.root / "speedtest.bin"))
            self.assertGreaterEqual(report.chunks, 8)
            self.assertGreaterEqual(self.service.stats.get("connections", 0) - before, 8)

    def test_error_messages(self):
        with self.client() as client:
            with self.assertRaises(MsfpError) as ctx:
                client.size("mods/not-here.jar")
            self.assertEqual(str(ctx.exception), "not found")
            for bad in ("../secret.txt", "/etc/passwd", "C:/Windows/win.ini", "mods/../../secret"):
                with self.assertRaises(MsfpError, msg=bad) as ctx:
                    client.size(bad)
                self.assertEqual(str(ctx.exception), "forbidden", bad)

    def test_bad_requests(self):
        cases = [
            (b"GET 0\n", b"ERR bad request\n"),
            (b"GET 0 -1\n", b"ERR bad request\n"),
            (b"GET x -1 mods/a.jar\n", b"ERR bad request\n"),
            (b"GET 0 y mods/a.jar\n", b"ERR bad request\n"),
            (b"GET -5 -1 mods/a.jar\n", b"ERR bad request\n"),
            (b"GET 100 50 mods/plain-mod.jar\n", b"ERR bad request\n"),
            (b"SIZE\n", b"ERR bad request\n"),
            (b"PING extra\n", b"ERR bad request\n"),
        ]
        for request, expected in cases:
            self.assertEqual(raw_request(self.host, self.port, request), expected, request)

    def test_unknown_verb_closes_connection(self):
        with socket.create_connection((self.host, self.port), timeout=10) as sock:
            sock.sendall(b"HELLO world\n")
            self.assertEqual(sock.recv(1024), b"ERR bad request\n")
            self.assertEqual(sock.recv(1024), b"", "未知动词后服务端应关闭连接")

    def test_idle_timeout_closes_connection(self):
        service = AutoSyncTCPService(self.root, host="127.0.0.1", port=0, logger=LOGGER, idle_timeout=1)
        assert service.start(), service.last_error
        try:
            sock = socket.create_connection(("127.0.0.1", service.port), timeout=10)
            self.assertEqual(sock.recv(64), b"")  # 1 秒后服务端主动断开
            self.assertGreaterEqual(service.stats.get("idle_timeouts", 0), 1)
            sock.close()
        finally:
            service.stop()

    def test_port_conflict_detected(self):
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        try:
            service = AutoSyncTCPService(self.root, host="127.0.0.1", port=port, logger=LOGGER)
            self.assertFalse(service.start())
            self.assertFalse(service.running)
            self.assertIn("占用", service.last_error)
        finally:
            busy.close()

    def test_stats(self):
        stats = self.service.stats
        self.assertGreater(stats.get("connections", 0), 0)
        self.assertGreater(stats.get("pings", 0), 0)
        self.assertGreater(stats.get("gets", 0), 0)
        self.assertGreater(stats.get("bytes_sent", 0), 0)


# --------------------------------------------------------------------------- 5. speedtest.bin
class TestSpeedtestFile(unittest.TestCase):
    def test_speedtest_bytes_deterministic(self):
        first = speedtest_bytes(65536)
        second = speedtest_bytes(65536)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 65536)
        self.assertNotEqual(first[:32], first[32:64], "内容不应是简单重复块")
        self.assertEqual(len(speedtest_bytes(33)), 33)
        self.assertEqual(len(speedtest_bytes(0)), 0)

    def test_ensure_reuses_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = ensure_speedtest_file(root, "speedtest.bin", 32768)
            self.assertTrue(first.written)
            target = root / "speedtest.bin"
            self.assertEqual(target.stat().st_size, 32768)
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), first.sha256)
            mtime = target.stat().st_mtime_ns
            time.sleep(0.01)
            second = ensure_speedtest_file(root, "speedtest.bin", 32768)
            self.assertFalse(second.written, "大小正确时必须复用，不能重写")
            self.assertEqual(target.stat().st_mtime_ns, mtime, "复用不应改动 mtime")
            self.assertEqual(target.read_bytes(), speedtest_bytes(32768))

    def test_ensure_regenerates_on_wrong_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_file(root / "speedtest.bin", b"broken")
            info = ensure_speedtest_file(root, "speedtest.bin", 4096)
            self.assertTrue(info.written)
            self.assertEqual((root / "speedtest.bin").stat().st_size, 4096)

    def test_disabled_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(ensure_speedtest_file(Path(tmp), "speedtest.bin", 0))
            self.assertIsNone(ensure_speedtest_file(Path(tmp), "", 1024))


# --------------------------------------------------------------------------- 6. Modrinth
class FakeModrinthHandler(BaseHTTPRequestHandler):
    #: dict（键为请求序号 str 或 "default"）或 callable(body, index) -> plan
    behavior = {}
    requests = []

    def log_message(self, *args):  # 静音
        pass

    @classmethod
    def _plan(cls, body, index):
        behavior = cls.behavior
        if callable(behavior):
            plan = behavior(body, index)
        else:
            plan = behavior.get(str(index)) or behavior.get("default") or {}
        return plan or {}

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        FakeModrinthHandler.requests.append(
            {"path": self.path, "body": body, "user_agent": self.headers.get("User-Agent")}
        )
        index = len(FakeModrinthHandler.requests)
        plan = self._plan(body, index)
        if plan.get("status") == 429:
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if plan.get("status", 200) != 200:
            self.send_response(plan["status"])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if plan.get("garbage"):
            data = b"{not json"
        else:
            payload = {}
            for sha1 in body.get("hashes", []):
                payload[sha1] = {
                    "id": "ver-" + sha1[:4],
                    "project_id": "proj-" + sha1[:4],
                    "version_number": "1.0." + sha1[:2],
                    "files": [
                        {"url": f"https://cdn.example/{sha1[:4]}/sources.jar", "filename": "sources.jar", "primary": False},
                        {
                            "url": f"https://cdn.example/{sha1[:4]}/{sha1[:4]}.jar",
                            "filename": f"{sha1[:4]}.jar",
                            "primary": True,
                            "hashes": {"sha1": sha1},
                        },
                    ],
                }
            data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class TestModrinthClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeModrinthHandler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}/v2"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeModrinthHandler.behavior = {}
        FakeModrinthHandler.requests = []

    def client(self, **kwargs):
        return ModrinthClient(
            user_agent="CSMC-AutoSync/1.0 (test)",
            api_base=self.base,
            batch_size=kwargs.pop("batch_size", 100),
            timeout=5,
            max_retries=kwargs.pop("max_retries", 2),
            logger=LOGGER,
            sleeper=lambda _s: None,
            **kwargs,
        )

    @staticmethod
    def sha1s(n):
        return [hashlib.sha1(f"seed-{i}".encode()).hexdigest() for i in range(n)]

    def test_batching_and_user_agent(self):
        hashes = self.sha1s(5)
        result = self.client(batch_size=2).lookup_sha1(hashes)
        self.assertEqual(len(result.hits), 5)
        self.assertEqual(result.batches, 3)
        self.assertEqual(result.failed_batches, 0)
        self.assertEqual(result.errors, [])
        for request in FakeModrinthHandler.requests:
            self.assertEqual(request["user_agent"], "CSMC-AutoSync/1.0 (test)")
            self.assertEqual(request["body"]["algorithm"], "sha1")
            self.assertLessEqual(len(request["body"]["hashes"]), 2)
        hit = result.hits[hashes[0]]
        # 必须挑出 sha1 与查询一致的那个文件，而不是 sources.jar
        self.assertEqual(hit.url, f"https://cdn.example/{hashes[0][:4]}/{hashes[0][:4]}.jar")
        self.assertEqual(hit.version_number, "1.0." + hashes[0][:2])

    def test_retry_on_500(self):
        """前两次 500，第三次成功。"""
        FakeModrinthHandler.behavior = lambda body, index: {"status": 500} if index <= 2 else {}
        result = self.client(max_retries=3).lookup_sha1(self.sha1s(2))
        self.assertEqual(len(result.hits), 2)
        self.assertEqual(len(FakeModrinthHandler.requests), 3)

    def test_retry_on_429(self):
        FakeModrinthHandler.behavior = lambda body, index: {"status": 429} if index == 1 else {}
        result = self.client().lookup_sha1(self.sha1s(1))
        self.assertEqual(len(result.hits), 1)
        self.assertGreaterEqual(len(FakeModrinthHandler.requests), 2)

    def test_batch_failure_isolated(self):
        """第一批永久 500，第二批仍要成功，且错误只记录不抛出。"""
        FakeModrinthHandler.behavior = lambda body, index: {"status": 500} if index == 1 else {}
        result = self.client(batch_size=2, max_retries=0).lookup_sha1(self.sha1s(4))
        self.assertEqual(result.failed_batches, 1)
        self.assertEqual(len(result.hits), 2)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("查询失败", result.errors[0])

    def test_garbage_json_isolated(self):
        FakeModrinthHandler.behavior = {"default": {"garbage": True}}
        result = self.client(max_retries=0).lookup_sha1(self.sha1s(2))
        self.assertEqual(result.hits, {})
        self.assertEqual(result.failed_batches, 1)

    def test_network_error_isolated(self):
        client = ModrinthClient(
            user_agent="x",
            api_base="http://127.0.0.1:1/v2",  # 必然连不上
            batch_size=100,
            timeout=1,
            max_retries=1,
            logger=LOGGER,
            sleeper=lambda _s: None,
        )
        result = client.lookup_sha1(self.sha1s(3))
        self.assertEqual(result.hits, {})
        self.assertEqual(result.failed_batches, 1)
        self.assertTrue(result.errors)

    def test_invalid_hash_filtered(self):
        result = self.client().lookup_sha1(["not-a-hash", "", self.sha1s(1)[0]])
        self.assertEqual(result.requested, 1)


# --------------------------------------------------------------------------- 7. Builder
class TestBuilder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dist = self.root / "client-dist"
        self.data = self.root / "data"
        self.payload = make_dist(self.dist)
        self.config = AutoSyncConfig.from_dict(
            {
                "dist_dir": "client-dist",
                "tcp_port": 0,
                "tcp_enabled": False,
                "speedtest_enabled": False,  # 单独用 test_speedtest_* 覆盖
                "modrinth_extensions": [".jar"],
                "modrinth_api_base": "http://127.0.0.1:1/v2",
                "modrinth_max_retries": 0,
                "modrinth_timeout_seconds": 1,
            }
        )
        self.core = AutoSyncCore(self.config, self.data, self.root, logger=LOGGER)

    def tearDown(self):
        self.core.stop_tcp()
        self.core.stop_watcher()
        self.tmp.cleanup()

    def seed_modrinth_hit(self):
        sha1 = hashlib.sha1((self.dist / CN_JAR).read_bytes()).hexdigest()
        self.core._modrinth_cache[sha1] = ModrinthHit(
            sha1=sha1,
            url="https://cdn.modrinth.com/data/ABC/versions/xyz/%E4%B8%AD%E6%96%87.jar",
            filename="mod.jar",
            version_number="1.2.3",
            project_id="ABC",
            version_id="xyz",
            queried_at=time.time(),
        )
        return sha1

    def test_build_basic_and_url_order(self):
        self.seed_modrinth_hit()
        result = self.core.build()
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.file_count, 6)
        self.assertEqual(result.jar_count, 4)
        self.assertEqual(result.modrinth_hits, 1)
        self.assertEqual(result.self_hosted_only, 5)
        self.assertEqual(result.deletes, [])
        manifest = load_manifest(self.dist / "manifest.json")
        entry = next(e for e in manifest["files"] if e["path"] == CN_JAR)
        self.assertEqual(len(entry["urls"]), 2)
        # MSFP：自托管地址就是分发目录相对路径，客户端直接当 GET 的 path 用
        self.assertEqual(entry["urls"][0], CN_JAR)
        self.assertTrue(entry["urls"][1].startswith("https://cdn.modrinth.com/"))
        self.assertEqual(entry["size"], len(self.payload))
        self.assertEqual(entry["sha256"], hashlib.sha256(self.payload).hexdigest())
        other = next(e for e in manifest["files"] if e["path"].endswith("plain-mod.jar"))
        self.assertEqual(other["urls"], ["mods/plain-mod.jar"])
        self.assertNotIn("manifest.json", [e["path"] for e in manifest["files"]])

    def test_version_stable_on_rebuild(self):
        self.seed_modrinth_hit()
        first = self.core.build()
        size_before = (self.dist / "manifest.json").stat().st_size
        second = self.core.build()
        self.assertTrue(second.ok)
        self.assertFalse(second.changed)
        self.assertEqual(first.version, second.version)
        self.assertEqual(size_before, (self.dist / "manifest.json").stat().st_size)

    def test_version_changes_on_modify_add_remove(self):
        self.seed_modrinth_hit()
        first = self.core.build()
        version = first.version

        write_file(self.dist / "mods/plain-mod.jar", b"changed" * 50)
        second = self.core.build()
        self.assertTrue(second.changed)
        self.assertNotEqual(version, second.version)

        write_file(self.dist / "mods/extra.jar", b"extra")
        third = self.core.build()
        self.assertNotEqual(second.version, third.version)
        self.assertEqual(third.deletes, [])

        os.remove(self.dist / "mods/extra.jar")
        fourth = self.core.build()
        self.assertNotEqual(third.version, fourth.version)
        self.assertEqual(fourth.deletes, ["mods/extra.jar"])
        manifest = load_manifest(self.dist / "manifest.json")
        self.assertEqual(manifest["deletes"], ["mods/extra.jar"])

    def test_prefer_third_party(self):
        self.seed_modrinth_hit()
        self.core.config.prefer_third_party = True
        result = self.core.build()
        self.assertTrue(result.ok)
        manifest = load_manifest(self.dist / "manifest.json")
        entry = next(e for e in manifest["files"] if e["path"] == CN_JAR)
        self.assertTrue(entry["urls"][0].startswith("https://cdn.modrinth.com/"))
        self.assertEqual(entry["urls"][1], CN_JAR)

    def test_http_style_prefix_and_encoding_option(self):
        """可选的 HTTP 风格（前缀 + percent 编码）仍然能生成，虽然 MSFP 不用。"""
        self.core.config.hosted_url_prefix = "files"
        self.core.config.url_encode_paths = True
        self.core.build()
        manifest = load_manifest(self.dist / "manifest.json")
        entry = next(e for e in manifest["files"] if e["path"] == CN_JAR)
        self.assertTrue(entry["urls"][0].startswith("files/mods/%5B"))

    def test_modrinth_failure_degrades_to_self_hosted(self):
        """网络不可达时构建仍然成功，所有文件降级为自托管。"""
        result = self.core.build()
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.modrinth_hits, 0)
        self.assertTrue(result.errors)
        manifest = load_manifest(self.dist / "manifest.json")
        self.assertTrue(all(len(e["urls"]) == 1 for e in manifest["files"]))

    def test_empty_dist_guard(self):
        empty = self.root / "empty-dist"
        empty.mkdir()
        self.core.config.dist_dir = "empty-dist"
        result = self.core.build()
        self.assertFalse(result.ok)
        self.assertIn("没有任何可分发文件", result.message)
        self.assertFalse((empty / "manifest.json").exists())

    def test_empty_dist_guard_ignores_speedtest_file(self):
        """只有 speedtest.bin 时也必须判定为空，不能生成"全部删除"的清单。"""
        empty = self.root / "empty-dist-2"
        empty.mkdir()
        self.core.config.dist_dir = "empty-dist-2"
        self.core.config.speedtest_enabled = True
        result = self.core.build()
        self.assertFalse(result.ok)
        self.assertIn("除测速文件外没有任何可分发文件", result.message)
        self.assertFalse((empty / "manifest.json").exists())
        self.assertTrue((empty / "speedtest.bin").is_file())

    def test_speedtest_file_in_manifest_and_stable(self):
        self.core.config.speedtest_enabled = True
        self.core.config.speedtest_size = 4096
        self.seed_modrinth_hit()
        first = self.core.build()
        self.assertTrue(first.ok, first.message)
        self.assertEqual(first.file_count, 7, "6 个普通文件 + speedtest.bin")
        self.assertIsNotNone(first.speedtest)
        self.assertTrue(first.speedtest["written"])
        manifest = load_manifest(self.dist / "manifest.json")
        entry = next(e for e in manifest["files"] if e["path"] == "speedtest.bin")
        self.assertEqual(entry["size"], 4096)
        self.assertEqual(entry["urls"], ["speedtest.bin"])
        self.assertEqual(entry["sha256"], hashlib.sha256(speedtest_bytes(4096)).hexdigest())

        second = self.core.build()
        self.assertFalse(second.changed)
        self.assertEqual(first.version, second.version)
        self.assertFalse(second.speedtest["written"], "第二次构建必须复用已有测速文件")

        joined = "\n".join(self.core.status_lines())
        self.assertIn("测速文件", joined)
        self.assertIn("speedtest.bin", joined)
        self.assertIn("speedtest", json.dumps(self.core.status_dict(), ensure_ascii=False))

    def test_speedtest_disabled(self):
        self.core.config.speedtest_enabled = False
        result = self.core.build()
        self.assertTrue(result.ok)
        self.assertIsNone(result.speedtest)
        self.assertFalse((self.dist / "speedtest.bin").exists())

    def test_cache_persists_across_instances(self):
        self.seed_modrinth_hit()
        self.core.build()
        fresh = AutoSyncCore(self.config, self.data, self.root, logger=LOGGER)
        self.assertEqual(len(fresh._modrinth_cache), 1)
        self.assertEqual(fresh.state.get("version"), self.core.state.get("version"))

    def test_concurrent_build_is_rejected(self):
        self.seed_modrinth_hit()
        self.assertTrue(self.core._build_lock.acquire(blocking=False))
        try:
            result = self.core.build()
            self.assertFalse(result.ok)
            self.assertTrue(result.busy)
        finally:
            self.core._build_lock.release()

    def test_build_runs_dependency_check_and_reports_missing(self):
        """build 完成后自动跑依赖检查：只告警、不阻断构建。"""
        self.config.deps_check_after_build = True
        make_jar(
            self.dist / "mods/consumer.jar",
            b"consumer" * 20,
            toml=mod_toml("consumer", "1.0.0", [("sable", "required", "[2.0,)")]),
        )
        result = self.core.build()
        self.assertTrue(result.ok, result.message)
        self.assertTrue(result.deps_checked)
        self.assertEqual(result.deps_missing, 1)
        self.assertEqual(result.deps_missing_ids, ["sable"])
        self.assertTrue(Path(result.deps_report_path).is_file(), result.deps_report_path)
        self.assertEqual(result.to_dict()["deps_missing"], 1)

    def test_build_dependency_check_ignores_jij_provided(self):
        """build 后的自动检查只对**真缺失**报警：JiJ 自带的前置必须被排除。"""
        self.config.deps_check_after_build = True
        make_jij_jar(
            self.dist / "mods/superb.jar",
            mod_toml("superb", "1.0.0", [("sable", "required", "[2.0,)")]),
            nested={"sable.jar": mod_toml("sable", "2.0.5")},
        )
        result = self.core.build()
        self.assertTrue(result.ok, result.message)
        self.assertTrue(result.deps_checked)
        self.assertEqual(result.deps_missing, 0, "JiJ 自带的 sable 不该被算成缺失")
        self.assertEqual(result.deps_missing_ids, [])
        payload = json.loads(Path(result.deps_report_path).read_text("utf-8"))
        self.assertEqual(payload["missing_count"], 0)
        self.assertEqual(payload["jij_provided_count"], 1)

    def test_dependency_check_can_be_disabled(self):
        make_jar(
            self.dist / "mods/consumer.jar",
            b"consumer" * 20,
            toml=mod_toml("consumer", "1.0.0", [("sable", "required", "[2.0,)")]),
        )
        self.config.deps_check_after_build = False
        result = self.core.build()
        self.assertTrue(result.ok, result.message)
        self.assertFalse(result.deps_checked)

    def test_tcp_service_serves_built_manifest(self):
        self.core.config.tcp_enabled = True
        self.core.config.tcp_port = 0
        result = self.core.build()
        self.assertTrue(result.ok)
        self.assertTrue(self.core.start_tcp())
        try:
            with MsfpClient("127.0.0.1", self.core.tcp_port) as client:
                raw = client.get("manifest.json")
                self.assertEqual(json.loads(raw.decode("utf-8"))["version"], result.version)
                self.assertEqual(client.size(CN_JAR), len(self.payload))
        finally:
            self.core.stop_tcp()
        self.assertFalse(self.core.tcp_running)


# --------------------------------------------------------------------------- 8. 独立 shell
class TestShell(unittest.TestCase):
    """shell 取代了 MCDR 入口：启动即拉起 MSFP，指令不带前缀，回复就是逐行 print。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.server_dir = self.root / "server"
        self.dist = self.server_dir / "client-dist"
        self.payload = make_dist(self.dist)
        self.config_path = self.root / "config.json"
        self.data_dir = self.root / "autosync-data"
        self.config_path.write_text(json.dumps(self.base_config(), ensure_ascii=False), "utf-8")
        self.outputs = []
        self.shell = None

    def tearDown(self):
        if self.shell is not None:
            self.shell.shutdown()
        self.tmp.cleanup()

    def base_config(self, **overrides):
        data = {
            "dist_dir": "client-dist",
            "tcp_port": 0,
            "tcp_host": "127.0.0.1",
            "modrinth_extensions": [],
            "auto_build_on_start": False,
            "poll_interval_seconds": 0,
        }
        data.update(overrides)
        return data

    def start_shell(self, overrides=None):
        """按（可选覆盖后的）config.json 起一个 shell，并启动 MSFP。"""
        if overrides:
            self.config_path.write_text(
                json.dumps(self.base_config(**overrides), ensure_ascii=False), "utf-8"
            )
        cfg = load_config_file(self.config_path)
        core = AutoSyncCore(
            config=cfg,
            data_dir=self.data_dir,
            base_dir=resolve_base_dir(cfg, cwd=self.server_dir),
            logger=LOGGER,
        )
        self.shell = AutoSyncShell(
            core=core,
            config_path=self.config_path,
            out=self.outputs.append,
            logger=LOGGER,
            color=False,  # 固定去色，输出可与字符串直接比较
        )
        self.shell.start()
        return self.shell

    @property
    def text(self) -> str:
        return "\n".join(self.outputs)

    def wait_for(self, predicate, timeout=30.0, message="等待超时"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        self.fail(message)

    # ---------------------------------------------------------------- 启动 / 状态
    def test_shell_starts_msfp_and_serves_files(self):
        shell = self.start_shell()
        core = shell.core
        self.assertTrue(core.tcp_running, "shell 启动应当拉起 MSFP 服务")
        self.assertGreater(core.tcp_port, 0, "tcp_port=0 应绑定到随机可用端口")
        self.assertIn("MSFP", self.text)
        # 服务确实在后台跑：真客户端能取到分发目录里的文件
        with MsfpClient("127.0.0.1", core.tcp_port) as client:
            self.assertEqual(client.size(CN_JAR), len(self.payload))
            self.assertEqual(client.get(CN_JAR, 0, 15), self.payload[:16])

        self.outputs.clear()
        self.assertTrue(shell.execute("status"))
        self.assertIn("AutoSync 状态", self.text)
        self.assertIn(str(self.dist), self.text)
        self.assertIn("MSFP", self.text)
        self.assertIn(str(self.config_path), self.text)

    def test_shell_build_writes_manifest_and_reports(self):
        shell = self.start_shell()
        core = shell.core
        shell.execute("build")
        self.assertIn("开始构建", self.text)
        self.assertIn("构建完成", self.text)

        result = core.last_result
        self.assertIsNotNone(result)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.file_count, 7, "6 个普通文件 + speedtest.bin")
        self.assertTrue(result.speedtest and result.speedtest["written"])
        self.assertEqual(len(load_manifest(self.dist / "manifest.json")["files"]), 7)

        with MsfpClient("127.0.0.1", core.tcp_port) as client:
            remote = json.loads(client.get("manifest.json").decode("utf-8"))
            self.assertEqual(remote["version"], result.version)
            self.assertEqual(client.size("speedtest.bin"), core.config.speedtest_size)
            self.assertEqual(len(client.get("speedtest.bin", 0, 511)), 512)

        # 幂等：内容未变时版本号不变
        shell.execute("build")
        self.assertGreaterEqual(core.state.get("total_builds", 0), 2)
        self.assertEqual(core.state["version"], result.version)

        self.outputs.clear()
        shell.execute("status")
        self.assertIn(result.version, self.text)
        self.assertIn(str(result.file_count), self.text)
        self.assertIn("speedtest.bin", self.text)
        self.assertIn("MSFP", self.text)

    def test_shell_help_lists_every_command(self):
        shell = self.start_shell()
        shell.execute("help")
        for token in (
            "status",
            "build",
            "build refresh",
            "reload",
            "tcp start|stop|restart",
            "classify",
            "classify apply",
            "deps",
            "deps fix",
            "deps fix 1a 2a",
            "deps fix apply",
            "help",
            "exit | quit",
        ):
            self.assertIn(token, self.text, token)
        # 空行等同 help
        self.outputs.clear()
        self.assertTrue(shell.execute("   "))
        self.assertIn("deps fix 1a 2a", self.text)

    def test_shell_unknown_command_and_exit(self):
        shell = self.start_shell()
        self.assertTrue(shell.execute("nosuchcmd"), "未知命令不该退出 shell")
        self.assertIn("未知命令", self.text)
        self.assertIn("help", self.text)
        self.assertTrue(shell.execute("status"), "status 后应继续留在 shell")
        self.assertFalse(shell.execute("exit"))
        self.assertFalse(shell.execute("quit"))

    # ---------------------------------------------------------------- 服务控制 / 配置
    def test_shell_tcp_control_and_reload(self):
        shell = self.start_shell()
        core = shell.core
        service = core.tcp_service()

        shell.execute("tcp stop")
        self.assertFalse(core.tcp_running)
        shell.execute("tcp start")
        self.assertTrue(core.tcp_running)
        shell.execute("http restart")  # 旧别名，与 tcp 同义
        self.assertTrue(core.tcp_running)
        self.assertIn("服务已重启", self.text)
        with MsfpClient("127.0.0.1", core.tcp_port) as client:
            self.assertEqual(client.size(CN_JAR), len(self.payload))
            for bad in ("../secret.txt", "/etc/passwd", "C:/Windows/win.ini", "mods/../../secret"):
                with self.assertRaises(MsfpError, msg=bad) as ctx:
                    client.size(bad)
                self.assertEqual(str(ctx.exception), "forbidden", bad)

        # reload：改磁盘上的 config.json 后必须生效，且不重建服务实例
        data = json.loads(self.config_path.read_text("utf-8"))
        data["tcp_idle_timeout_seconds"] = 15
        self.config_path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        self.outputs.clear()
        shell.execute("reload")
        self.assertIn("配置已重载", self.text)
        self.assertEqual(core.config.tcp_idle_timeout_seconds, 15)
        self.assertTrue(core.tcp_running)
        self.assertIs(core.tcp_service(), service, "reload 只重启服务，不重建实例")
        with MsfpClient("127.0.0.1", core.tcp_port) as client:
            self.assertEqual(client.size(CN_JAR), len(self.payload))

    def test_config_file_autocreated_with_defaults(self):
        path = self.root / "fresh" / "config.json"
        self.assertFalse(path.exists())
        self.assertTrue(ensure_config_file(path), "配置缺失时应自动生成默认配置")
        payload = json.loads(path.read_text("utf-8"))
        for key in (
            "dist_dir",
            "tcp_host",
            "tcp_port",
            "curseforge_api_key",
            "http_proxy",
            "deps_fix_max_size_mb",
            "classify_move_pure_server",
        ):
            self.assertIn(key, payload, key)
        self.assertEqual(payload["tcp_port"], 8123, "MSFP 端口必须保持 8123")
        self.assertEqual(load_config_file(path).tcp_port, 8123)
        self.assertFalse(ensure_config_file(path), "已存在的配置不得被覆盖")

    def test_legacy_http_keys_and_base_dir_resolution(self):
        """老 config.json 里的 http_port/http_host/http_enabled 必须还能用。"""
        self.config_path.write_text(
            json.dumps(
                {
                    "http_port": 0,
                    "http_host": "127.0.0.1",
                    "http_enabled": True,
                    "dist_dir": "client-dist",
                    "modrinth_extensions": [],
                },
                ensure_ascii=False,
            ),
            "utf-8",
        )
        cfg = load_config_file(self.config_path)
        self.assertEqual(cfg.tcp_host, "127.0.0.1")
        self.assertTrue(cfg.tcp_enabled)
        self.assertEqual(resolve_base_dir(cfg, cwd=self.server_dir), self.server_dir)
        self.assertEqual(
            resolve_base_dir(AutoSyncConfig(base_dir="mcdr_root"), cwd=self.server_dir),
            self.server_dir,
            "旧值 mcdr_root 等价于当前工作目录",
        )
        shell = self.start_shell()
        self.assertTrue(shell.core.tcp_running)
        self.assertEqual(shell.core.dist_dir, self.dist)

    # ---------------------------------------------------------------- 分类 / 依赖
    def test_shell_classify_can_be_disabled(self):
        shell = self.start_shell({"classify_enabled": False})
        shell.execute("classify")
        self.assertIn("已禁用", self.text)
        self.outputs.clear()
        shell.execute("help")
        self.assertNotIn("classify", self.text)

    def test_shell_classify_dry_run_then_refuses_apply(self):
        shell = self.start_shell(
            {
                "modrinth_api_base": "http://127.0.0.1:1/v2",
                "modrinth_max_retries": 0,
                "modrinth_timeout_seconds": 1,
            }
        )
        service = shell.make_classify_service(shell.core)
        self.assertEqual(service.client_mods_dir, self.dist / "mods")
        self.assertEqual(service.server_mods_dir, self.server_dir / "mods")

        before = {p.name: p.read_bytes() for p in (self.dist / "mods").iterdir() if p.is_file()}
        shell.execute("classify")
        self.assertIn("dry-run", self.text)
        self.wait_for(lambda: service.report_path.is_file(), message="分类报告超时")
        self.assertEqual(
            {p.name: p.read_bytes() for p in (self.dist / "mods").iterdir() if p.is_file()},
            before,
            "classify 干跑不得改动任何文件",
        )

        # 服务端 mods 目录不存在 -> apply 必须被拒绝并提示
        self.outputs.clear()
        shell.execute("classify apply")
        self.assertTrue(
            any(("不存在" in line or "未执行" in line) for line in self.outputs), self.text
        )

    def test_shell_deps_fix_needs_cache_and_lists_plan(self):
        shell = self.start_shell(
            {
                "modrinth_api_base": "http://127.0.0.1:1/v2",
                "modrinth_max_retries": 0,
                "modrinth_timeout_seconds": 1,
            }
        )
        # 没有清单缓存时按编号安装必须被拒绝，且不产生任何下载
        before = {p.name for p in (self.dist / "mods").iterdir()}
        self.outputs.clear()
        shell.execute("deps fix 1a")
        self.assertTrue(
            any(("清单缓存" in line or "编号" in line) for line in self.outputs), self.text
        )
        self.assertEqual({p.name for p in (self.dist / "mods").iterdir()}, before)

        # 不带参数：输出缺失前置清单（这里没有缺失，也要正常收尾）
        self.outputs.clear()
        shell.execute("deps fix")
        self.assertTrue(any("缺失前置清单" in line for line in self.outputs), self.text)

    def test_shell_deps_fix_output_is_line_by_line(self):
        """清单必须**逐行**输出：多行绝不被折叠，每项一候选一行，项间空行。"""
        shell = self.start_shell()
        real = shell.make_deps_fix_service(shell.core)
        fix_plan = FixPlan(
            message="缺失前置清单：缺失 2 个，可下载 2 个",
            game_version="1.21.1",
            loader="neoforge",
            max_size_mb=50,
            missing_total=2,
            client_mods_dir=str(real.mods_dir),
            items=[
                FixPlanItem(
                    mod_id="sable",
                    index=1,
                    status=STATUS_PLANNED,
                    source="Modrinth",
                    dependent_files=["createbetterfps-1.21.1-1.1.4.jar"]
                    + ["mod{}.jar".format(i) for i in range(15)],
                    candidates=[
                        FixCandidate(
                            label="a",
                            version_number="2.0.5",
                            version_type="release",
                            filename="sable-2.0.5.jar",
                            url="http://localhost/sable-2.0.5.jar",
                            size=13002342,
                            sha1="a" * 40,
                        )
                    ],
                ),
                FixPlanItem(
                    mod_id="cflib",
                    index=2,
                    status=STATUS_PLANNED,
                    source="CurseForge/官方API",
                    candidates=[
                        FixCandidate(
                            label="a",
                            version_number="1.0.0",
                            version_type="release",
                            filename="cflib-1.0.0.jar",
                            url="http://localhost/cflib-1.0.0.jar",
                            size=1024,
                            sha1="b" * 40,
                        )
                    ],
                ),
            ],
        )

        class _StubDepsFix:
            """只替换网络部分：清单渲染走真实 DepsFixService，保证测的是真排版。"""

            def __init__(self, service, plan_obj):
                self._service = service
                self._plan = plan_obj
                self.report_path = service.report_path

            def plan(self):
                return self._plan

            def plan_lines(self, plan_obj, **kwargs):
                return self._service.plan_lines(plan_obj, **kwargs)

        shell.make_deps_fix_service = lambda core: _StubDepsFix(real, fix_plan)
        self.outputs.clear()
        shell.execute("deps fix")

        replies = list(self.outputs)
        self.assertGreater(len(replies), 8, replies)
        for line in replies:
            self.assertNotIn("\n", line, "每次输出只能是一行")
        # 每个候选独占一行；需求方折叠成「等 N 个」
        self.assertIn("    需要方: createbetterfps-1.21.1-1.1.4.jar 等 16 个", replies)
        self.assertIn("[1] sable [Modrinth]", replies)
        self.assertIn("[2] cflib [CurseForge/官方API]", replies)
        candidate_lines = [line.strip() for line in replies if line.strip()[:2] in ("a)", "b)", "c)")]
        self.assertEqual(len(candidate_lines), 2, replies)
        for line in candidate_lines:
            self.assertLessEqual(len(line), 60, line)
        self.assertIn("", replies, "项与项之间要有空行")


# --------------------------------------------------------------------------- 9. 模组分类与搬运
def make_jar(path: Path, payload: bytes, toml: str = "") -> Path:
    """造一个假 jar：可选写入 ``META-INF/neoforge.mods.toml``（TOML 兜底判定用）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not toml:
        path.write_bytes(payload)
        return path
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("META-INF/neoforge.mods.toml", toml)
        archive.writestr("payload.bin", payload)
    return path


def make_nested_jar_bytes(toml: str = "", payload: bytes = b"nested") -> bytes:
    """把一个假 jar 打成字节流，供 :func:`make_jij_jar` 嵌套用。"""
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if toml:
            archive.writestr("META-INF/neoforge.mods.toml", toml)
        archive.writestr("payload.bin", payload)
    return buffer.getvalue()


def make_jij_jar(
    path: Path,
    toml: str,
    nested=None,
    metadata="auto",
    nested_dir: str = "META-INF/jarjar",
    payload: bytes = b"parent",
) -> Path:
    """造一个**程序生成的、带 JiJ 嵌套 jar** 的父 jar。

    ``nested``: ``{归档内 entry 名: toml 文本 或 原始字节}``；值是 ``bytes`` 时原样写入
    （用来造「损坏的嵌套 jar」），是 ``str`` 时按 toml 打成嵌套 jar。
    ``metadata``: ``"auto"`` 自动生成 ``META-INF/jarjar/metadata.json``，``None`` 不写，
    ``"garbage"`` 写非法 JSON，``dict`` 原样序列化。
    entry 名不带 ``/`` 时会被放到 ``nested_dir`` 下。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    blobs = {}
    for name, value in dict(nested or {}).items():
        entry = name if "/" in name else f"{nested_dir}/{name}"
        blobs[entry] = value if isinstance(value, bytes) else make_nested_jar_bytes(value)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("META-INF/neoforge.mods.toml", toml)
        for entry, blob in blobs.items():
            archive.writestr(entry, blob)
        if metadata is not None:
            if metadata == "auto":
                doc = {
                    "jars": [
                        {
                            "identifier": {"group": "test.group", "artifact": entry},
                            "version": {"range": "[1.0,)", "artifactVersion": "1.0"},
                            "path": entry,
                        }
                        for entry in blobs
                    ]
                }
            elif metadata == "garbage":
                doc = None
                archive.writestr("META-INF/jarjar/metadata.json", "{这不是 JSON")
            else:
                doc = metadata
            if doc is not None:
                archive.writestr("META-INF/jarjar/metadata.json", json.dumps(doc))
        archive.writestr("payload.bin", payload)
    return path


class FakeClassifyApiHandler(BaseHTTPRequestHandler):
    """同时支持 ``POST /v2/version_files`` 与 ``GET /v2/projects`` 的假 Modrinth。"""

    #: {project_id: (client_side, server_side)}；未登记的 project_id 视为"Modrinth 查不到"
    sides = {}

    def log_message(self, *args):  # 静音
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        payload = {}
        for sha1 in body.get("hashes", []):
            payload[sha1] = {
                "id": "ver-" + sha1[:4],
                "project_id": "proj-" + sha1[:8],
                "version_number": "1.0",
                "files": [
                    {
                        "url": f"https://cdn.example/{sha1[:4]}.jar",
                        "filename": "x.jar",
                        "primary": True,
                        "hashes": {"sha1": sha1},
                    }
                ],
            }
        self._send(json.dumps(payload).encode("utf-8"))

    def do_GET(self):  # noqa: N802
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        ids = json.loads((query.get("ids") or ["[]"])[0])
        out = []
        for project_id in ids:
            sides = self.sides.get(project_id)
            if sides is None:
                continue
            out.append(
                {
                    "id": project_id,
                    "slug": project_id,
                    "title": project_id,
                    "client_side": sides[0],
                    "server_side": sides[1],
                    "project_type": "mod",
                    "downloads": 1,
                }
            )
        self._send(json.dumps(out).encode("utf-8"))

    def _send(self, data: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class TestClassify(unittest.TestCase):
    """分类判定与搬运：dry-run 不改文件、apply 复制/移动、冲突不覆盖、备份可查。"""

    @classmethod
    def setUpClass(cls):
        cls.api = ThreadingHTTPServer(("127.0.0.1", 0), FakeClassifyApiHandler)
        cls.api.daemon_threads = True
        cls.api_thread = threading.Thread(target=cls.api.serve_forever, daemon=True)
        cls.api_thread.start()
        cls.api_base = f"http://127.0.0.1:{cls.api.server_address[1]}/v2"

    @classmethod
    def tearDownClass(cls):
        cls.api.shutdown()
        cls.api.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.server_dir = self.root / "server"
        self.dist = self.server_dir / "client-dist"
        self.mods = self.dist / "mods"
        self.target = self.server_dir / "mods"
        self.data = self.root / "data"
        FakeClassifyApiHandler.sides = {}

    def tearDown(self):
        self.tmp.cleanup()

    # ---------------------------------------------------------------- 夹具
    def make_dist(self, target_exists: bool = True) -> None:
        if target_exists:
            self.target.mkdir(parents=True, exist_ok=True)
        make_jar(self.mods / "sodium.jar", b"client-only" * 40)
        make_jar(self.mods / "create.jar", b"both-sides" * 40)
        make_jar(self.mods / "coreprotect.jar", b"server-only" * 40)
        make_jar(self.mods / "mystery.jar", b"unknown-mod" * 40)
        make_jar(
            self.mods / "tomlclient.jar",
            b"toml-client" * 40,
            toml='modLoader="javafml"\n[[mods]]\nmodId="tomlclient"\nclientSideOnly=true\n',
        )
        make_jar(
            self.mods / "tomldisplay.jar",
            b"toml-display" * 40,
            toml='modLoader="javafml"\n[[mods]]\nmodId="tomldisplay"\ndisplayTest="IGNORE_ALL_VERSION"\n',
        )
        (self.mods / "notes.txt").write_bytes(b"not a mod")
        sides = {}
        for name, pair in (
            ("sodium.jar", ("required", "unsupported")),
            ("create.jar", ("required", "required")),
            ("coreprotect.jar", ("unsupported", "required")),
        ):
            sha1 = hashlib.sha1((self.mods / name).read_bytes()).hexdigest()
            sides["proj-" + sha1[:8]] = pair
        FakeClassifyApiHandler.sides = sides

    def service(self, **overrides) -> ClassifyService:
        data = {
            "dist_dir": "client-dist",
            "classify_server_mods_dir": "../mods",
            "classify_unknown_as": "both",
            "modrinth_api_base": self.api_base,
            "modrinth_max_retries": 0,
            "modrinth_timeout_seconds": 3,
            "tcp_enabled": False,
        }
        data.update(overrides)
        config = AutoSyncConfig.from_dict(data)
        return ClassifyService(config, dist_dir=self.dist, data_dir=self.data, logger=LOGGER)

    @staticmethod
    def snapshot(root: Path):
        snap = {}
        for path in sorted(root.rglob("*")):
            if path.is_file():
                snap[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        return snap

    # ---------------------------------------------------------------- dry-run
    def test_analyze_is_dry_run_and_reports_everything(self):
        self.make_dist()
        service = self.service()
        before_mods = self.snapshot(self.mods)
        before_target = self.snapshot(self.target)
        report = service.analyze()

        self.assertTrue(report.ok, report.message)
        self.assertEqual(self.snapshot(self.mods), before_mods, "dry-run 绝不能改动客户端 mods")
        self.assertEqual(self.snapshot(self.target), before_target, "dry-run 绝不能改动服务端 mods")

        counts = report.counts
        self.assertEqual(counts[CATEGORY_CLIENT_ONLY], 2)  # sodium（Modrinth）+ tomlclient（TOML）
        self.assertEqual(counts[CATEGORY_BOTH], 1)
        self.assertEqual(counts[CATEGORY_SERVER_ONLY], 1)
        self.assertEqual(counts[CATEGORY_UNKNOWN], 2)  # mystery + tomldisplay
        self.assertEqual(counts["unknown_as_both"], 2)
        self.assertEqual(counts["unknown_kept"], 0)
        # 默认 classify_move_pure_server=false：纯服务端也只复制，所以复制 4 个、移动 0 个
        self.assertEqual(counts["planned_copy"], 4)  # create + coreprotect + 2 个待定
        self.assertEqual(counts["planned_move"], 0)
        self.assertEqual(counts["planned_keep"], 2)
        self.assertEqual(counts["planned_skip"], 0)
        self.assertEqual(counts["planned_conflict"], 0)
        self.assertEqual(counts["not_mod"], 1)
        self.assertFalse(report.move_pure_server, "默认必须是保守模式（只复制不移走）")
        self.assertEqual(report.modrinth_hits, 3)
        # tomlclient（clientSideOnly）与 tomldisplay（displayTest=IGNORE_ALL_VERSION）都靠 TOML 兜底
        self.assertEqual(report.toml_fallbacks, 2)

        # 绝对路径（两个目录）必须写进报告，便于服主核对
        self.assertEqual(report.source_dir, str(self.dist))
        self.assertEqual(report.target_dir, str(self.target))
        self.assertTrue(report.target_exists)

        payload = json.loads(service.report_path.read_text("utf-8"))
        self.assertEqual(len(payload["entries"]), 7)
        by_name = {entry["name"]: entry for entry in payload["entries"]}
        self.assertEqual(by_name["sodium.jar"]["source"], "modrinth")
        self.assertEqual(by_name["sodium.jar"]["category"], CATEGORY_CLIENT_ONLY)
        self.assertEqual(by_name["sodium.jar"]["action"], "keep")
        self.assertEqual(by_name["tomlclient.jar"]["source"], "toml")
        self.assertEqual(by_name["mystery.jar"]["source"], "unknown")
        self.assertEqual(by_name["mystery.jar"]["action"], "copy")
        self.assertIn("按双端处理", by_name["mystery.jar"]["note"])
        self.assertEqual(by_name["coreprotect.jar"]["action"], "copy")
        # 判定依据必须透明：纯服务端项要带 Modrinth 返回的原始 side 值与 project id
        self.assertEqual(by_name["coreprotect.jar"]["client_side"], "unsupported")
        self.assertEqual(by_name["coreprotect.jar"]["server_side"], "required")
        self.assertTrue(by_name["coreprotect.jar"]["project_id"].startswith("proj-"))
        self.assertEqual(by_name["notes.txt"]["category"], "not_mod")

        lines = "\n".join(service.report_lines(report))
        self.assertIn("dry-run", lines)
        self.assertIn("待定清单", lines)
        self.assertIn("sodium.jar", lines)
        # 计划动作统计必须四类分开
        self.assertIn("计划动作：复制 4 / 移动 0 / 跳过 0 / 冲突 0 / 待定 2", lines)
        # 保守模式的理由要出现在报告里
        self.assertIn("代价不对等", lines)
        # 纯服务端清单要能看到原始依据
        self.assertIn("纯服务端清单（1 项", lines)
        self.assertIn("client_side=unsupported", lines)

    # ---------------------------------------------------------------- apply
    def test_apply_copies_dual_and_moves_server_only(self):
        """classify_move_pure_server=true：恢复旧的「移动」语义。"""
        self.make_dist()
        service = self.service(classify_move_pure_server=True)
        report = service.analyze()
        result = service.apply(report)

        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.copied, 3)
        self.assertEqual(result.moved, 1)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(result.conflicts, 0)
        self.assertEqual(result.unknown, 2)
        self.assertEqual(result.failed, 0)

        # 双端 + 待定（按双端处理）：两边都有且 SHA-256 相同
        for name in ("create.jar", "mystery.jar", "tomldisplay.jar"):
            self.assertTrue((self.mods / name).is_file(), name)
            self.assertTrue((self.target / name).is_file(), name)
            self.assertEqual(sha256_file(self.mods / name), sha256_file(self.target / name), name)

        # 纯客户端：只在客户端
        for name in ("sodium.jar", "tomlclient.jar"):
            self.assertTrue((self.mods / name).is_file(), name)
            self.assertFalse((self.target / name).exists(), name)

        # 纯服务端：只在服务端（已从客户端目录移走）
        self.assertFalse((self.mods / "coreprotect.jar").exists())
        self.assertTrue((self.target / "coreprotect.jar").is_file())

        # 非 jar 文件原地不动
        self.assertTrue((self.mods / "notes.txt").is_file())
        self.assertFalse((self.target / "notes.txt").exists())

        # 备份目录里能找到被移动的原文件
        backup = Path(result.backup_dir)
        self.assertTrue(backup.is_dir(), result.backup_dir)
        self.assertTrue((backup / "mods" / "coreprotect.jar").is_file())
        self.assertEqual(
            sha256_file(backup / "mods" / "coreprotect.jar"), sha256_file(self.target / "coreprotect.jar")
        )
        self.assertTrue(Path(result.report_path).is_file())

    def test_apply_without_report_is_refused(self):
        self.make_dist()
        service = self.service()
        before_mods = self.snapshot(self.mods)
        before_target = self.snapshot(self.target)
        result = service.apply()
        self.assertFalse(result.ok)
        self.assertIn("classify", result.message)
        self.assertEqual(self.snapshot(self.mods), before_mods, "拒绝执行时不得改动任何文件")
        self.assertEqual(self.snapshot(self.target), before_target)
        self.assertTrue((self.mods / "coreprotect.jar").is_file(), "拒绝执行时不得移动任何文件")
        self.assertFalse((self.target / "create.jar").exists())

    def test_apply_refused_when_source_changed(self):
        self.make_dist()
        service = self.service()
        service.analyze()
        (self.mods / "sodium.jar").write_bytes(b"changed after classify")
        result = service.apply()
        self.assertFalse(result.ok)
        self.assertIn("变化", result.message)
        self.assertFalse((self.target / "create.jar").exists(), "拒绝执行时不得改动任何文件")

    def test_apply_refused_when_target_dir_missing(self):
        self.make_dist(target_exists=False)
        service = self.service()
        report = service.analyze()
        self.assertFalse(report.target_exists)
        result = service.apply(report)
        self.assertFalse(result.ok)
        self.assertIn("不存在", result.message)
        self.assertFalse(self.target.exists(), "不得自动创建服务端 mods 目录")

    def test_conflict_is_never_overwritten(self):
        self.make_dist()
        (self.target / "create.jar").write_bytes(b"different server side content")
        service = self.service(classify_move_pure_server=True)
        result = service.apply(service.analyze())

        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.conflicts, 1)
        self.assertEqual(result.copied, 2)
        self.assertEqual((self.target / "create.jar").read_bytes(), b"different server side content")
        self.assertTrue((self.mods / "create.jar").is_file(), "冲突时源文件必须保留")
        conflicts = [item for item in result.actions if item["result"] == "conflict"]
        self.assertEqual([Path(item["target"]).name for item in conflicts], ["create.jar"])

    def test_conflict_blocks_move_and_keeps_source(self):
        self.make_dist()
        (self.target / "coreprotect.jar").write_bytes(b"a different coreprotect")
        service = self.service(classify_move_pure_server=True)
        result = service.apply(service.analyze())
        self.assertEqual(result.conflicts, 1)
        self.assertEqual(result.moved, 0)
        self.assertTrue((self.mods / "coreprotect.jar").is_file(), "冲突时不得删除源文件")
        self.assertEqual((self.target / "coreprotect.jar").read_bytes(), b"a different coreprotect")

    def test_same_content_target_is_skipped(self):
        self.make_dist()
        (self.target / "create.jar").write_bytes((self.mods / "create.jar").read_bytes())
        service = self.service(classify_move_pure_server=True)
        result = service.apply(service.analyze())
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.copied, 2)
        self.assertEqual(result.conflicts, 0)

    def test_move_with_identical_target_cleans_source(self):
        """目标已存在同名同内容文件：跳过复制，但仍按移动语义清理客户端目录里的副本。"""
        self.make_dist()
        (self.target / "coreprotect.jar").write_bytes((self.mods / "coreprotect.jar").read_bytes())
        service = self.service(classify_move_pure_server=True)
        result = service.apply(service.analyze())
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.cleaned, 1)
        self.assertEqual(result.moved, 0)
        self.assertFalse((self.mods / "coreprotect.jar").exists())
        self.assertTrue((Path(result.backup_dir) / "mods" / "coreprotect.jar").is_file())

    def test_backup_can_be_disabled(self):
        self.make_dist()
        service = self.service(classify_backup=False, classify_move_pure_server=True)
        result = service.apply(service.analyze())
        self.assertEqual(result.moved, 1)
        self.assertEqual(result.backup_dir, "")
        self.assertFalse((self.data / "classify-backup").exists())

    def test_unknown_as_none_keeps_unknown_in_place(self):
        self.make_dist()
        service = self.service(classify_unknown_as="none")
        report = service.analyze()
        actions = {entry.name: entry.action for entry in report.entries}
        self.assertEqual(actions["mystery.jar"], "none")
        self.assertEqual(actions["tomldisplay.jar"], "none")
        self.assertEqual(report.counts["unknown_kept"], 2)
        # 只剩确定双端的 create.jar + 纯服务端的 coreprotect.jar（默认只复制）
        self.assertEqual(report.counts["planned_copy"], 2)

        result = service.apply(report)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.copied, 2)
        self.assertTrue((self.mods / "mystery.jar").is_file())
        self.assertFalse((self.target / "mystery.jar").exists())

    # ---------------------------------------------------------------- 保守默认（事故修复）
    def make_dependency_dist(self) -> None:
        """sable.jar 被 Modrinth 判为纯服务端，但 consumer.jar 必需依赖它。"""
        self.target.mkdir(parents=True, exist_ok=True)
        make_jar(
            self.mods / "consumer.jar",
            b"consumer-mod" * 40,
            toml=(
                'modLoader="javafml"\n'
                "[[mods]] #mandatory\n"
                'modId="consumer"\n'
                'version="1.0.0"\n'
                "[[dependencies.consumer]]\n"
                'modId="sable"\n'
                'type="required"\n'
                'versionRange="[2.0,)"\n'
                "[[dependencies.consumer]]\n"
                'modId="minecraft"\n'
                'type="required"\n'
                'versionRange="[1.21,)"\n'
                "[[dependencies.consumer]]\n"
                'modId="jei"\n'
                'type="optional"\n'
                'versionRange="[1.0,)"\n'
            ),
        )
        make_jar(
            self.mods / "sable.jar",
            b"sable-lib" * 40,
            toml='modLoader="javafml"\n[[mods]] #mandatory\nmodId="sable"\nversion="2.0.5"\n',
        )
        sha1 = hashlib.sha1((self.mods / "sable.jar").read_bytes()).hexdigest()
        FakeClassifyApiHandler.sides = {"proj-" + sha1[:8]: ("unsupported", "required")}

    def test_default_keeps_pure_server_mod_in_client_dist(self):
        """新默认 classify_move_pure_server=false：纯服务端 mod 只复制、不移走。"""
        self.make_dist()
        service = self.service()
        report = service.analyze()
        self.assertFalse(report.move_pure_server, "默认必须是保守模式")
        result = service.apply(report)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.moved, 0)
        self.assertEqual(result.copied, 4)
        self.assertTrue((self.mods / "coreprotect.jar").is_file(), "纯服务端 mod 必须仍在客户端目录")
        self.assertTrue((self.target / "coreprotect.jar").is_file(), "同时必须复制到服务端目录")
        self.assertEqual(result.backup_dir, "", "没有移动就没有备份")

    def test_move_pure_server_true_moves_and_warns(self):
        """classify_move_pure_server=true：恢复移动语义，且报告必须有醒目风险提示。"""
        self.make_dist()
        service = self.service(classify_move_pure_server=True)
        report = service.analyze()
        self.assertTrue(report.move_pure_server)
        self.assertEqual(report.counts["planned_move"], 1)
        self.assertEqual(report.counts["planned_copy"], 3)
        lines = "\n".join(service.report_lines(report))
        self.assertIn("!!! 风险", lines)
        self.assertIn("会从 client-dist/mods 移除", lines)
        self.assertIn("计划动作：复制 3 / 移动 1 / 跳过 0 / 冲突 0 / 待定 2", lines)

    def test_jij_self_supplied_dependency_does_not_protect_stray_top_level_mod(self):
        """依赖由依赖方自己的 JiJ 自带时，别处同名的顶层 mod 不该被「依赖保护」钉住。

        语义：consumer.jar 自己就带了 lib（JiJ），另一个顶层 straylib.jar 提供的同名 lib
        对它毫无意义，所以 straylib.jar 在移动到服务端语义下**可以**被移走。
        """
        self.target.mkdir(parents=True, exist_ok=True)
        make_jij_jar(
            self.mods / "consumer.jar",
            mod_toml("consumer", "1.0.0", [("lib", "required", "[1.0,)")]),
            nested={"lib.jar": mod_toml("lib", "1.0.0")},
        )
        stray = make_jar(
            self.mods / "straylib.jar",
            b"stray-lib" * 40,
            toml=mod_toml("lib", "1.0.0"),
        )
        sha1 = hashlib.sha1(stray.read_bytes()).hexdigest()
        FakeClassifyApiHandler.sides = {"proj-" + sha1[:8]: ("unsupported", "required")}

        service = self.service(classify_move_pure_server=True)
        report = service.analyze()
        by_name = {entry.name: entry for entry in report.entries}
        stray_entry = by_name["straylib.jar"]
        consumer_entry = by_name["consumer.jar"]
        self.assertEqual(consumer_entry.jij_mod_ids, ["lib"])
        self.assertEqual(stray_entry.depended_by, [], "自给自足的依赖不该保护无关的顶层同名 mod")
        self.assertFalse(stray_entry.forced_keep)
        self.assertEqual(stray_entry.action, "move")
        self.assertEqual(report.counts["forced_keep"], 0)
        self.assertIn("自带 JiJ 前置 1 个", "\n".join(service.report_lines(report)))

    def test_jij_provider_parent_is_protected(self):
        """前置只由某个父 mod 的 JiJ 提供时，那个父 mod 必须被保护（移走它依赖方就崩）。"""
        self.target.mkdir(parents=True, exist_ok=True)
        make_jar(
            self.mods / "consumer.jar",
            b"consumer-mod" * 40,
            toml=mod_toml("consumer", "1.0.0", [("lib", "required", "[1.0,)")]),
        )
        supplier = make_jij_jar(
            self.mods / "supplier.jar",
            mod_toml("supplier", "1.0.0", []),
            nested={"lib.jar": mod_toml("lib", "1.0.0")},
        )
        sha1 = hashlib.sha1(supplier.read_bytes()).hexdigest()
        FakeClassifyApiHandler.sides = {"proj-" + sha1[:8]: ("unsupported", "required")}

        service = self.service(classify_move_pure_server=True)
        report = service.analyze()
        by_name = {entry.name: entry for entry in report.entries}
        supplier_entry = by_name["supplier.jar"]
        self.assertEqual(supplier_entry.category, CATEGORY_SERVER_ONLY)
        self.assertEqual(supplier_entry.depended_by, ["consumer.jar"])
        self.assertTrue(supplier_entry.forced_keep)
        self.assertEqual(supplier_entry.action, "copy", "提供 JiJ 前置的父 mod 绝不能移走")
        result = service.apply(report)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.moved, 0)
        self.assertTrue((self.mods / "supplier.jar").is_file())

    def test_dependent_pure_server_mod_is_never_moved(self):
        """被其他 mod 依赖的核心前置：即使判为纯服务端、即使开了移动语义，也不移走。"""
        self.make_dependency_dist()
        service = self.service(classify_move_pure_server=True)
        report = service.analyze()
        by_name = {entry.name: entry for entry in report.entries}
        sable = by_name["sable.jar"]
        self.assertEqual(sable.category, CATEGORY_SERVER_ONLY)
        self.assertEqual(sable.action, "copy", "被依赖的 mod 绝不能被移走")
        self.assertTrue(sable.forced_keep)
        self.assertEqual(sable.depended_by, ["consumer.jar"])
        self.assertIn("被 1 个 mod 依赖，已强制保留", sable.note)
        self.assertEqual(report.counts["forced_keep"], 1)

        lines = "\n".join(service.report_lines(report))
        self.assertIn("被 1 个 mod 依赖：consumer.jar", lines)
        self.assertIn("被依赖，已强制保留在客户端", lines)

        result = service.apply(report)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.moved, 0)
        self.assertTrue((self.mods / "sable.jar").is_file(), "被依赖的前置必须留在客户端目录")
        self.assertTrue((self.target / "sable.jar").is_file())

    def test_apply_downgrades_stale_move_plan(self):
        """旧版本生成的报告里可能带 move：apply 必须降级为 copy，绝不删除客户端副本。"""
        self.make_dist()
        service = self.service()
        report = service.analyze()
        core = next(entry for entry in report.entries if entry.name == "coreprotect.jar")
        core.action = "move"  # 模拟旧报告
        core.note = ""
        result = service.apply(report)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.moved, 0)
        self.assertTrue((self.mods / "coreprotect.jar").is_file(), "保守模式下绝不允许移走")
        self.assertIn("降级", core.note)

    def test_config_default_is_conservative(self):
        cfg = AutoSyncConfig.from_dict({})
        self.assertFalse(cfg.classify_move_pure_server)
        self.assertFalse(cfg.deps_check_after_build)  # v0.1 起默认关闭
        self.assertIs(cfg.to_dict()["classify_move_pure_server"], False)
        # 类型纠偏：字符串 "false" 之类不能被当成 True
        self.assertTrue(AutoSyncConfig.from_dict({"classify_move_pure_server": True}).classify_move_pure_server)

    # ---------------------------------------------------------------- 判定规则
    def test_toml_rules(self):
        self.assertEqual(judge_toml("clientSideOnly = true")[0], CATEGORY_CLIENT_ONLY)
        self.assertEqual(judge_toml("  clientSideOnly   =   true  ")[0], CATEGORY_CLIENT_ONLY)
        # TOML 允许行尾注释
        self.assertEqual(judge_toml("clientSideOnly = true #mandatory")[0], CATEGORY_CLIENT_ONLY)
        self.assertIsNone(judge_toml("# clientSideOnly = true")[0])
        self.assertIsNone(judge_toml("clientSideOnly = false")[0])
        self.assertEqual(judge_toml('displayTest="IGNORE_ALL_VERSION"')[0], CATEGORY_UNKNOWN)
        self.assertIsNone(judge_toml('displayTest="MATCH_VERSION"')[0])
        # [[dependencies]] 的 side= 只描述依赖，不能用来判定 mod 自身
        self.assertIsNone(judge_toml('[[dependencies."x"]]\nmodId="neoforge"\nside="SERVER"\n')[0])
        self.assertIsNone(judge_toml(None)[0])
        self.assertIsNone(judge_toml("")[0])

    def test_apply_refused_when_classify_had_query_errors(self):
        """Modrinth 全部查询失败时结果不可信（会大面积变待定），apply 必须拒绝。"""
        self.make_dist()
        service = self.service(
            modrinth_api_base="http://127.0.0.1:1/v2",
            modrinth_max_retries=0,
            modrinth_timeout_seconds=1,
        )
        report = service.analyze()
        self.assertTrue(report.errors, "网络不可达时必须记录查询错误")
        # TOML 兜底仍然生效：tomlclient 判为纯客户端，其余无 Modrinth 信息 -> 待定
        self.assertEqual(report.counts[CATEGORY_CLIENT_ONLY], 1)
        self.assertEqual(report.counts[CATEGORY_UNKNOWN], 5)
        before = self.snapshot(self.mods)
        result = service.apply(report)
        self.assertFalse(result.ok)
        self.assertIn("查询错误", result.message)
        self.assertEqual(self.snapshot(self.mods), before, "拒绝执行时不得改动任何文件")

    def test_project_lookup_batches_and_missing(self):
        FakeClassifyApiHandler.sides = {
            "proj-aaaa": ("required", "unsupported"),
            "proj-bbbb": ("unsupported", "required"),
            "proj-cccc": ("required", "optional"),
        }
        client = ModrinthClient(
            user_agent="test",
            api_base=self.api_base,
            timeout=3,
            max_retries=0,
            logger=LOGGER,
            sleeper=lambda _s: None,
        )
        result = client.lookup_projects(["proj-aaaa", "proj-bbbb", "proj-cccc", "proj-missing", "proj-aaaa"])
        self.assertEqual(result.requested, 4)
        self.assertEqual(result.batches, 1)
        self.assertEqual(len(result.projects), 3)
        self.assertEqual(result.missing, ["proj-missing"])
        self.assertEqual(result.errors, [])
        self.assertTrue(is_unsupported(result.projects["proj-aaaa"].server_side))
        self.assertFalse(is_unsupported(result.projects["proj-cccc"].server_side))

    def test_result_cache_persists_across_instances(self):
        """第二次分析不应再打 Modrinth（缓存落盘 + state.json 复用）。"""
        self.make_dist()
        first = self.service()
        self.assertTrue(first.analyze().ok)
        FakeClassifyApiHandler.sides = {}  # 清空假服务端：若还联网就会全部变成待定
        second = self.service()
        report = second.analyze()
        self.assertEqual(report.counts[CATEGORY_SERVER_ONLY], 1)
        self.assertEqual(report.counts[CATEGORY_CLIENT_ONLY], 2)


# --------------------------------------------------------------------------- 10. 依赖检查
def mod_toml(mod_id: str, version: str = "1.0.0", deps=(), header_comment: bool = True) -> str:
    """造一份 neoforge.mods.toml。

    ``[[mods]]`` 段头默认带 ``#mandatory`` 行内注释 —— 这是真实 mod 的常见写法，
    也是解析时踩过的坑，必须能正确识别 modId。
    """
    lines = ['modLoader="javafml"', 'loaderVersion="[1,)"']
    lines.append("[[mods]] #mandatory" if header_comment else "[[mods]]")
    lines.append(f'modId="{mod_id}"')
    lines.append(f'version="{version}"')
    for dep in deps:
        dep_id, dep_type = dep[0], dep[1]
        version_range = dep[2] if len(dep) > 2 else ""
        lines.append(f"[[dependencies.{mod_id}]]")
        lines.append(f'modId="{dep_id}"')
        lines.append(f'type="{dep_type}"')
        if version_range:
            lines.append(f'versionRange="{version_range}"')
    return "\n".join(lines) + "\n"


class TestDependencyParsing(unittest.TestCase):
    """``neoforge.mods.toml`` 的极简解析与版本范围比较。"""

    def test_array_header_with_inline_comment(self):
        """[[mods]] #mandatory 后面跟行内注释时，modId 必须仍能解析出来。"""
        info = parse_mod_toml(mod_toml("sable", "2.0.5"))
        self.assertEqual(info.mod_ids, ["sable"])
        self.assertEqual(info.versions["sable"], "2.0.5")

    def test_required_optional_discouraged(self):
        text = mod_toml(
            "consumer",
            "1.0.0",
            [
                ("sable", "required", "[2.0,)"),
                ("jei", "optional", "[1.0,)"),
                ("embeddium", "discouraged", ""),
                ("neoforge", "required", "[21.1,)"),
            ],
        )
        info = parse_mod_toml(text)
        required = {dep.mod_id: dep for dep in info.required_dependencies}
        self.assertEqual(set(required), {"sable", "neoforge"})
        self.assertEqual(required["sable"].version_range, "[2.0,)")
        self.assertEqual(required["sable"].owner, "consumer")
        self.assertFalse(required["sable"].dep_type == "optional")
        self.assertEqual(len(info.dependencies), 4)
        # type 缺失时保守地当作必需依赖（漏报比误报危险得多）
        no_type = parse_mod_toml('[[mods]]\nmodId="a"\n[[dependencies.a]]\nmodId="b"\n')
        self.assertEqual([dep.mod_id for dep in no_type.required_dependencies], ["b"])

    def test_quoted_dependency_header_and_multiple_mods(self):
        text = (
            '[[mods]] #mandatory\nmodId="kotlinforforge"\nversion="5.12.0"\n'
            '[[mods]]\nmodId="kotlin"\nversion="2.4.20"\n'
            '[[dependencies."kotlinforforge"]] #前置\nmodId="kotlin"\ntype="required"\n'
        )
        info = parse_mod_toml(text)
        self.assertEqual(info.mod_ids, ["kotlinforforge", "kotlin"])
        self.assertEqual([dep.mod_id for dep in info.required_dependencies], ["kotlin"])
        self.assertEqual(info.required_dependencies[0].owner, "kotlinforforge")

    def test_inline_comment_and_commented_out_lines(self):
        text = (
            '# [[mods]]\nmodLoader="javafml"\n[[mods]]#紧贴注释\nmodId="x" # 我的 mod\n'
            'version="1.0" #版本\n[[dependencies.x]]\nmodId="y"\ntype="required" #必需\n'
        )
        info = parse_mod_toml(text)
        self.assertEqual(info.mod_ids, ["x"])
        self.assertEqual(info.versions["x"], "1.0")
        self.assertEqual(info.required_dependencies[0].mod_id, "y")

    def test_compare_versions(self):
        self.assertLess(compare_versions("1.9.0", "1.10.0"), 0)
        self.assertGreater(compare_versions("2.0.1", "2.0"), 0)
        self.assertEqual(compare_versions("1.0.0", "1.0.0"), 0)
        self.assertLess(compare_versions("1.0", "1.0.0"), 0)

    def test_version_range(self):
        self.assertTrue(version_in_range("2.0.5", "[2.0,)")[0])
        self.assertFalse(version_in_range("1.9", "[2.0,)")[0])
        self.assertTrue(version_in_range("1.5", "(,2.0]")[0])
        self.assertFalse(version_in_range("2.1", "[1.0,2.0)")[0])
        self.assertTrue(version_in_range("1.0", "[1.0]")[0])
        self.assertFalse(version_in_range("1.0.1", "[1.0]")[0])
        self.assertTrue(version_in_range("9.9", "*")[0])
        # Maven 语义：裸版本是「软要求」，任何版本都算满足
        self.assertTrue(version_in_range("1.0", "1.0")[0])
        # 本地版本未知 -> 判不了（返回 None，报告里单独列「无法比较」）
        self.assertIsNone(version_in_range("", "[1.0,)")[0])
        self.assertTrue(version_in_range("2.0", "[1.0,3.0),[5.0,)")[0])
        self.assertFalse(version_in_range("4.0", "[1.0,3.0),[5.0,)")[0])

    def test_read_mod_toml_fallbacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            neoforge = root / "neo.jar"
            make_jar(neoforge, b"neo", toml=mod_toml("neo"))
            text, source = read_mod_toml(neoforge)
            self.assertEqual(source, "META-INF/neoforge.mods.toml")
            self.assertIn("neo", text or "")

            forge = root / "forge.jar"
            with zipfile.ZipFile(forge, "w") as archive:
                archive.writestr("META-INF/mods.toml", mod_toml("forge_mod"))
            self.assertEqual(read_mod_toml(forge)[1], "META-INF/mods.toml")

            not_a_jar = root / "plain.jar"
            not_a_jar.write_bytes(b"not a zip")
            self.assertEqual(read_mod_toml(not_a_jar), (None, ""))


class TestJijDependencies(unittest.TestCase):
    """JiJ（Jar-in-Jar）：嵌套 jar 提供的 modId 必须算「本地已有」，不能误报缺失。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.server_dir = self.root / "server"
        self.dist = self.server_dir / "client-dist"
        self.mods = self.dist / "mods"
        self.server_mods = self.server_dir / "mods"
        self.data = self.root / "data"
        self.mods.mkdir(parents=True)
        self.server_mods.mkdir(parents=True)
        self.config = AutoSyncConfig.from_dict(
            {"dist_dir": "client-dist", "classify_server_mods_dir": "../mods", "tcp_enabled": False}
        )

    def tearDown(self):
        self.tmp.cleanup()

    def service(self) -> DependencyService:
        return DependencyService(self.config, self.dist, self.data, logger=LOGGER)

    def test_nested_jar_provides_dependency(self):
        """父 mod 通过 META-INF/jarjar 自带的前置，不算缺失，归入「JiJ 已提供」。"""
        make_jij_jar(
            self.mods / "superb.jar",
            mod_toml("superb", "1.0.0", [("flywheel", "required", "[1.0.6,)")]),
            nested={"flywheel.jar": mod_toml("flywheel", "1.0.6")},
        )
        make_jar(
            self.mods / "consumer.jar",
            b"consumer" * 20,
            toml=mod_toml("consumer", "1.0.0", [("flywheel", "required", "[1.0.0,2.0)")]),
        )
        service = self.service()
        report = service.analyze()
        self.assertTrue(report.ok, report.message)
        self.assertEqual(report.missing, [], "JiJ 自带的前置绝不能报成缺失")
        self.assertEqual([item.mod_id for item in report.jij_provided], ["flywheel"])
        item = report.jij_provided[0]
        self.assertEqual(item.provided_version, "1.0.6")
        self.assertEqual(item.parent_files, ["superb.jar"])
        self.assertEqual(item.dependent_files, ["consumer.jar", "superb.jar"])
        self.assertEqual(report.jij_jars, 1)
        self.assertEqual(report.jij_mod_ids, 1)
        self.assertIn("真缺失前置 0 个", report.message)
        self.assertIn("JiJ 已提供 1 个", report.message)

        lines = "\n".join(service.report_lines(report))
        self.assertIn("由 superb.jar 通过 JiJ 提供: flywheel 1.0.6", lines)
        self.assertIn("META-INF/jarjar/flywheel.jar", lines)
        self.assertIn("[OK] 未发现缺失前置", lines)

        payload = json.loads(service.report_path.read_text("utf-8"))
        self.assertEqual(payload["missing_count"], 0)
        self.assertEqual(payload["jij_provided_count"], 1)
        self.assertEqual(payload["jij_provided"][0]["mod_id"], "flywheel")
        self.assertEqual(payload["jij_provided"][0]["parent_files"], ["superb.jar"])

    def test_jars_dir_convention_is_recognised(self):
        """嵌套 jar 放在 META-INF/jars/（非官方目录）时也必须识别。"""
        make_jij_jar(
            self.mods / "exposure.jar",
            mod_toml("exposure", "1.0.0", [("mixinextras", "required", "")]),
            nested={"mixinextras.jar": mod_toml("mixinextras", "0.4.1")},
            nested_dir="META-INF/jars",
        )
        report = self.service().analyze()
        self.assertEqual(report.missing, [])
        self.assertEqual([item.mod_id for item in report.jij_provided], ["mixinextras"])
        self.assertEqual(report.jij_provided[0].parent_files, ["exposure.jar"])

    def test_metadata_missing_falls_back_to_directory_scan(self):
        """metadata.json 缺失时退回「扫描 JiJ 目录下所有 .jar」。"""
        make_jij_jar(
            self.mods / "nometa.jar",
            mod_toml("nometa", "1.0.0", [("libx", "required", "")]),
            nested={"libx.jar": mod_toml("libx", "1.0.0")},
            metadata=None,
        )
        report = self.service().analyze()
        self.assertEqual(report.missing, [])
        self.assertEqual([item.mod_id for item in report.jij_provided], ["libx"])

    def test_metadata_garbage_is_recorded_but_falls_back(self):
        """metadata.json 格式异常：记录到「无法解析」，同时仍靠目录扫描兜底。"""
        make_jij_jar(
            self.mods / "badsmeta.jar",
            mod_toml("badsmeta", "1.0.0", [("liby", "required", "")]),
            nested={"liby.jar": mod_toml("liby", "1.0.0")},
            metadata="garbage",
        )
        report = self.service().analyze()
        self.assertEqual(report.missing, [])
        self.assertEqual([item.mod_id for item in report.jij_provided], ["liby"])
        self.assertTrue(
            any("metadata.json" in key for key in report.unparsed_jij),
            report.unparsed_jij,
        )
        lines = "\n".join(self.service().report_lines(report))
        self.assertIn("[嵌套]", lines)

    def test_corrupt_nested_jar_recorded_not_fatal(self):
        """损坏的嵌套 jar 只记录，不影响父 jar 自身的解析。"""
        make_jij_jar(
            self.mods / "parent.jar",
            mod_toml("parent", "1.0.0", []),
            nested={"broken.jar": b"this is not a zip file"},
        )
        report = self.service().analyze()
        self.assertTrue(report.ok, report.message)
        self.assertEqual(report.parsed_files, 1)
        self.assertTrue(any("broken.jar" in key for key in report.unparsed_jij), report.unparsed_jij)
        self.assertEqual(report.unparsed_files, {}, "父 jar 自身有元数据，不该算无法解析")

    def test_multi_level_nesting_and_depth_limit(self):
        """多层嵌套：3 层内识别，超过 JIJ_MAX_DEPTH 的层不再深入（也不能崩）。"""
        level3 = make_nested_jar_bytes(mod_toml("level3", "3.0.0"))
        # 逐层手工组装：level3 装在 level2 里，level2 装在 level1 里，level1 装在顶层里
        import io

        def pack(toml: str, inner_name: str, inner_bytes: bytes) -> bytes:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("META-INF/neoforge.mods.toml", toml)
                archive.writestr(f"META-INF/jarjar/{inner_name}", inner_bytes)
            return buffer.getvalue()

        level2 = pack(mod_toml("level2", "2.0.0"), "level3.jar", level3)
        level1 = pack(mod_toml("level1", "1.0.0"), "level2.jar", level2)
        deep_jar = pack(mod_toml("level4", "4.0.0"), "unused.jar", b"x")
        with zipfile.ZipFile(self.mods / "deep.jar", "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("META-INF/neoforge.mods.toml", mod_toml("deep", "1.0.0"))
            archive.writestr("META-INF/jarjar/level1.jar", level1)
            archive.writestr("META-INF/jarjar/level4.jar", deep_jar)
        scan = scan_jar(self.mods / "deep.jar", "deep.jar")
        found = {ref.mod_id for ref in scan.nested}
        self.assertIn("level1", found, "第 1 层必须识别")
        self.assertIn("level2", found, "第 2 层必须识别")
        self.assertIn("level3", found, "第 3 层必须识别")
        self.assertEqual(JIJ_MAX_DEPTH, 3)
        # 第 4 层（level4 里再嵌的东西）不再深入，但也不能抛异常
        self.assertEqual({ref.mod_id for ref in scan.nested if ref.depth > JIJ_MAX_DEPTH}, set())

    def test_duplicate_metadata_path_counted_once(self):
        """metadata.json 里重复列同一个 path 时，提供者只记一次（防重复计数）。"""
        nested_entry = "META-INF/jarjar/dupe.jar"
        make_jij_jar(
            self.mods / "dupeparent.jar",
            mod_toml("dupeparent", "1.0.0", [("dupelib", "required", "")]),
            nested={"dupe.jar": mod_toml("dupelib", "1.0.0")},
            metadata={
                "jars": [
                    {"identifier": {"group": "g", "artifact": "a"}, "version": {"artifactVersion": "1.0"}, "path": nested_entry},
                    {"identifier": {"group": "g", "artifact": "a"}, "version": {"artifactVersion": "1.0"}, "path": nested_entry},
                ]
            },
        )
        report = self.service().analyze()
        self.assertEqual([item.mod_id for item in report.jij_provided], ["dupelib"])
        self.assertEqual(len(report.jij_provided[0].providers), 1)

    def test_metadata_pointing_to_missing_path_is_recorded(self):
        """metadata.json 指向不存在的 entry：记录，不能让分析崩掉。"""
        make_jij_jar(
            self.mods / "ghostparent.jar",
            mod_toml("ghostparent", "1.0.0", []),
            nested={"real.jar": mod_toml("reallib", "1.0.0")},
            metadata={
                "jars": [
                    {"identifier": {"group": "g", "artifact": "a"}, "version": {"artifactVersion": "1.0"}, "path": "META-INF/jarjar/ghost.jar"}
                ]
            },
        )
        report = self.service().analyze()
        self.assertTrue(report.ok, report.message)
        self.assertTrue(
            any("ghost.jar" in value for value in report.unparsed_jij.values()),
            report.unparsed_jij,
        )

    def test_jij_provided_mod_id_shows_in_graph(self):
        """依赖图层面：JiJ 提供的 modId 计入 is_provided，且保护判定区分「自给」与「外部提供」。"""
        make_jij_jar(
            self.mods / "provider.jar",
            mod_toml("provider", "1.0.0", [("libz", "required", "")]),
            nested={"libz.jar": mod_toml("libz", "1.0.0")},
        )
        make_jar(
            self.mods / "outsider.jar",
            b"outsider" * 20,
            toml=mod_toml("outsider", "1.0.0", [("libz", "required", "")]),
        )
        graph = build_dependency_graph(self.mods)
        self.assertTrue(graph.is_provided("libz"))
        self.assertNotIn("libz", graph.mod_files)
        self.assertEqual(graph.file_jij_mod_ids["provider.jar"], ["libz"])
        # outsider 靠 provider 的 JiJ 满足：必须保护 provider（移走它 outsider 就崩）
        self.assertEqual(graph.protection.get("provider.jar"), ["outsider.jar"])

        # 自给自足不产生保护需求：单独一个「自己带 libz、也只有它依赖 libz」的目录
        solo = self.root / "solo" / "mods"
        solo.mkdir(parents=True)
        make_jij_jar(
            solo / "provider.jar",
            mod_toml("provider", "1.0.0", [("libz", "required", "")]),
            nested={"libz.jar": mod_toml("libz", "1.0.0")},
        )
        solo_graph = build_dependency_graph(solo)
        self.assertEqual(solo_graph.protection, {}, "依赖由自己 JiJ 自带时不该有保护压力")


class TestDependencyService(unittest.TestCase):
    """``deps``：缺失前置 / 服务端可复制源 / 循环依赖 / 版本不匹配。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.server_dir = self.root / "server"
        self.dist = self.server_dir / "client-dist"
        self.mods = self.dist / "mods"
        self.server_mods = self.server_dir / "mods"
        self.data = self.root / "data"
        self.mods.mkdir(parents=True)
        self.server_mods.mkdir(parents=True)
        self.config = AutoSyncConfig.from_dict(
            {
                "dist_dir": "client-dist",
                "classify_server_mods_dir": "../mods",
                "tcp_enabled": False,
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def service(self) -> DependencyService:
        return DependencyService(self.config, self.dist, self.data, logger=LOGGER)

    def test_missing_dependency_with_dependents_and_server_source(self):
        make_jar(
            self.mods / "consumer.jar",
            b"consumer" * 20,
            toml=mod_toml(
                "consumer",
                "1.0.0",
                [("sable", "required", "[2.0,)"), ("minecraft", "required", "[1.21,)")],
            ),
        )
        make_jar(
            self.mods / "other.jar",
            b"other" * 20,
            toml=mod_toml("other", "1.0.0", [("sable", "required", "[2.0,)")]),
        )
        # 服务端 mods 里恰好有这个前置，报告应给出可直接复制的绝对路径
        sable_source = make_jar(
            self.server_mods / "sable-2.0.5.jar",
            b"sable" * 20,
            toml=mod_toml("sable", "2.0.5"),
        )

        service = self.service()
        report = service.analyze()
        self.assertTrue(report.ok, report.message)
        self.assertEqual(report.jar_count, 2)
        self.assertEqual([item.mod_id for item in report.missing], ["sable"])
        item = report.missing[0]
        self.assertEqual(item.version_text, "[2.0,)")
        self.assertEqual(item.dependent_files, ["consumer.jar", "other.jar"])
        self.assertEqual(len(item.server_sources), 1)
        self.assertEqual(item.server_sources[0].abs_path, str(sable_source))
        self.assertEqual(item.server_sources[0].version, "2.0.5")
        # minecraft 是平台项，绝不能算缺失
        self.assertNotIn("minecraft", [entry.mod_id for entry in report.missing])

        payload = json.loads(service.report_path.read_text("utf-8"))
        self.assertEqual(payload["missing_count"], 1)
        self.assertEqual(payload["missing"][0]["mod_id"], "sable")
        self.assertEqual(payload["missing"][0]["copy_from"], str(sable_source))
        self.assertIn("minecraft", payload["platform_ignored"])

        lines = "\n".join(service.report_lines(report))
        self.assertIn("缺失前置 1 个", lines)
        self.assertIn("sable | [2.0,) | 2 个：consumer.jar, other.jar", lines)
        self.assertIn(f"可从 {sable_source} 复制", lines)

    def test_missing_disappears_when_dependency_added(self):
        make_jar(
            self.mods / "consumer.jar",
            b"consumer" * 20,
            toml=mod_toml("consumer", "1.0.0", [("sable", "required", "[2.0,)")]),
        )
        service = self.service()
        self.assertEqual([item.mod_id for item in service.analyze().missing], ["sable"])
        # 把 sable 放回分发目录：缺口必须消失
        make_jar(self.mods / "sable-2.0.5.jar", b"sable" * 20, toml=mod_toml("sable", "2.0.5"))
        report = service.analyze()
        self.assertEqual(report.missing, [])
        self.assertEqual(report.version_mismatches, [])
        self.assertIn("[OK] 未发现缺失前置", "\n".join(service.report_lines(report)))

    def test_server_source_reported_when_directory_missing(self):
        make_jar(
            self.mods / "consumer.jar",
            b"consumer" * 20,
            toml=mod_toml("consumer", "1.0.0", [("sable", "required", "[2.0,)")]),
        )
        import shutil as _shutil

        _shutil.rmtree(self.server_mods)
        report = self.service().analyze()
        self.assertFalse(report.server_mods_exists)
        self.assertEqual(report.copyable_count, 0)
        self.assertIn("服务端 mods 目录不存在", "\n".join(self.service().report_lines(report)))

    def test_cycles_and_version_mismatch(self):
        make_jar(
            self.mods / "a.jar",
            b"a" * 20,
            toml=mod_toml("a", "1.0.0", [("b", "required", "[1.0,)")]),
        )
        make_jar(
            self.mods / "b.jar",
            b"b" * 20,
            toml=mod_toml("b", "1.0.0", [("a", "required", "[2.0,)")]),
        )
        report = self.service().analyze()
        self.assertEqual(report.missing, [], "两边都有，不缺前置")
        self.assertEqual(report.cycles, [["a", "b"]])
        self.assertEqual([item.mod_id for item in report.version_mismatches], ["a"])
        self.assertEqual(report.version_mismatches[0].local_version, "1.0.0")
        self.assertEqual(report.version_mismatches[0].required_range, "[2.0,)")
        lines = "\n".join(self.service().report_lines(report))
        self.assertIn("循环依赖 1 组", lines)
        self.assertIn("版本范围不匹配 1 条", lines)

    def test_optional_and_platform_dependencies_ignored(self):
        make_jar(
            self.mods / "x.jar",
            b"x" * 20,
            toml=mod_toml(
                "x",
                "1.0.0",
                [
                    ("missinglib", "optional", "[1.0,)"),
                    ("minecraft", "required", "[1.21,)"),
                    ("neoforge", "required", "[21.1,)"),
                    ("forge", "required", ""),
                    ("fml", "required", ""),
                ],
            ),
        )
        report = self.service().analyze()
        self.assertEqual(report.missing, [])
        self.assertEqual(report.jar_count, 1)

    def test_unparsed_jar_is_recorded_not_fatal(self):
        make_jar(self.mods / "no-meta.jar", b"raw-bytes" * 10)
        make_jar(self.mods / "good.jar", b"good" * 10, toml=mod_toml("good"))
        report = self.service().analyze()
        self.assertTrue(report.ok, report.message)
        self.assertEqual(report.jar_count, 2)
        self.assertEqual(report.parsed_files, 1)
        self.assertIn("no-meta.jar", report.unparsed_files)

    def test_analyze_is_read_only(self):
        make_jar(
            self.mods / "consumer.jar",
            b"consumer" * 20,
            toml=mod_toml("consumer", "1.0.0", [("sable", "required", "[2.0,)")]),
        )
        before_mods = {p.name: p.read_bytes() for p in self.mods.iterdir()}
        before_server = {p.name: p.read_bytes() for p in self.server_mods.iterdir()}
        self.service().analyze()
        self.assertEqual({p.name: p.read_bytes() for p in self.mods.iterdir()}, before_mods)
        self.assertEqual({p.name: p.read_bytes() for p in self.server_mods.iterdir()}, before_server)

    def test_missing_client_mods_dir(self):
        import shutil as _shutil

        _shutil.rmtree(self.mods)
        report = self.service().analyze()
        self.assertFalse(report.ok)
        self.assertIn("不存在", report.message)


# --------------------------------------------------------------------------- 11. 自动下载缺失前置
class FakeDepsFixHandler(BaseHTTPRequestHandler):
    """假 Modrinth：支持 ``GET /v2/project/{id}``、``/version``、``/v2/search`` 与文件下载。"""

    #: {project_id 或 slug: project dict}
    projects = {}
    #: {project_id: [version dict]}
    versions = {}
    #: {filename: bytes} 供 ``/files/<filename>`` 下载
    files = {}
    #: 下载时故意返回错误内容的文件名集合（验证哈希校验）
    corrupt = set()

    def log_message(self, *args):  # 静音
        pass

    def _send(self, data: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload, status: int = 200) -> None:
        self._send(json.dumps(payload).encode("utf-8"), status)

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/v2/project/"):
            rest = path[len("/v2/project/") :]
            if rest.endswith("/version"):
                project_id = rest[: -len("/version")]
                self._send_json(self.versions.get(project_id, []))
                return
            project = self.projects.get(rest)
            if project is None:
                self._send_json({"error": "not_found"}, status=404)
                return
            self._send_json(project)
            return
        if path == "/v2/search":
            query = (urllib.parse.parse_qs(parsed.query).get("query") or [""])[0]
            hits = [
                project
                for project in self.projects.values()
                if str(project.get("slug")) == query or str(project.get("title")) == query
            ]
            self._send_json({"hits": hits})
            return
        if path.startswith("/files/"):
            name = urllib.parse.unquote(path[len("/files/") :])
            if name in self.corrupt:
                self._send(b"corrupted-content", 200)
                return
            data = self.files.get(name)
            if data is None:
                self._send(b"not found", 404)
                return
            self._send(data, 200)
            return
        self._send(b"{}", 404)


class TestDepsFix(unittest.TestCase):
    """``deps fix``：定位 + 选版 + dry-run 报告；``apply`` 下载 + 哈希校验 + 不覆盖冲突。"""

    @classmethod
    def setUpClass(cls):
        cls.api = ThreadingHTTPServer(("127.0.0.1", 0), FakeDepsFixHandler)
        cls.api.daemon_threads = True
        cls.thread = threading.Thread(target=cls.api.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.api.server_address[1]
        cls.api_base = f"http://127.0.0.1:{cls.port}/v2"
        cls.file_base = f"http://127.0.0.1:{cls.port}/files"

    @classmethod
    def tearDownClass(cls):
        cls.api.shutdown()
        cls.api.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dist = self.root / "server" / "client-dist"
        self.mods = self.dist / "mods"
        self.server_mods = self.root / "server" / "mods"
        self.data = self.root / "data"
        self.mods.mkdir(parents=True)
        self.server_mods.mkdir(parents=True)
        FakeDepsFixHandler.projects = {}
        FakeDepsFixHandler.versions = {}
        FakeDepsFixHandler.files = {}
        FakeDepsFixHandler.corrupt = set()

    def tearDown(self):
        self.tmp.cleanup()

    # ---------------------------------------------------------------- 夹具
    def add_project(self, mod_id, *, slug="", title="", versions=(), direct=True) -> str:
        """注册一个假项目与它的版本；``versions`` 为 ``(版本号, 类型, 发布日期, 文件名, 内容)``。"""
        project_id = "proj-" + mod_id
        project = {
            "id": project_id,
            "project_id": project_id,
            "slug": slug or mod_id,
            "title": title or mod_id,
            "project_type": "mod",
        }
        if direct:
            FakeDepsFixHandler.projects[mod_id] = project
            FakeDepsFixHandler.projects[slug or mod_id] = project
        else:
            # 模拟「slug/id 直接查询 404、只能靠搜索命中」的项目
            FakeDepsFixHandler.projects["hidden-" + mod_id] = project
        entries = []
        for number, vtype, date, filename, data in versions:
            FakeDepsFixHandler.files[filename] = data
            entries.append(
                {
                    "id": f"ver-{mod_id}-{number}",
                    "project_id": project_id,
                    "version_number": number,
                    "version_type": vtype,
                    "date_published": date,
                    "loaders": ["neoforge"],
                    "game_versions": ["1.21.1"],
                    "files": [
                        {
                            "url": f"{self.file_base}/{filename}",
                            "filename": filename,
                            "primary": True,
                            "size": len(data),
                            "hashes": {
                                "sha1": hashlib.sha1(data).hexdigest(),
                                "sha512": hashlib.sha512(data).hexdigest(),
                            },
                        }
                    ],
                }
            )
        FakeDepsFixHandler.versions[project_id] = entries
        return project_id

    def make_consumer(self, deps) -> None:
        make_jar(
            self.mods / "consumer.jar",
            b"consumer" * 20,
            toml=mod_toml("consumer", "1.0.0", [(dep, "required", rng) for dep, rng in deps]),
        )

    def service(self, **overrides) -> DepsFixService:
        data = {
            "dist_dir": "client-dist",
            "classify_server_mods_dir": "../mods",
            "tcp_enabled": False,
            "modrinth_api_base": self.api_base,
            "modrinth_max_retries": 1,
            "modrinth_timeout_seconds": 5,
            "deps_fix_max_size_mb": 50,
            # 这个类只测 Modrinth 路径：关掉 CurseForge 兜底，保证单测不碰真实网络
            "curseforge_api_key": "",
            "curseforge_cfwidget_enabled": False,
        }
        data.update(overrides)
        return DepsFixService(
            AutoSyncConfig.from_dict(data),
            self.dist,
            self.data,
            logger=LOGGER,
            api_base=self.api_base,
        )

    # ---------------------------------------------------------------- dry-run
    def test_plan_locates_selects_and_reports_without_downloading(self):
        """dry-run：定位（直接 + 搜索兜底）、按版本范围过滤、release 优先、排除/超限/无法定位都列出。"""
        self.make_consumer(
            [
                ("sable", "[2.0,)"),
                ("sodium", ""),
                ("rubidium", ""),
                ("embeddium", ""),
                ("biglib", ""),
                ("ghostlib", ""),
            ]
        )
        # sable：1.9.9 不满足 [2.0,)，2.0.5（release）应被选中（beta 更晚但优先级低）
        self.add_project(
            "sable",
            versions=[
                ("1.9.9", "release", "2026-01-03T00:00:00Z", "sable-1.9.9.jar", b"s" * 100),
                ("2.0.5", "release", "2026-01-01T00:00:00Z", "sable-2.0.5.jar", b"s" * 200),
                ("2.1.0-beta", "beta", "2026-01-05T00:00:00Z", "sable-2.1.0-beta.jar", b"s" * 300),
            ],
        )
        # sodium：直接查询 404，只能靠搜索精确匹配命中
        self.add_project(
            "sodium",
            slug="sodium",
            direct=False,
            versions=[("0.6.0", "release", "2026-01-01T00:00:00Z", "sodium-0.6.0.jar", b"n" * 50)],
        )
        self.add_project(
            "rubidium",
            versions=[("0.6.5", "release", "2026-01-01T00:00:00Z", "rubidium-0.6.5.jar", b"r" * 50)],
        )
        self.add_project(
            "embeddium",
            versions=[("1.0.0", "release", "2026-01-01T00:00:00Z", "embeddium-1.0.0.jar", b"e" * 50)],
        )
        self.add_project(
            "biglib",
            versions=[("1.0.0", "release", "2026-01-01T00:00:00Z", "biglib-1.0.0.jar", b"b" * (60 * 1024 * 1024))],
        )
        # ghostlib 不注册 -> 无法定位

        service = self.service(deps_fix_exclude=["embeddium"])
        plan = service.plan()
        self.assertTrue(plan.ok, plan.message)
        by_id = {item.mod_id: item for item in plan.items}
        self.assertEqual(sorted(by_id), ["biglib", "embeddium", "ghostlib", "rubidium", "sable", "sodium"])
        self.assertEqual(sorted(item.mod_id for item in plan.planned), ["rubidium", "sable", "sodium"])
        # 版本范围过滤 + release 优先
        self.assertEqual(by_id["sable"].version_number, "2.0.5")
        self.assertEqual(by_id["sable"].version_type, "release")
        self.assertEqual(by_id["sable"].filename, "sable-2.0.5.jar")
        self.assertEqual(by_id["sable"].size, 200)
        self.assertTrue(by_id["sable"].sha512)
        # 搜索兜底定位
        self.assertIn("搜索", by_id["sodium"].locator)
        self.assertEqual(by_id["sodium"].version_number, "0.6.0")
        # 排除 / 超限 / 无法定位
        self.assertEqual(by_id["embeddium"].status, "excluded")
        self.assertEqual(by_id["biglib"].status, "too_large")
        self.assertEqual(by_id["ghostlib"].status, "not_found")
        # 同类互斥警告（sodium 与 rubidium 同属渲染替换组）
        self.assertTrue(plan.conflicts, plan.conflicts)
        self.assertIn("sodium", "\n".join(plan.conflicts))
        # 候选：a 为推荐（release 优先），beta 排在 b
        self.assertEqual([c.label for c in by_id["sable"].candidates], ["a", "b"])
        self.assertEqual(by_id["sable"].candidates[0].version_number, "2.0.5")
        self.assertEqual(by_id["sable"].candidates[1].version_number, "2.1.0-beta")
        # dry-run 绝不写文件
        self.assertEqual(sorted(p.name for p in self.mods.iterdir()), ["consumer.jar"])
        self.assertTrue(service.report_path.is_file())
        text = "\n".join(service.plan_lines(plan))
        self.assertIn("[1] ", text)
        self.assertIn("2.0.5", text)
        self.assertIn("build", text)
        self.assertIn("互斥", text)
        self.assertIn("deps fix 1a 2a", text)
        # 清单缓存写盘（deps fix <编号...> 要用）
        self.assertTrue(service.plan_cache_path.is_file())

    # ---------------------------------------------------------------- apply
    def test_apply_downloads_verifies_and_skips_existing(self):
        """apply：下载成功 + 哈希校验 + 写入 mods/ + 进度日志 + 汇总；再跑一次按「已存在」跳过。"""
        self.make_consumer([("sable", "[2.0,)")])
        payload = b"sable-real-content" * 100
        self.add_project(
            "sable",
            versions=[("2.0.5", "release", "2026-01-01T00:00:00Z", "sable-2.0.5.jar", payload)],
        )
        service = self.service()
        outcome = service.apply()
        self.assertTrue(outcome.ok, outcome.message)
        self.assertEqual(len(outcome.downloaded), 1)
        self.assertEqual(outcome.failed, [])
        self.assertEqual(outcome.selected, ["1a"], "apply 等价于全选推荐候选（每项取 a）")
        self.assertIn("未选择，已跳过 0 个", outcome.message)
        target = self.mods / "sable-2.0.5.jar"
        self.assertEqual(target.read_bytes(), payload)
        self.assertEqual(outcome.total_bytes, len(payload))
        self.assertIn("成功 1 个", outcome.message)
        self.assertFalse(Path(str(target) + ".part").exists())

        # 第二次：同名且哈希一致 -> 跳过，不重复下载
        again = service.apply()
        self.assertEqual(len(again.downloaded), 0)
        self.assertEqual(len(again.skipped), 1)
        self.assertEqual(again.skipped[0]["reason"], "已存在且哈希一致")

    def test_hash_mismatch_deletes_download_and_reports_failure(self):
        """哈希不符：删除下载的临时文件、不落盘、记为失败。"""
        self.make_consumer([("sable", "")])
        self.add_project(
            "sable",
            versions=[("2.0.5", "release", "2026-01-01T00:00:00Z", "sable-2.0.5.jar", b"good" * 100)],
        )
        FakeDepsFixHandler.corrupt = {"sable-2.0.5.jar"}
        service = self.service()
        outcome = service.apply()
        self.assertEqual(len(outcome.downloaded), 0)
        self.assertEqual(len(outcome.failed), 1)
        self.assertIn("哈希校验失败", outcome.failed[0]["reason"])
        self.assertFalse((self.mods / "sable-2.0.5.jar").exists())
        self.assertFalse(Path(str(self.mods / "sable-2.0.5.jar") + ".part").exists())

    def test_same_name_different_content_is_never_overwritten(self):
        """已存在同名文件但内容不同 -> 列入冲突报告，绝不覆盖。"""
        self.make_consumer([("sable", "")])
        self.add_project(
            "sable",
            versions=[("2.0.5", "release", "2026-01-01T00:00:00Z", "sable-2.0.5.jar", b"new" * 100)],
        )
        existing = self.mods / "sable-2.0.5.jar"
        existing.write_bytes(b"manual-version" * 10)
        outcome = self.service().apply()
        self.assertEqual(len(outcome.downloaded), 0)
        self.assertEqual(len(outcome.conflicts), 1)
        self.assertEqual(existing.read_bytes(), b"manual-version" * 10, "冲突文件必须原样保留")
        text = "\n".join(self.service().outcome_lines(outcome))
        self.assertIn("冲突", text)

    # ---------------------------------------------------------------- 编号清单
    def make_consumers(self, count: int, deps) -> None:
        """造 count 个都依赖同一批前置的 jar（用来验证需求方文件名折叠）。"""
        for i in range(count):
            make_jar(
                self.mods / "consumer{}.jar".format(i),
                b"consumer" * 5,
                toml=mod_toml("consumer{}".format(i), "1.0.0", [(dep, "required", rng) for dep, rng in deps]),
            )

    def read_cache(self) -> dict:
        return json.loads(self.service().plan_cache_path.read_text("utf-8"))

    def write_cache(self, payload: dict) -> None:
        self.service().plan_cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")

    def test_checklist_folds_dependents_and_maps_letters_to_types(self):
        """清单：编号=前置本身；候选按类型固定对应 a/b/c；每行一个候选且行短；需求方折叠成「等 N 个」。"""
        self.make_consumers(5, [("sable", "[2.0,)")])
        self.add_project(
            "sable",
            versions=[
                ("1.9.9", "release", "2026-01-03T00:00:00Z", "sable-1.9.9.jar", b"s" * 100),
                ("2.0.5", "release", "2026-01-01T00:00:00Z", "sable-2.0.5.jar", b"s" * 200),
                ("2.0.6-beta", "beta", "2026-01-02T00:00:00Z", "sable-2.0.6-beta.jar", b"s" * 300),
                ("2.1.0-alpha", "alpha", "2026-01-04T00:00:00Z", "sable-2.1.0-alpha.jar", b"s" * 400),
            ],
        )
        plan = self.service().plan()
        item = plan.items[0]
        self.assertEqual((item.index, item.mod_id), (1, "sable"))
        self.assertEqual(
            [(c.label, c.version_type) for c in item.candidates],
            [("a", "release"), ("b", "beta"), ("c", "alpha")],
        )
        self.assertEqual([c.version_number for c in item.candidates], ["2.0.5", "2.0.6-beta", "2.1.0-alpha"])
        self.assertEqual(item.version_number, "2.0.5", "推荐候选（a）要回填到兼容字段")
        self.assertEqual(item.candidates[0].filename, "sable-2.0.5.jar")
        self.assertEqual(item.source, "Modrinth", "清单每项都要标来源")

        lines = self.service().plan_lines(plan)
        text = "\n".join(lines)
        self.assertIn("[1] sable [Modrinth]", text)
        self.assertIn("需要方: consumer0.jar 等 5 个", text, "需求方只显示一个文件名 + 总数，保证行短")
        self.assertNotIn("（其余", text, "不再拼接多个文件名")
        # 每个候选独占一行：a/b/c 各一行，版本号与类型都在同一行
        rows = {}
        for line in lines:
            stripped = line.strip()
            for label in ("a", "b", "c"):
                if stripped.startswith(label + ") "):
                    rows[label] = stripped
        self.assertEqual(sorted(rows), ["a", "b", "c"], lines)
        self.assertIn("2.0.5", rows["a"])
        self.assertIn("release", rows["a"])
        self.assertIn("2.0.6-beta", rows["b"])
        self.assertIn("beta", rows["b"])
        self.assertIn("2.1.0-alpha", rows["c"])
        self.assertIn("alpha", rows["c"])
        for label, row in rows.items():
            self.assertLessEqual(len(row), 60, "候选行必须短：{}".format(row))
        self.assertNotIn("1.9.9", text, "不满足版本范围的版本不能出现在清单里")


    def test_letters_do_not_shift_when_a_type_is_missing(self):
        """字母与类型固定对应：只有 beta 就只显示 (b)，绝不顺延成 (a)。"""
        self.make_consumer([("betaonly", "")])
        self.add_project(
            "betaonly",
            versions=[("0.2.0-beta", "beta", "2026-01-01T00:00:00Z", "betaonly-0.2.0-beta.jar", b"b" * 20)],
        )
        plan = self.service().plan()
        item = plan.items[0]
        self.assertEqual([c.label for c in item.candidates], ["b"])
        self.assertEqual(item.version_type, "beta")
        text = "\n".join(self.service().plan_lines(plan))
        self.assertIn("b) 0.2.0-beta", text)
        self.assertNotIn("a) 0.2.0-beta", text)

    def test_checklist_one_candidate_per_line_short_and_separated(self):
        """排版：每个候选独占一行、行宽 ≤ 60、超长版本号截断、项与项之间空一行。"""
        self.make_consumer([("longlib", ""), ("otherlib", "")])
        long_version = "1.21.1-neoforge-3.0.0-beta.20260101.123456"
        self.add_project(
            "longlib", versions=[(long_version, "beta", "2026-01-01T00:00:00Z", "longlib.jar", b"L" * 60)]
        )
        self.add_project(
            "otherlib",
            versions=[("1.0.0", "release", "2026-01-01T00:00:00Z", "otherlib-1.0.0.jar", b"O" * 60)],
        )
        plan = self.service().plan()
        lines = self.service().plan_lines(plan)
        text = "\n".join(lines)
        self.assertIn("…", text, "超长版本号要截断")
        self.assertNotIn(long_version, text, "完整超长版本号不许出现在清单里")
        by_id = {item.mod_id: item for item in plan.items}
        mark_long = "[{}] longlib [Modrinth]".format(by_id["longlib"].index)
        mark_other = "[{}] otherlib [Modrinth]".format(by_id["otherlib"].index)
        self.assertIn(mark_long, lines)
        self.assertIn(mark_other, lines)
        lower, upper = sorted((lines.index(mark_long), lines.index(mark_other)))
        self.assertEqual(lines[upper - 1], "", "项与项之间必须空一行")
        for line in lines:
            stripped = line.strip()
            if stripped[:2] in ("a)", "b)", "c)"):
                self.assertLessEqual(len(stripped), 60, stripped)

    def test_show_prerelease_false_lists_release_only(self):
        """deps_fix_show_prerelease=false：只列正式版（只有 (a)）。"""
        self.make_consumer([("sable", ""), ("betaonly", "")])
        self.add_project(
            "sable",
            versions=[
                ("2.0.5", "release", "2026-01-01T00:00:00Z", "sable-2.0.5.jar", b"s" * 20),
                ("2.0.6-beta", "beta", "2026-01-02T00:00:00Z", "sable-2.0.6-beta.jar", b"s" * 30),
            ],
        )
        self.add_project(
            "betaonly",
            versions=[("0.2.0-beta", "beta", "2026-01-01T00:00:00Z", "betaonly-0.2.0-beta.jar", b"b" * 20)],
        )
        plan = self.service(deps_fix_show_prerelease=False).plan()
        by_id = {item.mod_id: item for item in plan.items}
        self.assertEqual([c.label for c in by_id["sable"].candidates], ["a"])
        self.assertEqual(by_id["betaonly"].status, "no_version")
        self.assertIn("deps_fix_show_prerelease", by_id["betaonly"].note)

    # ---------------------------------------------------------------- 选择性安装
    def test_selection_installs_only_chosen_and_lists_unselected(self):
        """deps fix <编号...> 只装选中的项，未选的一律不装，汇总里明确列「未选择，已跳过」。"""
        self.make_consumer([("sable", ""), ("sodium", ""), ("embeddium", "")])
        self.add_project("sable", versions=[("2.0.5", "release", "2026-01-01T00:00:00Z", "sable-2.0.5.jar", b"A" * 120)])
        self.add_project("sodium", versions=[("0.6.0", "release", "2026-01-01T00:00:00Z", "sodium-0.6.0.jar", b"B" * 120)])
        self.add_project("embeddium", versions=[("1.0.0", "release", "2026-01-01T00:00:00Z", "embeddium-1.0.0.jar", b"C" * 120)])

        plan = self.service().plan()
        by_id = {item.mod_id: item for item in plan.items}
        picked = ["{}a".format(by_id["sable"].index), "{}a".format(by_id["embeddium"].index)]

        outcome = self.service().apply_selection(picked)
        self.assertTrue(outcome.ok, outcome.message)
        self.assertEqual(outcome.selected, picked)
        self.assertEqual(
            sorted(r["filename"] for r in outcome.downloaded), ["embeddium-1.0.0.jar", "sable-2.0.5.jar"]
        )
        self.assertFalse((self.mods / "sodium-0.6.0.jar").exists(), "未选中的项绝不能下载")
        self.assertEqual([r["mod_id"] for r in outcome.unselected], ["sodium"])
        self.assertEqual(outcome.unselected[0]["reason"], "未选择，已跳过")
        self.assertIn("未选择，已跳过 1 个", outcome.message)
        self.assertIn("sodium", outcome.message)
        text = "\n".join(self.service().outcome_lines(outcome))
        self.assertIn("未选择，已跳过", text)
        self.assertIn("sodium", text)
        self.assertIn("build", text)

    def test_selection_picks_beta_candidate_by_letter(self):
        """选中 (b) 就必须下 beta 那个文件，而不是推荐候选。"""
        self.make_consumer([("sable", "")])
        self.add_project(
            "sable",
            versions=[
                ("2.0.5", "release", "2026-01-01T00:00:00Z", "sable-2.0.5.jar", b"R" * 60),
                ("2.0.6-beta", "beta", "2026-01-02T00:00:00Z", "sable-2.0.6-beta.jar", b"B" * 70),
            ],
        )
        self.assertTrue(self.service().plan().ok)
        outcome = self.service().apply_selection(["1b"])
        self.assertTrue(outcome.ok, outcome.message)
        self.assertEqual([r["filename"] for r in outcome.downloaded], ["sable-2.0.6-beta.jar"])
        self.assertFalse((self.mods / "sable-2.0.5.jar").exists())

    def test_single_number_equals_recommended_label(self):
        """``1`` 等价 ``1a``。"""
        self.make_consumer([("sable", "")])
        self.add_project("sable", versions=[("2.0.5", "release", "2026-01-01T00:00:00Z", "sable-2.0.5.jar", b"S" * 50)])
        self.assertTrue(self.service().plan().ok)
        outcome = self.service().apply_selection(["1"])
        self.assertTrue(outcome.ok, outcome.message)
        self.assertEqual(outcome.selected, ["1a"])
        self.assertEqual([r["filename"] for r in outcome.downloaded], ["sable-2.0.5.jar"])

    def test_invalid_selection_reports_available_and_downloads_nothing(self):
        """无效编号：清晰报错 + 列出可用编号 + 一次下载都不执行。"""
        self.make_consumer([("sable", ""), ("sodium", "")])
        self.add_project("sable", versions=[("2.0.5", "release", "2026-01-01T00:00:00Z", "sable-2.0.5.jar", b"S" * 50)])
        self.add_project("sodium", versions=[("0.6.0", "release", "2026-01-01T00:00:00Z", "sodium-0.6.0.jar", b"N" * 50)])
        self.assertTrue(self.service().plan().ok)
        for bad in ("1z", "9a", "abc", "1c"):
            before = sorted(p.name for p in self.mods.iterdir())
            outcome = self.service().apply_selection(bad.split())
            self.assertFalse(outcome.ok, bad)
            self.assertEqual(outcome.downloaded, [], bad)
            self.assertEqual(outcome.failed, [], bad)
            self.assertTrue(any("无效编号" in err for err in outcome.errors), (bad, outcome.errors))
            self.assertTrue(any("可用编号" in err for err in outcome.errors), (bad, outcome.errors))
            self.assertEqual(sorted(p.name for p in self.mods.iterdir()), before, "无效编号不得产生任何下载")

    def test_invalid_selection_does_not_install_the_valid_half(self):
        """同一批里混了无效编号 -> 整体放弃，绝不「装一半」。"""
        self.make_consumer([("sable", ""), ("sodium", "")])
        self.add_project("sable", versions=[("2.0.5", "release", "2026-01-01T00:00:00Z", "sable-2.0.5.jar", b"S" * 50)])
        self.add_project("sodium", versions=[("0.6.0", "release", "2026-01-01T00:00:00Z", "sodium-0.6.0.jar", b"N" * 50)])
        self.assertTrue(self.service().plan().ok)
        outcome = self.service().apply_selection(["1a", "9b"])
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.downloaded, [])
        self.assertFalse((self.mods / "sable-2.0.5.jar").exists())

    def test_no_selection_downloads_nothing(self):
        """不填编号时什么都不下载（拒绝并要求给出编号）。"""
        self.make_consumer([("sable", "")])
        self.add_project("sable", versions=[("2.0.5", "release", "2026-01-01T00:00:00Z", "sable-2.0.5.jar", b"S" * 50)])
        self.assertTrue(self.service().plan().ok)
        outcome = self.service().apply_selection([])
        self.assertFalse(outcome.ok)
        self.assertIn("没有给出编号", outcome.message)
        self.assertEqual(outcome.downloaded, [])

    # ---------------------------------------------------------------- 清单缓存
    def test_selection_requires_fresh_plan_cache(self):
        """缓存不存在 / 过期 / 与当前 deps 结果不一致时都拒绝安装。"""
        self.make_consumer([("sable", "")])
        self.add_project("sable", versions=[("2.0.5", "release", "2026-01-01T00:00:00Z", "sable-2.0.5.jar", b"S" * 50)])

        service = self.service()
        self.assertFalse(service.plan_cache_path.exists())
        missing = service.apply_selection(["1a"])
        self.assertFalse(missing.ok)
        self.assertIn("请先运行", missing.message)
        self.assertEqual(missing.downloaded, [])

        self.assertTrue(service.plan().ok)
        self.assertTrue(service.apply_selection(["1a"]).ok, "刚生成的缓存必须可用")

        # 过期：把生成时间改成 31 分钟前
        payload = self.read_cache()
        payload["generated_ts"] = time.time() - 31 * 60
        self.write_cache(payload)
        stale = self.service().apply_selection(["1a"])
        self.assertFalse(stale.ok)
        self.assertIn("已过期", stale.message)

        # 与当前 deps 结果不一致：重新 plan 后多出一个缺失前置
        self.assertTrue(self.service().plan().ok)
        make_jar(
            self.mods / "consumer-new.jar",
            b"c" * 10,
            toml=mod_toml("consumer-new", "1.0.0", [("newlib", "required", "")]),
        )
        self.add_project("newlib", versions=[("1.0.0", "release", "2026-01-01T00:00:00Z", "newlib-1.0.0.jar", b"X" * 10)])
        changed = self.service().apply_selection(["1a"])
        self.assertFalse(changed.ok)
        self.assertIn("依赖结果已变化", changed.message)

    def test_broken_cache_is_refused(self):
        """缓存文件损坏时拒绝安装并要求重新运行 deps fix。"""
        self.make_consumer([("sable", "")])
        self.add_project("sable", versions=[("2.0.5", "release", "2026-01-01T00:00:00Z", "sable-2.0.5.jar", b"S" * 50)])
        service = self.service()
        self.assertTrue(service.plan().ok)
        service.plan_cache_path.write_text("{ not json", "utf-8")
        outcome = self.service().apply_selection(["1a"])
        self.assertFalse(outcome.ok)
        self.assertIn("损坏", outcome.message)
        self.assertEqual(outcome.downloaded, [])


# --------------------------------------------------------------------------- 12. CurseForge 兜底下载源
class FakeCurseForgeHandler(BaseHTTPRequestHandler):
    """假 CurseForge 官方 API v1：search / files / download-url / 401 / 429 / CDN 文件。

    真实 API 强制 ``x-api-key``，这里照样校验：key 不匹配一律 401。
    """

    #: 服务端认可的 API Key（测试里可改成别的值来模拟 401）
    api_key = "test-key"
    #: {slug: mod dict}
    mods = {}
    #: {mod_id: [file dict]}
    files = {}
    #: {filename: bytes} 供 ``/cf-files/<filename>``（模拟 CDN）
    payloads = {}
    #: 这些 fileId 的 download-url 端点直接 429（端点有速率限制）
    download_url_fail = set()
    #: 头 N 次请求强制 429（验证退避重试）
    throttle_search = 0
    #: True = ``/v1/mods/search`` 一律 403（官方对新 key 默认禁用 search，key 本身有效）
    search_forbidden = False
    #: 这些 modId 的 ``/v1/mods/{id}`` 与 ``/files`` 一律 403（作者禁止第三方 API 分发）
    project_forbidden = set()
    requests = []
    download_url_calls = 0

    def log_message(self, *args):  # 静音
        pass

    def _send(self, data: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload, status: int = 200) -> None:
        self._send(json.dumps(payload).encode("utf-8"), status)

    def _send_429(self) -> None:
        self.send_response(429)
        self.send_header("Retry-After", "0")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        FakeCurseForgeHandler.requests.append(self.path)
        # CDN 文件不走 API 鉴权
        if path.startswith("/cf-files/"):
            name = urllib.parse.unquote(path[len("/cf-files/") :])
            data = self.payloads.get(name)
            if data is None:
                self._send(b"not found", 404)
                return
            self._send(data)
            return
        if (self.headers.get("x-api-key") or "") != self.api_key:
            self._send_json({"error": "Unauthorized"}, status=401)
            return
        if path == "/v1/mods/search" and FakeCurseForgeHandler.throttle_search > 0:
            FakeCurseForgeHandler.throttle_search -= 1
            self._send_429()
            return
        if path == "/v1/mods/search" and FakeCurseForgeHandler.search_forbidden:
            # 官方对新 key 默认禁用 search：key 有效也 403
            self._send_json({"error": "Forbidden"}, status=403)
            return
        if path == "/v1/mods/search":
            slug = (query.get("slug") or [""])[0]
            # 故意返回「前缀匹配」的结果（含 slug-legacy 之类的诱饵），验证只取精确 slug
            hits = [mod for mod in self.mods.values() if slug and mod["slug"].startswith(slug)]
            self._send_json({"data": hits})
            return
        if path.startswith("/v1/mods/") and path.endswith("/files"):
            mod_id = path[len("/v1/mods/") : -len("/files")]
            if mod_id in FakeCurseForgeHandler.project_forbidden:
                self._send_json({"error": "Forbidden"}, status=403)
                return
            self._send_json({"data": self.files.get(mod_id, [])})
            return
        if path.startswith("/v1/mods/") and "/files/" not in path:
            mod_id = urllib.parse.unquote(path[len("/v1/mods/") :])
            if mod_id in FakeCurseForgeHandler.project_forbidden:
                self._send_json({"error": "Forbidden"}, status=403)
                return
            project = next((m for m in self.mods.values() if str(m.get("id")) == mod_id), None)
            if project is None:
                self._send_json({"error": "not found"}, status=404)
                return
            self._send_json({"data": project})
            return
        if "/files/" in path and path.endswith("/download-url"):
            file_id = path.rsplit("/files/", 1)[1][: -len("/download-url")]
            FakeCurseForgeHandler.download_url_calls += 1
            if file_id in self.download_url_fail:
                self._send_429()
                return
            name = ""
            for entries in self.files.values():
                for entry in entries:
                    if str(entry.get("id")) == file_id:
                        name = str(entry.get("fileName") or "")
            host = self.headers.get("Host")
            self._send_json(
                {"data": "http://{}/cf-files/{}".format(host, urllib.parse.quote(name))}
            )
            return
        self._send_json({}, 404)


class FakeCFWidgetHandler(BaseHTTPRequestHandler):
    """假 CFWidget（免 key 第三方源）：``/minecraft/mc-mods/<slug>`` 返回项目 JSON。"""

    projects = {}
    requests = []

    def log_message(self, *args):  # 静音
        pass

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        FakeCFWidgetHandler.requests.append(self.path)
        prefix = "/minecraft/mc-mods/"
        payload = None
        if parsed.path.startswith(prefix):
            payload = self.projects.get(urllib.parse.unquote(parsed.path[len(prefix) :]))
        data = json.dumps(payload if payload is not None else {"error": "not found"}).encode("utf-8")
        self.send_response(200 if payload is not None else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class TestDepsFixCurseForge(unittest.TestCase):
    """``deps fix`` 的 CurseForge 兜底：官方 API / CFWidget / 401 / 429 / downloadUrl=null。"""

    @classmethod
    def setUpClass(cls):
        cls.modrinth = ThreadingHTTPServer(("127.0.0.1", 0), FakeDepsFixHandler)
        cls.curseforge = ThreadingHTTPServer(("127.0.0.1", 0), FakeCurseForgeHandler)
        cls.cfwidget = ThreadingHTTPServer(("127.0.0.1", 0), FakeCFWidgetHandler)
        for server in (cls.modrinth, cls.curseforge, cls.cfwidget):
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, daemon=True).start()
        cls.modrinth_base = "http://127.0.0.1:{}/v2".format(cls.modrinth.server_address[1])
        cls.curseforge_base = "http://127.0.0.1:{}".format(cls.curseforge.server_address[1])
        cls.cf_file_base = cls.curseforge_base + "/cf-files"
        cls.cfwidget_base = "http://127.0.0.1:{}".format(cls.cfwidget.server_address[1])

    @classmethod
    def tearDownClass(cls):
        for server in (cls.modrinth, cls.curseforge, cls.cfwidget):
            server.shutdown()
            server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dist = self.root / "server" / "client-dist"
        self.mods = self.dist / "mods"
        self.data = self.root / "data"
        self.mods.mkdir(parents=True)
        (self.root / "server" / "mods").mkdir(parents=True)
        FakeDepsFixHandler.projects = {}
        FakeDepsFixHandler.versions = {}
        FakeDepsFixHandler.files = {}
        FakeDepsFixHandler.corrupt = set()
        FakeCurseForgeHandler.api_key = "test-key"
        FakeCurseForgeHandler.mods = {}
        FakeCurseForgeHandler.files = {}
        FakeCurseForgeHandler.payloads = {}
        FakeCurseForgeHandler.download_url_fail = set()
        FakeCurseForgeHandler.throttle_search = 0
        FakeCurseForgeHandler.search_forbidden = False
        FakeCurseForgeHandler.project_forbidden = set()
        FakeCurseForgeHandler.requests = []
        FakeCurseForgeHandler.download_url_calls = 0
        FakeCFWidgetHandler.projects = {}
        FakeCFWidgetHandler.requests = []

    def tearDown(self):
        self.tmp.cleanup()

    # ---------------------------------------------------------------- 夹具
    def make_consumer(self, deps) -> None:
        make_jar(
            self.mods / "consumer.jar",
            b"consumer" * 20,
            toml=mod_toml("consumer", "1.0.0", [(dep, "required", rng) for dep, rng in deps]),
        )

    def add_modrinth_project(self, mod_id, versions) -> str:
        """在假 Modrinth 上注册项目（``versions`` 为 ``(版本号, 类型, 日期, 文件名, 内容)``）。"""
        project_id = "proj-" + mod_id
        project = {
            "id": project_id,
            "project_id": project_id,
            "slug": mod_id,
            "title": mod_id,
            "project_type": "mod",
        }
        FakeDepsFixHandler.projects[mod_id] = project
        entries = []
        for number, vtype, date, filename, data in versions:
            FakeDepsFixHandler.files[filename] = data
            entries.append(
                {
                    "id": "ver-{}-{}".format(mod_id, number),
                    "project_id": project_id,
                    "version_number": number,
                    "version_type": vtype,
                    "date_published": date,
                    "files": [
                        {
                            "url": "http://127.0.0.1:{}/files/{}".format(self.modrinth.server_address[1], filename),
                            "filename": filename,
                            "primary": True,
                            "size": len(data),
                            "hashes": {
                                "sha1": hashlib.sha1(data).hexdigest(),
                                "sha512": hashlib.sha512(data).hexdigest(),
                            },
                        }
                    ],
                }
            )
        FakeDepsFixHandler.versions[project_id] = entries
        return project_id

    def add_cf_mod(self, slug, files, *, title="", mod_id="") -> str:
        """在假 CurseForge 上注册项目。

        ``files`` 每项为 ``(fileId, displayName, releaseType, date, filename, payload, direct)``；
        ``direct=False`` 表示官方 API 的 ``downloadUrl`` 为 null（必须走 download-url 端点）。
        ``mod_id`` 缺省为 ``cf-<slug>``；官方 search 被禁用时由 CFWidget 提供数字 ID，
        这时要显式传数字（如 ``"778899"``），因为代码要求 CFWidget 的 id 是数字。
        """
        mod_id = str(mod_id or ("cf-" + slug))
        FakeCurseForgeHandler.mods[slug] = {"id": mod_id, "slug": slug, "name": title or slug}
        entries = []
        for file_id, display, release_type, date, filename, payload, direct in files:
            FakeCurseForgeHandler.payloads[filename] = payload
            entries.append(
                {
                    "id": file_id,
                    "displayName": display,
                    "fileName": filename,
                    "releaseType": release_type,
                    "fileDate": date,
                    "fileLength": len(payload),
                    "hashes": [
                        {"algo": 1, "value": hashlib.sha1(payload).hexdigest()},
                        {"algo": 2, "value": hashlib.md5(payload).hexdigest()},
                    ],
                    "downloadUrl": (
                        "{}/{}".format(self.cf_file_base, filename) if direct else None
                    ),
                }
            )
        FakeCurseForgeHandler.files[mod_id] = entries
        return mod_id

    def service(self, **overrides) -> DepsFixService:
        data = {
            "dist_dir": "client-dist",
            "classify_server_mods_dir": "../mods",
            "tcp_enabled": False,
            "modrinth_api_base": self.modrinth_base,
            "modrinth_max_retries": 1,
            "modrinth_timeout_seconds": 5,
            "deps_fix_max_size_mb": 50,
            "curseforge_api_key": "test-key",
            "curseforge_api_base": self.curseforge_base,
            "curseforge_cfwidget_api_base": self.cfwidget_base,
        }
        data.update(overrides)
        return DepsFixService(
            AutoSyncConfig.from_dict(data),
            self.dist,
            self.data,
            logger=LOGGER,
            api_base=self.modrinth_base,
        )

    # ---------------------------------------------------------------- 优先级
    def test_modrinth_hit_does_not_query_curseforge(self):
        """Modrinth 命中时**不查** CurseForge（官方 API 与 CFWidget 都不许被打）。"""
        self.make_consumer([("sable", "")])
        self.add_modrinth_project("sable", [("2.0.5", "release", "2026-01-01T00:00:00Z", "sable-2.0.5.jar", b"S" * 40)])
        self.add_cf_mod("sable", [(1, "2.0.5", 1, "2026-01-01T00:00:00Z", "sable-cf-2.0.5.jar", b"C" * 40, True)])
        plan = self.service().plan()
        item = plan.items[0]
        self.assertEqual(item.status, "planned")
        self.assertEqual(item.source, "Modrinth")
        self.assertEqual(item.filename, "sable-2.0.5.jar")
        self.assertEqual(FakeCurseForgeHandler.requests, [], "Modrinth 命中时不能打 CurseForge 官方 API")
        self.assertEqual(FakeCFWidgetHandler.requests, [], "Modrinth 命中时不能打 CFWidget")

    def test_fallback_to_official_api_selects_and_downloads(self):
        """Modrinth 没有 -> 官方 API 兜底：slug 精确定位、a/b/c 映射、下载并校验 sha1。"""
        self.make_consumer([("cflib", "[2.0,)")])
        self.add_cf_mod(
            "cflib",
            [
                (11, "1.9.9", 1, "2026-01-03T00:00:00Z", "cflib-1.9.9.jar", b"old" * 10, True),
                (12, "2.0.5", 1, "2026-01-01T00:00:00Z", "cflib-2.0.5.jar", b"A" * 120, True),
                (13, "2.1.0-beta", 2, "2026-01-05T00:00:00Z", "cflib-2.1.0-beta.jar", b"B" * 120, True),
                (14, "2.2.0-alpha", 3, "2026-01-06T00:00:00Z", "cflib-2.2.0-alpha.jar", b"C" * 120, True),
            ],
        )
        # 诱饵：slug 前缀相同但不是精确匹配，绝不能被选中
        FakeCurseForgeHandler.mods["cflib-legacy"] = {"id": "cf-decoy", "slug": "cflib-legacy", "name": "decoy"}
        FakeCurseForgeHandler.files["cf-decoy"] = []
        plan = self.service().plan()
        item = plan.items[0]
        self.assertTrue(plan.ok, plan.message)
        self.assertEqual(item.status, "planned")
        self.assertEqual(item.source, "CurseForge/官方API")
        self.assertEqual(item.slug, "cflib")
        self.assertEqual(
            [(c.label, c.version_type) for c in item.candidates],
            [("a", "release"), ("b", "beta"), ("c", "alpha")],
        )
        self.assertEqual(item.version_number, "2.0.5", "[2.0,) 过滤掉 1.9.9")
        self.assertTrue(item.sha1, "官方 API 的 sha1 要用于下载校验")
        text = "\n".join(self.service().plan_lines(plan))
        self.assertIn("[1] cflib [CurseForge/官方API]", text)
        # 请求参数：gameId/slug、gameVersion/modLoader(neoforge->6)/pageSize
        search = next(r for r in FakeCurseForgeHandler.requests if r.startswith("/v1/mods/search"))
        self.assertIn("gameId=432", search)
        self.assertIn("slug=cflib", search)
        self.assertIn("pageSize=10", search)
        listing = next(r for r in FakeCurseForgeHandler.requests if "/files?" in r)
        self.assertIn("pageSize=50", listing)
        self.assertIn("gameVersion=1.21.1", listing)
        self.assertIn("modLoader=6", listing)
        # 真下载 + 哈希校验
        outcome = self.service().apply()
        self.assertEqual([r["filename"] for r in outcome.downloaded], ["cflib-2.0.5.jar"])
        self.assertEqual((self.mods / "cflib-2.0.5.jar").read_bytes(), b"A" * 120)

    def test_release_type_maps_to_letters_and_missing_type_does_not_shift(self):
        """``releaseType`` 1/2/3 -> a/b/c；只有 beta 时只显示 (b)，绝不顺延成 (a)。"""
        self.make_consumer([("betaonly", ""), ("alphalib", "")])
        self.add_cf_mod("betaonly", [(21, "0.2.0-beta", 2, "2026-01-01T00:00:00Z", "betaonly-0.2.0-beta.jar", b"b" * 20, True)])
        self.add_cf_mod("alphalib", [(22, "0.3.0-alpha", 3, "2026-01-01T00:00:00Z", "alphalib-0.3.0-alpha.jar", b"a" * 20, True)])
        plan = self.service().plan()
        by_id = {item.mod_id: item for item in plan.items}
        self.assertEqual([c.label for c in by_id["betaonly"].candidates], ["b"])
        self.assertEqual(by_id["betaonly"].version_type, "beta")
        self.assertEqual([c.label for c in by_id["alphalib"].candidates], ["c"])
        self.assertEqual(by_id["alphalib"].version_type, "alpha")
        text = "\n".join(self.service().plan_lines(plan))
        self.assertIn("b) 0.2.0-beta", text)
        self.assertIn("c) 0.3.0-alpha", text)
        self.assertNotIn("a) 0.2.0-beta", text)

    def test_null_download_url_uses_download_url_endpoint(self):
        """``downloadUrl`` 为 null -> 走 ``/files/{id}/download-url`` 拿临时链接并成功下载。"""
        self.make_consumer([("nulllib", "")])
        payload = b"N" * 90
        self.add_cf_mod("nulllib", [(31, "1.0.0", 1, "2026-01-01T00:00:00Z", "nulllib-1.0.0.jar", payload, False)])
        plan = self.service().plan()
        self.assertEqual(plan.items[0].status, "planned")
        self.assertIn("/cf-files/", plan.items[0].url, "downloadUrl=null 时要用 download-url 端点返回的临时链接")
        self.assertGreaterEqual(FakeCurseForgeHandler.download_url_calls, 1)
        outcome = self.service().apply()
        self.assertEqual([r["filename"] for r in outcome.downloaded], ["nulllib-1.0.0.jar"])
        self.assertEqual((self.mods / "nulllib-1.0.0.jar").read_bytes(), payload)

    def test_download_url_failure_is_reported_not_fatal(self):
        """download-url 端点失败（速率限制）只记报告，不崩，并给人工下载链接。"""
        self.make_consumer([("nullfail", "")])
        self.add_cf_mod("nullfail", [(41, "1.0.0", 1, "2026-01-01T00:00:00Z", "nullfail-1.0.0.jar", b"F" * 50, False)])
        FakeCurseForgeHandler.download_url_fail = {"41"}
        plan = self.service(modrinth_max_retries=0).plan()
        self.assertTrue(plan.ok, "download-url 端点失败不能让整个清单崩掉")
        item = plan.items[0]
        self.assertNotEqual(item.status, "planned")
        self.assertTrue(any("临时下载链接" in err for err in plan.errors), plan.errors)
        self.assertIn("人工下载", item.note)
        self.assertEqual(FakeCurseForgeHandler.download_url_calls, 1)
        self.assertFalse((self.mods / "nullfail-1.0.0.jar").exists())

    def test_size_limit_applies_to_curseforge(self):
        """``fileLength`` 超过 ``deps_fix_max_size_mb`` -> 列为超限，不下。"""
        self.make_consumer([("bigcf", "")])
        self.add_cf_mod("bigcf", [(51, "1.0.0", 1, "2026-01-01T00:00:00Z", "bigcf-1.0.0.jar", b"Z" * (2 * 1024 * 1024), True)])
        plan = self.service(deps_fix_max_size_mb=1).plan()
        self.assertEqual(plan.items[0].status, "too_large")
        self.assertIn("deps_fix_max_size_mb", plan.items[0].note)
        self.assertEqual(self.service(deps_fix_max_size_mb=1).apply().downloaded, [])

    # ---------------------------------------------------------------- 免 key 路径
    def test_missing_key_uses_cfwidget_and_lists_manual_link(self):
        """没配 key：官方 API 一次都不打，改走 CFWidget；没有可校验直链 -> 只给人工下载。"""
        self.make_consumer([("cflib", "")])
        self.add_cf_mod("cflib", [(61, "2.0.5", 1, "2026-01-01T00:00:00Z", "cflib-2.0.5.jar", b"X" * 30, True)])
        FakeCFWidgetHandler.projects["cflib"] = {
            "id": 999,
            "slug": "cflib",
            "title": "CFLib",
            "download": {
                "id": 77,
                "name": "cflib-2.0.5.jar",
                "url": "https://www.curseforge.com/minecraft/mc-mods/cflib/download/77",
                "type": "release",
                "uploaded_at": "2026-01-01T00:00:00Z",
            },
        }
        service = self.service(curseforge_api_key="")
        plan = service.plan()
        item = plan.items[0]
        self.assertEqual(FakeCurseForgeHandler.requests, [], "没有 key 时不能请求官方 API")
        self.assertTrue(FakeCFWidgetHandler.requests, "没有 key 时应走免 key 的 CFWidget")
        self.assertEqual(item.source, "CurseForge/CFWidget")
        self.assertEqual(item.status, STATUS_MANUAL, "第三方源没有可校验直链/哈希 -> 只能人工下载")
        self.assertEqual([c.label for c in item.candidates], ["a"])
        self.assertIn("curseforge.com/minecraft/mc-mods/cflib", item.manual_url)
        text = "\n".join(service.plan_lines(plan))
        self.assertIn("[CurseForge/CFWidget]", text)
        self.assertIn("人工下载", text)
        self.assertIn("禁止自动下载", text)
        # apply（全选推荐）不会碰 manual 项
        outcome = service.apply()
        self.assertEqual(outcome.downloaded, [])
        self.assertEqual([r["mod_id"] for r in outcome.unselected], ["cflib"])
        self.assertEqual(sorted(p.name for p in self.mods.iterdir()), ["consumer.jar"])

    def test_cfwidget_direct_url_with_hash_can_be_downloaded(self):
        """CFWidget 若给了直链 + 哈希，就可以自动下载（防御性解析，坏条目直接跳过）。"""
        self.make_consumer([("freelib", "")])
        payload = b"free" * 40
        FakeCFWidgetHandler.projects["freelib"] = {
            "id": 5,
            "slug": "freelib",
            "title": "FreeLib",
            "files": [
                {
                    "id": 62,
                    "name": "freelib-broken.jar",
                    "url": "https://www.curseforge.com/minecraft/mc-mods/freelib/download/62",
                    "type": "beta",
                },
                "garbage-entry",
                {"nothing": True},
                {
                    "id": 61,
                    "name": "freelib-1.0.0.jar",
                    "url": self.cf_file_base + "/freelib-1.0.0.jar",
                    "type": "release",
                    "uploaded_at": "2026-01-01T00:00:00Z",
                    "sha1": hashlib.sha1(payload).hexdigest(),
                },
            ],
        }
        FakeCurseForgeHandler.payloads["freelib-1.0.0.jar"] = payload
        service = self.service(curseforge_api_key="")
        plan = service.plan()
        item = plan.items[0]
        self.assertEqual(item.source, "CurseForge/CFWidget")
        self.assertEqual(item.status, "planned")
        self.assertEqual(item.candidates[0].filename, "freelib-1.0.0.jar")
        outcome = service.apply()
        self.assertEqual([r["filename"] for r in outcome.downloaded], ["freelib-1.0.0.jar"])
        self.assertEqual((self.mods / "freelib-1.0.0.jar").read_bytes(), payload)

    def test_cfwidget_unparsable_project_falls_back_to_manual_page(self):
        """CFWidget 数据不可解析（第三方源字段变了）-> 记无法定位 + 给人工下载页，不崩。"""
        self.make_consumer([("weirdlib", "")])
        FakeCFWidgetHandler.projects["weirdlib"] = {"id": 7, "slug": "weirdlib", "files": ["x", {"y": 1}, None]}
        plan = self.service(curseforge_api_key="").plan()
        item = plan.items[0]
        self.assertTrue(plan.ok)
        self.assertEqual(item.status, "not_found")
        self.assertIn("CFWidget", item.note)
        self.assertIn("https://www.curseforge.com/minecraft/mc-mods/weirdlib/files", item.manual_url)
        text = "\n".join(self.service(curseforge_api_key="").plan_lines(plan))
        self.assertIn("人工下载: https://www.curseforge.com/minecraft/mc-mods/weirdlib/files", text)

    def test_search_403_falls_back_to_cfwidget_id_then_official_api(self):
        """官方 search 被 403 禁用（新 key 常见）：静默用 CFWidget 拿数字 ID，再回官方 API 取文件。

        验收点：来源仍是 ``[CurseForge/官方API]``（并注明 ID 来自 CFWidget）、
        403 不进报告不刷错误、search 只尝试一次、能真下载。
        """
        self.make_consumer([("geckoanimfix", ""), ("palladium", "")])
        payload_a = b"G" * 80
        payload_b = b"P" * 80
        # 数字 projectId 由 CFWidget 提供，官方 API 用同一个数字 ID 返回文件
        self.add_cf_mod(
            "geckoanimfix",
            [(91, "1.0.0-beta", 2, "2026-01-02T00:00:00Z", "geckoanimfix-1.0.0-beta.jar", payload_a, True)],
            mod_id="778899",
        )
        self.add_cf_mod(
            "palladium",
            [(92, "1.0.0", 1, "2026-01-01T00:00:00Z", "palladium-1.0.0.jar", payload_b, True)],
            mod_id="778900",
        )
        FakeCurseForgeHandler.search_forbidden = True
        FakeCFWidgetHandler.projects["geckoanimfix"] = {"id": 778899, "slug": "geckoanimfix", "title": "GeckoAnimFix"}
        FakeCFWidgetHandler.projects["palladium"] = {"id": 778900, "slug": "palladium", "title": "Palladium"}

        plan = self.service().plan()
        self.assertTrue(plan.ok, plan.message)
        by_id = {item.mod_id: item for item in plan.items}
        for mod_id, expected in (("geckoanimfix", "geckoanimfix-1.0.0-beta.jar"), ("palladium", "palladium-1.0.0.jar")):
            item = by_id[mod_id]
            self.assertEqual(item.source, "CurseForge/官方API", mod_id)
            self.assertEqual(item.status, "planned", mod_id)
            self.assertEqual(item.candidates[0].filename, expected)
            self.assertIn("CFWidget", item.locator, "报告里要注明项目 ID 来自 CFWidget")
        # 403 是"search 被禁用"而不是"key 无效"：不进报告、不刷错误
        self.assertEqual([e for e in plan.errors if "403" in e or "API Key" in e], [], plan.errors)
        searches = [r for r in FakeCurseForgeHandler.requests if r.startswith("/v1/mods/search")]
        self.assertEqual(len(searches), 1, "search 被禁用后不该对每个项重复尝试")
        self.assertTrue(any(r.startswith("/v1/mods/778899") for r in FakeCurseForgeHandler.requests))
        text = "\n".join(self.service().plan_lines(plan))
        self.assertIn("[CurseForge/官方API]（ID 来自 CFWidget）", text)
        # 真下载（官方文件对象带 sha1）
        outcome = self.service().apply()
        self.assertEqual(
            sorted(r["filename"] for r in outcome.downloaded),
            ["geckoanimfix-1.0.0-beta.jar", "palladium-1.0.0.jar"],
        )

    def test_search_403_without_cfwidget_data_is_not_an_error(self):
        """search 403 + CFWidget 也没有该项目 -> 记「无法定位」+ 人工链接，但没有 403 报错。"""
        self.make_consumer([("nolib", "")])
        FakeCurseForgeHandler.search_forbidden = True
        plan = self.service().plan()
        self.assertTrue(plan.ok)
        item = plan.items[0]
        self.assertEqual(item.status, "not_found")
        self.assertEqual([e for e in plan.errors if "403" in e], [], plan.errors)
        self.assertIn("人工下载", item.note)

    def test_project_403_is_item_scoped_and_does_not_disable_official_api(self):
        """项目级 403（作者禁止 API 分发）只影响那一项：回退 CFWidget，其它项继续用官方 API。

        实测：同一把 key 下 JEI（238222）200，而 geckoanimfix（959388）403。
        """
        self.make_consumer([("blockedlib", ""), ("okmod", "")])
        self.add_cf_mod(
            "blockedlib",
            [(95, "1.0.0", 1, "2026-01-01T00:00:00Z", "blockedlib-1.0.0.jar", b"K" * 30, True)],
            mod_id="959388",
        )
        self.add_cf_mod(
            "okmod",
            [(96, "2.0.0", 1, "2026-01-01T00:00:00Z", "okmod-2.0.0.jar", b"D" * 30, True)],
            mod_id="238222",
        )
        FakeCurseForgeHandler.search_forbidden = True
        FakeCurseForgeHandler.project_forbidden = {"959388"}
        FakeCFWidgetHandler.projects["blockedlib"] = {
            "id": 959388,
            "slug": "blockedlib",
            "title": "BlockedLib",
            "download": {
                "id": 1,
                "name": "blockedlib-1.0.0.jar",
                "url": "https://www.curseforge.com/minecraft/mc-mods/blockedlib/download/1",
                "type": "release",
            },
        }
        FakeCFWidgetHandler.projects["okmod"] = {"id": 238222, "slug": "okmod", "title": "OkMod"}

        plan = self.service().plan()
        self.assertTrue(plan.ok)
        by_id = {item.mod_id: item for item in plan.items}
        # 被禁项目：回退 CFWidget（没有可校验直链 -> 人工下载），403 记进报告
        self.assertEqual(by_id["blockedlib"].source, "CurseForge/CFWidget")
        self.assertEqual(by_id["blockedlib"].status, STATUS_MANUAL)
        self.assertTrue(any("403" in err for err in plan.errors), plan.errors)
        self.assertFalse(any("API Key 无效" in err for err in plan.errors), plan.errors)
        # 其它项目：官方 API 没有被全局禁用
        self.assertEqual(by_id["okmod"].source, "CurseForge/官方API")
        self.assertEqual(by_id["okmod"].status, "planned")
        self.assertTrue(any(r.startswith("/v1/mods/238222") for r in FakeCurseForgeHandler.requests))

    # ---------------------------------------------------------------- 鉴权与限流
    def test_401_reports_clear_hint_and_does_not_block_other_items(self):
        """401：明确提示 Key 无效并给申请地址；其它项照常处理；官方 API 不再重复打。"""
        self.make_consumer([("sable", ""), ("cflib", ""), ("otherlib", "")])
        self.add_modrinth_project("sable", [("2.0.5", "release", "2026-01-01T00:00:00Z", "sable-2.0.5.jar", b"S" * 40)])
        self.add_cf_mod("cflib", [(71, "1.0.0", 1, "2026-01-01T00:00:00Z", "cflib-1.0.0.jar", b"C" * 30, True)])
        self.add_cf_mod("otherlib", [(72, "1.0.0", 1, "2026-01-01T00:00:00Z", "otherlib-1.0.0.jar", b"O" * 30, True)])
        FakeCurseForgeHandler.api_key = "some-other-key"  # 模拟 Key 无效 / 未配置
        plan = self.service().plan()
        self.assertTrue(plan.ok, "Key 无效不能阻断整个清单")
        by_id = {item.mod_id: item for item in plan.items}
        self.assertEqual(by_id["sable"].source, "Modrinth")
        self.assertEqual(by_id["sable"].status, "planned")
        hints = [err for err in plan.errors if "CurseForge API Key 无效或未配置" in err]
        self.assertTrue(hints, plan.errors)
        self.assertIn("console.curseforge.com", hints[0])
        for mod_id in ("cflib", "otherlib"):
            self.assertIn("CurseForge API Key 无效或未配置", by_id[mod_id].note, by_id[mod_id].note)
            self.assertNotEqual(by_id[mod_id].status, "planned")
            self.assertIn("人工下载", by_id[mod_id].note)
        searches = [r for r in FakeCurseForgeHandler.requests if r.startswith("/v1/mods/search")]
        self.assertEqual(len(searches), 1, "401 之后不该对每个项重复打官方 API")

    def test_429_on_official_api_backs_off_and_retries(self):
        """429 退避重试一次后成功（读 Retry-After）。"""
        self.make_consumer([("cflib", "")])
        self.add_cf_mod("cflib", [(81, "2.0.5", 1, "2026-01-01T00:00:00Z", "cflib-2.0.5.jar", b"R" * 30, True)])
        FakeCurseForgeHandler.throttle_search = 1
        plan = self.service(modrinth_max_retries=1).plan()
        self.assertEqual(plan.items[0].status, "planned")
        self.assertEqual(plan.items[0].source, "CurseForge/官方API")
        searches = [r for r in FakeCurseForgeHandler.requests if r.startswith("/v1/mods/search")]
        self.assertEqual(len(searches), 2, "429 应退避后重试一次")

    def test_both_failed_gives_manual_download_suggestion(self):
        """两边都失败：状态保留「无法定位」，报告里给 CurseForge 人工下载链接。"""
        self.make_consumer([("nowherelib", "")])
        service = self.service(curseforge_api_key="")
        plan = service.plan()
        item = plan.items[0]
        self.assertEqual(item.status, "not_found")
        self.assertEqual(item.manual_url, "https://www.curseforge.com/minecraft/mc-mods/nowherelib/files")
        self.assertIn("Modrinth", item.note)
        self.assertIn("CFWidget", item.note)
        text = "\n".join(service.plan_lines(plan))
        self.assertIn("人工下载: https://www.curseforge.com/minecraft/mc-mods/nowherelib/files", text)

    # ---------------------------------------------------------------- 代理
    def test_http_proxy_config_and_socks_warning(self):
        """``http_proxy``：http(s) 走 ProxyHandler；SOCKS 标准库不支持 -> 告警忽略，不炸。"""
        from autosync.modrinth import ModrinthClient as _Client, build_opener, normalize_proxy

        self.assertEqual(normalize_proxy(""), ("", ""))
        for value in ("http://127.0.0.1:7890", "https://proxy.example:8080"):
            self.assertEqual(normalize_proxy(value), (value, ""))
        for value in ("socks5h://127.0.0.1:65532", "socks5://127.0.0.1:1080"):
            address, warning = normalize_proxy(value)
            self.assertEqual(address, "")
            self.assertIn("标准库不支持", warning)
        address, warning = normalize_proxy("127.0.0.1:7890")
        self.assertEqual(address, "")
        self.assertIn("无法识别", warning)
        self.assertIsNotNone(build_opener("http://127.0.0.1:7890"))
        # 客户端构造时只告警不抛异常
        client = _Client(user_agent="x", proxy="socks5h://127.0.0.1:65532", logger=LOGGER)
        self.assertEqual(client.proxy, "socks5h://127.0.0.1:65532")
        cfg = AutoSyncConfig.from_dict({"http_proxy": "http://127.0.0.1:7890"})
        self.assertEqual(cfg.http_proxy, "http://127.0.0.1:7890")
        self.assertEqual(cfg.to_dict()["http_proxy"], "http://127.0.0.1:7890")

    # ---------------------------------------------------------------- 配置
    def test_config_example_and_defaults_document_curseforge(self):
        """配置默认值/示例文件都要有 CurseForge 配置项，且示例里 key 必须为空。"""
        from autosync.config import DEFAULT_CONFIG

        example = json.loads((PLUGIN_DIR / "config.example.json").read_text("utf-8"))
        for key in (
            "curseforge_api_key",
            "curseforge_api_base",
            "curseforge_cfwidget_enabled",
            "curseforge_cfwidget_api_base",
            "http_proxy",
        ):
            self.assertIn(key, DEFAULT_CONFIG, key)
            self.assertIn(key, example, key)
        self.assertEqual(example["curseforge_api_key"], "", "示例配置里绝不能带真实 key")
        self.assertEqual(example["curseforge_api_base"], "https://api.curseforge.com")
        self.assertEqual(DEFAULT_CONFIG["curseforge_api_key"], "")
        cfg = AutoSyncConfig.from_dict({})
        self.assertFalse(cfg.curseforge_api_key)
        self.assertEqual(cfg.curseforge_api_base, "https://api.curseforge.com")
        self.assertTrue(cfg.curseforge_cfwidget_enabled)
        self.assertEqual(cfg.to_dict()["curseforge_api_base"], "https://api.curseforge.com")
        # 源码/文档里不许出现 API Key 形态的 UUID（防泄漏回归）
        pattern = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
        for path in sorted(PLUGIN_DIR.rglob("*")):
            if path.is_file() and path.suffix in (".py", ".json", ".md", ".txt"):
                self.assertIsNone(pattern.search(path.read_text("utf-8", errors="replace")), str(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
