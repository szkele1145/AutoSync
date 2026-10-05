#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AutoSync 一键上传 + 刷新清单

把本地 mods 目录里的 jar 增量上传到服务器，然后自动重建清单。

用法：
    python deploy.py               增量上传 + 重建清单
    python deploy.py --full        强制全量上传
    python deploy.py --no-build    只上传，不重建
    python deploy.py --dry-run     只显示会传什么，不实际传

依赖：pip install paramiko

连接参数（**不要写死在文件里**，用环境变量或命令行参数传入）：
    AUTOSYNC_HOST      服务器地址            （必填，例如 sync.example.com）
    AUTOSYNC_PORT      SSH 端口              （默认 22）
    AUTOSYNC_USER      SSH 用户              （默认 root）
    AUTOSYNC_KEY       SSH 私钥路径          （默认 ~/.ssh/id_ed25519）
    AUTOSYNC_MODS      本地 mods 目录         （默认 <仓库根>/mods）
    AUTOSYNC_ROOT      服务器上的部署根目录   （默认 /opt/autosync）
    AUTOSYNC_PYTHON    服务器上的 Python 命令 （默认 python3）

也可以直接用命令行参数覆盖，见 --help。
"""

import argparse
import os
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import paramiko
except ImportError:
    print("缺少 paramiko，请先运行：pip install paramiko")
    sys.exit(1)

# ==================== 配置（全部来自环境变量，仓库里不放任何服务器信息） ====================
_DEFAULT_HOST = os.environ.get("AUTOSYNC_HOST", "")
_DEFAULT_PORT = int(os.environ.get("AUTOSYNC_PORT", "22") or 22)
_DEFAULT_USER = os.environ.get("AUTOSYNC_USER", "root")
_DEFAULT_KEY = os.path.expanduser(os.environ.get("AUTOSYNC_KEY", "~/.ssh/id_ed25519"))
_DEFAULT_MODS = os.environ.get("AUTOSYNC_MODS", str(Path(__file__).resolve().parent.parent / "mods"))
_DEFAULT_ROOT = os.environ.get("AUTOSYNC_ROOT", "/opt/autosync")
_DEFAULT_PYTHON = os.environ.get("AUTOSYNC_PYTHON", "python3")

HOST = _DEFAULT_HOST
PORT = _DEFAULT_PORT
USER = _DEFAULT_USER
KEY_PATH = _DEFAULT_KEY  # SSH 私钥（免密登录，无需密码）
LOCAL_MODS = _DEFAULT_MODS
REMOTE_ROOT = _DEFAULT_ROOT
REMOTE_MODS = REMOTE_ROOT.rstrip("/") + "/client-dist/mods"
PYTHON = _DEFAULT_PYTHON
# =========================================================================================


def human(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.1f %s" % (n, u)
        n /= 1024
    return "%.1f TB" % n


def collect_local():
    if not os.path.isdir(LOCAL_MODS):
        os.makedirs(LOCAL_MODS, exist_ok=True)
        return {}
    out = {}
    for name in os.listdir(LOCAL_MODS):
        p = os.path.join(LOCAL_MODS, name)
        if os.path.isfile(p) and name.lower().endswith(".jar"):
            out[name] = (os.path.getsize(p), os.path.getmtime(p))
    return out


def main():
    ap = argparse.ArgumentParser(description="AutoSync 增量上传 + 重建清单")
    ap.add_argument("--full", action="store_true", help="强制全量上传")
    ap.add_argument("--no-build", action="store_true", help="只上传，不重建清单")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不实际上传")
    ap.add_argument("--delete", action="store_true", help="删除服务器上多余的 mod（默认不删，安全）")
    ap.add_argument("--jobs", type=int, default=4, help="并行上传的文件数（默认 4，实测比单线程快约 2.7 倍；8 收益很小）")
    ap.add_argument("--host", default=_DEFAULT_HOST, help="服务器地址（默认取环境变量 AUTOSYNC_HOST）")
    ap.add_argument("--port", type=int, default=_DEFAULT_PORT, help="SSH 端口（默认取环境变量 AUTOSYNC_PORT）")
    ap.add_argument("--user", default=_DEFAULT_USER, help="SSH 用户（默认取环境变量 AUTOSYNC_USER）")
    ap.add_argument("--key", default=_DEFAULT_KEY, help="SSH 私钥路径（默认取环境变量 AUTOSYNC_KEY）")
    ap.add_argument("--mods", default=_DEFAULT_MODS, help="本地 mods 目录（默认取环境变量 AUTOSYNC_MODS）")
    ap.add_argument("--remote-root", default=_DEFAULT_ROOT, help="服务器部署根目录（默认取环境变量 AUTOSYNC_ROOT）")
    ap.add_argument("--python", default=_DEFAULT_PYTHON, help="服务器上的 Python 命令（默认取环境变量 AUTOSYNC_PYTHON）")
    args = ap.parse_args()

    global HOST, PORT, USER, KEY_PATH, LOCAL_MODS, REMOTE_ROOT, REMOTE_MODS, PYTHON
    HOST = args.host
    PORT = args.port
    USER = args.user
    KEY_PATH = os.path.expanduser(args.key)
    LOCAL_MODS = args.mods
    REMOTE_ROOT = args.remote_root
    REMOTE_MODS = REMOTE_ROOT.rstrip("/") + "/client-dist/mods"
    PYTHON = args.python

    if not HOST:
        print("!! 没有配置服务器地址。")
        print("   请设置环境变量 AUTOSYNC_HOST=<主机名或 IP>，或加 --host 参数。")
        print("   可选的还有 AUTOSYNC_PORT / AUTOSYNC_USER / AUTOSYNC_KEY / AUTOSYNC_MODS /")
        print("   AUTOSYNC_ROOT / AUTOSYNC_PYTHON。参考 docs/部署-Linux.md。")
        return 2
    if not os.path.isfile(KEY_PATH):
        print("!! 找不到 SSH 私钥：%s" % KEY_PATH)
        print("   请设置环境变量 AUTOSYNC_KEY=<私钥路径>，或加 --key 参数。")
        return 2

    local = collect_local()
    print("本地 mods：%d 个 jar  (%s)" % (len(local), LOCAL_MODS))
    if not local:
        print("  目录是空的，先放 .jar 进去")
        return 1

    print("连接 %s:%d ..." % (HOST, PORT))
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(HOST, port=PORT, username=USER, pkey=paramiko.Ed25519Key.from_private_key_file(KEY_PATH), timeout=30)
    except Exception as e:
        print("!! 连接失败：%s" % e)
        return 1

    def run(cmd, timeout=900):
        _, o, e = c.exec_command(cmd, timeout=timeout)
        return o.read().decode("utf-8", "replace"), e.read().decode("utf-8", "replace")

    sftp = c.open_sftp()

    # 远端现状
    try:
        remote = {a.filename: (a.st_size, a.st_mtime)
                  for a in sftp.listdir_attr(REMOTE_MODS)
                  if a.filename.lower().endswith(".jar")}
    except IOError:
        run("mkdir -p '%s'" % REMOTE_MODS)
        remote = {}
    print("远端 mods：%d 个 jar" % len(remote))

    to_upload, unchanged, to_delete = [], [], []
    for name, (sz, mt) in sorted(local.items()):
        r = remote.get(name)
        if r is None or args.full or r[0] != sz or abs(r[1] - mt) > 2:
            to_upload.append(name)
        else:
            unchanged.append(name)
    to_delete = [n for n in sorted(remote) if n not in local] if args.delete else []

    total = sum(local[n][0] for n in to_upload)
    print()
    print("  新增/变更   %d 个  (%s)" % (len(to_upload), human(total)))
    print("  未变化      %d 个" % len(unchanged))
    extra = len([n for n in remote if n not in local])
    print("  远端多余    %d 个%s" % (extra, "  （默认不删，加 --delete 才删）" if extra else ""))
    print()

    if args.dry_run:
        for n in to_upload:
            print("  \u2191 %s  (%s)" % (n, human(local[n][0])))
        for n in to_delete:
            print("  \u2718 %s  (远端多余)" % n)
        print("\n(dry-run，未做任何改动)")
        sftp.close(); c.close()
        return 0

    # 上传
    import threading
    from concurrent.futures import ThreadPoolExecutor

    uploaded = 0
    grand_bytes = 0
    grand_t0 = time.time()
    jobs = max(1, int(args.jobs))

    if jobs == 1:
        # ---- 单线程：逐文件实时进度 ----
        for i, name in enumerate(to_upload, 1):
            src = os.path.join(LOCAL_MODS, name)
            size = local[name][0]
            t0 = time.time()
            st = {"last": 0, "t": t0}

            def on_progress(done, total, _i=i, _n=len(to_upload), _name=name, _st=st):
                now = time.time()
                if now - _st["t"] < 0.4 and done < total:
                    return
                dt = now - _st["t"]
                speed = (done - _st["last"]) / dt / 1048576.0 if dt > 0 else 0.0
                pct = int(done * 100 / total) if total else 100
                sys.stdout.write("\r  [%d/%d] %-40s %3d%%  %7.2f MB/s   "
                                 % (_i, _n, _name[:40], pct, speed))
                sys.stdout.flush()
                _st["last"], _st["t"] = done, now

            try:
                sftp.put(src, REMOTE_MODS + "/" + name, callback=on_progress)
                try:
                    sftp.utime(REMOTE_MODS + "/" + name, (local[name][1], local[name][1]))
                except Exception:
                    pass
                el = time.time() - t0
                avg = size / 1048576.0 / el if el > 0 else 0.0
                sys.stdout.write("\r  [%d/%d] %-40s OK %8s  %5.1fs  %6.2f MB/s   \n"
                                 % (i, len(to_upload), name[:40], human(size), el, avg))
                sys.stdout.flush()
                uploaded += 1
                grand_bytes += size
            except Exception as e:
                sys.stdout.write("\r  [%d/%d] %-40s !! %s\n" % (i, len(to_upload), name[:40], e))
                sys.stdout.flush()
    else:
        # ---- 多线程：每文件独立 SSH 连接，显示总体进度 ----
        lock = threading.Lock()
        state = {"done": 0, "finished": 0}
        prog = {n: 0 for n in to_upload}
        failed = []

        def draw(current=""):
            with lock:
                done = state["done"]
            el = time.time() - grand_t0
            speed = done / el / 1048576.0 if el > 0 else 0.0
            pct = int(done * 100 / total) if total else 100
            sys.stdout.write("\r  总 %3d%%  %7.2f MB/s  %3d/%d 个  %-30s"
                             % (pct, speed, state["finished"], len(to_upload), current[:30]))
            sys.stdout.flush()

        def worker(name):
            src = os.path.join(LOCAL_MODS, name)
            cc = paramiko.SSHClient()
            cc.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                cc.connect(HOST, port=PORT, username=USER, pkey=paramiko.Ed25519Key.from_private_key_file(KEY_PATH), timeout=30)
                sf = cc.open_sftp()
                try:
                    def cb(d, t, _n=name):
                        with lock:
                            state["done"] += d - prog[_n]
                            prog[_n] = d
                        draw(_n)
                    sf.put(src, REMOTE_MODS + "/" + name, callback=cb)
                    try:
                        sf.utime(REMOTE_MODS + "/" + name, (local[name][1], local[name][1]))
                    except Exception:
                        pass
                    with lock:
                        state["finished"] += 1
                    return (name, True, "")
                finally:
                    sf.close()
            except Exception as e:
                with lock:
                    state["finished"] += 1
                return (name, False, str(e))
            finally:
                cc.close()

        with ThreadPoolExecutor(max_workers=jobs) as ex:
            for name, ok, err in ex.map(worker, to_upload):
                if ok:
                    uploaded += 1
                    grand_bytes += local[name][0]
                else:
                    failed.append((name, err))
        sys.stdout.write("\r" + " " * 100 + "\r")
        for name, err in failed:
            print("  !! %s : %s" % (name, err))

    total_el = time.time() - grand_t0
    if uploaded:
        print("  ---- 合计 %s / %.1fs / 平均 %.2f MB/s"
              % (human(grand_bytes), total_el,
                 grand_bytes / 1048576.0 / total_el if total_el > 0 else 0.0))

    # 删除远端多余
    for name in to_delete:
        try:
            sftp.remove(REMOTE_MODS + "/" + name)
            print("  \u2718 已从服务器删除 %s" % name)
        except Exception as e:
            print("  !! 删除失败 %s: %s" % (name, e))

    print("\n上传完成：%d 个" % uploaded)
    sftp.close()

    if args.no_build:
        c.close()
        return 0

    # 重建清单
    print("\n=== 重建清单 ===")
    out, err = run("cd '%s' && %s -m autosync --base . build 2>&1" % (REMOTE_ROOT, PYTHON))
    print(out.strip())
    if err.strip():
        print("[stderr] " + err.strip())

    print("\n=== 服务状态 ===")
    out, _ = run("cd '%s' && %s -m autosync --base . status 2>&1 | head -12" % (REMOTE_ROOT, PYTHON))
    print(out.strip())

    c.close()
    print("\n完成。提示：如果服务正在跑，玩家下次启动就会自动同步新清单。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
