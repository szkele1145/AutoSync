# 部署到 Linux（Ubuntu 24.04）

本文档从一台**全新的 Ubuntu 24.04** 开始，把 AutoSync 独立 Python 版服务端跑起来、开机自启、并通过 frp 把 MSFP 端口暴露到公网。

全程只需要 Python 标准库，**不需要 Docker、不需要 Nginx、不需要 requests**。

---

## 0. 前置条件

| 项 | 要求 |
| --- | --- |
| 系统 | Ubuntu 24.04 LTS（22.04 同样适用） |
| Python | 3.11 及以上（Ubuntu 24.04 自带 3.12） |
| 权限 | 能 `sudo` |
| 网络 | 能出网访问 `api.modrinth.com`（查 CDN 直链用）；玩家的更新源入口由 frp 提供 |
| 客户端更新源 | 一个公网可访问的 `主机:端口`（本文用 `mc.example.net:8123` 举例） |

---

## 1. 安装 Python

Ubuntu 24.04 默认已装 Python 3.12，先确认：

```bash
python3 --version
# 期望输出：Python 3.12.x
```

如果没有（或版本低于 3.11）：

```bash
sudo apt update
sudo apt install -y python3 python3-venv
python3 --version
```

AutoSync 不依赖任何第三方包，所以**不需要** pip / venv。建议装个 git 方便更新：

```bash
sudo apt install -y git
```

---

## 2. 放置代码

```bash
sudo mkdir -p /opt/autosync
sudo chown "$USER":"$USER" /opt/autosync
cd /opt/autosync

# 方式 A：从仓库克隆（推荐，方便以后 git pull 更新）
git clone <你的仓库地址> repo
# 方式 B：把你的 AutoSync 目录直接上传到 /opt/autosync/repo

ls repo/server/python
# 期望看到：autosync/  config.example.json  requirements.txt  README.md
```

验证程序能跑：

```bash
cd /opt/autosync/repo/server/python
python3 -m autosync --version
# 期望输出：AutoSync 1.0.0
```

> 如果报 `No module named autosync`，说明你不在 `server/python` 目录下，或者目录名被改过。`autosync/` 这个包名和相对位置都不能动。

---

## 3. 准备分发目录

`dist_dir` 是"要发给玩家的文件"的根目录。约定成 `client-dist/`，里面按客户端 `base-path` 的相对路径组织：

```bash
cd /opt/autosync/repo/server
mkdir -p client-dist/mods
```

把要发的模组放进去。**推荐用硬链接**，这样客户端分发目录和服务端 `mods/` 指向同一份文件，不占额外磁盘：

```bash
cd /opt/autosync/repo/server
for f in mods/*.jar; do
  name="$(basename "$f")"
  case "$name" in
    # 这些是纯服务端模组，不要发给客户端（按你自己的情况增删）
    coreprotect*|LuckPerms*|tabtps*|spark*) continue;;
  esac
  ln -f "$f" "client-dist/mods/$name"
done

ls client-dist/mods | head
```

只想额外发给客户端的模组（服务端不装）直接丢进 `client-dist/mods/` 即可。

> 目录结构示例：
> ```
> /opt/autosync/repo/server/
> ├── mods/                 服务端自己的模组
> ├── client-dist/          要分发给客户端的根目录（= dist_dir）
> │   ├── mods/             要发的模组
> │   ├── config/           可选：要发的一份默认配置
> │   └── manifest.json     构建时自动生成
> ├── config.json           服务端配置（第一次运行自动生成）
> └── autosync-data/        运行状态与缓存（state.json / 各类报告）
> ```

---

## 4. 生成并修改配置

第一次运行会在**当前工作目录**生成 `config.json`：

```bash
cd /opt/autosync/repo/server
python3 -m autosync --base . status
```

该命令会创建 `config.json`、打印状态（此时文件数应为 0），然后退出。接着改这几项：

```bash
nano config.json
```

至少要确认的键：

| 键 | 建议值 | 原因 |
| --- | --- | --- |
| `dist_dir` | `"client-dist"` | 默认值，指向上一步建好的目录 |
| `base_dir` | `""` | 空 = 相对当前工作目录解析（systemd 里由 `WorkingDirectory` 决定） |
| `tcp_host` | `"0.0.0.0"` | 要接受 frp 从本机转发过来的连接 |
| `tcp_port` | `8123` | frp 的 `local_port` 要指向它 |
| `tcp_enabled` | `true` | 关掉就不分发文件了 |
| `speedtest_enabled` | `true` | 客户端靠 `speedtest.bin` 做多源测速 |
| `poll_interval_seconds` | `0` | 0 = 关闭定时检测；用 `tools/deploy.py` 上传后手动 build 更可控 |
| `allow_empty_dist` | `false` | **保持 false**：目录配错时宁可构建失败，也不要生成"全部删除"的清单把客户端 mods 清空 |
| `classify_server_mods_dir` | `"../mods"` | 分类时服务端 mods 的位置（相对 `dist_dir`） |
| `deps_fix_game_version` | 你的版本，如 `"1.21.1"` | 自动补前置时用它筛版本 |
| `deps_fix_loader` | 你的加载器，如 `"neoforge"` | 同上 |
| `curseforge_api_key` | `""` 或你的 key | 留空则用免 key 的 CFWidget 兜底（数据可能滞后） |

完整键与注释见 `server/python/config.example.json`。

---

## 5. 构建清单并验证

```bash
cd /opt/autosync/repo/server
python3 -m autosync --base . build
```

期望输出类似（QBM 风格框）：

```
  ✔ 文件总数    123 个 / 277.8 MB（jar 118 个）
  ✔ 清单版本    2025-06-01-001（内容有变化）
    清单大小    1.2 MB · 待删除 0 条
```

确认产物：

```bash
ls -lh client-dist/manifest.json client-dist/speedtest.bin
python3 -c "import json;d=json.load(open('client-dist/manifest.json'));print('version',d['version'],'files',len(d['files']))"
```

跑一遍协议自检（会临时起一个 MSFP 服务，做 PING / SIZE / GET / 分块 / 并发 / 路径逃逸拦截）：

```bash
python3 -m autosync --base . check
# 期望最后一行：结果：全部通过
```

---

## 6. 手动启动（常驻）

```bash
cd /opt/autosync/repo/server
python3 -m autosync --base . serve
```

期望输出：

```
  ✔ MSFP    服务运行中：0.0.0.0:8123（协议 MSFP v1）· 目录 /opt/autosync/repo/server/client-dist（Ctrl+C 退出）
```

在另一台机器验证端口通不通：

```bash
# 在服务器本机
ss -lntp | grep 8123

# 在任意一台能访问的机器上（先确认防火墙/安全组已放行）
nc -vz <服务器IP> 8123
# 期望：succeeded!
```

`Ctrl+C` 停止。

也可以进交互式 shell（启动即拉起服务，再进 `AutoSync> ` 提示符）：

```bash
python3 -m autosync --base . shell
# 提示符里输入 help 看命令，exit / quit 退出
```

---

## 7. systemd 开机自启

创建服务单元（**全文如下，可直接复制**）：

```bash
sudo nano /etc/systemd/system/autosync.service
```

```ini
[Unit]
Description=AutoSync MSFP mod distribution server
Documentation=https://github.com/
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# 换成实际的运行用户（不要用 root 跑业务进程）
User=ubuntu
Group=ubuntu
# 关键：工作目录决定 config.json / client-dist / autosync-data 的位置
WorkingDirectory=/opt/autosync/repo/server
# 关键：--base . 让 dist_dir 相对工作目录解析
ExecStart=/usr/bin/python3 -m autosync --base . serve
Restart=always
RestartSec=5
# 日志走 journald
StandardOutput=journal
StandardError=journal
SyslogIdentifier=autosync
# 环境变量：关掉终端颜色，日志更干净
Environment=NO_COLOR=1
Environment=PYTHONUNBUFFERED=1
# 一点基本加固
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

把上面 `User` / `Group` / `WorkingDirectory` / `ExecStart` 换成你的实际情况，然后：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now autosync
sudo systemctl status autosync --no-pager
```

看日志：

```bash
sudo journalctl -u autosync -f
```

常用操作：

```bash
sudo systemctl restart autosync     # 改了 config.json 后重启
sudo systemctl stop autosync
sudo systemctl disable autosync     # 取消开机自启
```

> **注意**：改了 `config.json` 之后必须 `systemctl restart autosync`。独立版的 `serve` 只在启动时读一次配置（和 shell 里的 `reload` 命令不同）。

---

## 8. frp 端口转发

服务端监听的是 `0.0.0.0:8123`，但公网玩家要连的是 frp 服务端上的一个端口。在**运行 AutoSync 的这台机器**上装 frpc：

### 8.1 安装 frpc

```bash
cd /tmp
# 换成 frp 的最新版本号；下面以 0.61.0 为例
FRP_VER=0.61.0
wget "https://github.com/fatedier/frp/releases/download/v${FRP_VER}/frp_${FRP_VER}_linux_amd64.tar.gz"
tar -xzf "frp_${FRP_VER}_linux_amd64.tar.gz"
sudo mv "frp_${FRP_VER}_linux_amd64/frpc" /usr/local/bin/frpc
sudo chmod +x /usr/local/bin/frpc
frpc --version
```

### 8.2 配置 frpc

```bash
sudo mkdir -p /etc/frp
sudo nano /etc/frp/frpc.toml
```

```toml
# frpc 配置：把本机 8123 暴露成 frps 上的 8123
serverAddr = "mc.example.net"
serverPort = 7000
auth.method = "token"
auth.token = "把这里换成你的 frp token"

[[proxies]]
name = "autosync-msfp"
type = "tcp"
localIP = "127.0.0.1"
localPort = 8123
remotePort = 8123
```

> **不要**把 `auth.token` 提交进任何仓库。放 `/etc/frp/frpc.toml` 里并 `sudo chmod 600 /etc/frp/frpc.toml`。

### 8.3 frpc 自启

```bash
sudo nano /etc/systemd/system/frpc.service
```

```ini
[Unit]
Description=frp client
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/frpc -c /etc/frp/frpc.toml
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now frpc
sudo systemctl status frpc --no-pager
```

### 8.4 验证公网可达

```bash
# 从任意一台外网机器
nc -vz mc.example.net 8123
# 期望：succeeded!
```

客户端 `mcpatch.yml` 里就写：

```yaml
urls:
  - mc.example.net:8123
```

> 也可以直接填客户端用的域名，例如 `cn-cd-4.example.net:63358`——只要它最终转发到本机的 `8123`。

### 8.5 云厂商安全组

除了本机 `ufw`，**云控制台的安全组也要放行** `remotePort`（上面例子里的 8123）。这是最常见的"端口本机通、外网不通"的原因。

```bash
# 本机防火墙（如果开了 ufw）
sudo ufw status
# 只放行 frp 的入站端口即可，8123 本身不必对公网开放（frpc 走的是出站连接）
sudo ufw allow 7000/tcp comment 'frp control'   # 仅当 frps 也在本机时才需要
```

---

## 9. 日常更新模组

在**你自己的电脑**上（Windows 或 Linux 都行）用仓库里的上传工具：

```bash
# Linux / macOS
cd /path/to/AutoSync
export AUTOSYNC_HOST=你的服务器地址        # 例如 198.51.100.10
export AUTOSYNC_PORT=22                    # SSH 端口
export AUTOSYNC_USER=ubuntu
export AUTOSYNC_KEY=~/.ssh/id_ed25519
export AUTOSYNC_MODS=/path/to/local/mods
export AUTOSYNC_ROOT=/opt/autosync/repo/server
export AUTOSYNC_PYTHON=python3

pip install paramiko
python tools/deploy.py --dry-run           # 先看会传什么
python tools/deploy.py                     # 上传 + 远端自动 build
```

它会：增量上传变化的 jar → 在远端执行 `python3 -m autosync --base . build` → 打印状态。

加 `--delete` 会顺带删除服务器上多余的 jar（**危险**，确认无误再用；Windows 上的 `tools/upload.bat` 默认就带 `--delete`）。

> `deploy.py` 里**没有任何硬编码的服务器地址或密码**，全部走环境变量或命令行参数，方便你把仓库公开。

---

## 10. 验证清单

上线前逐条过一遍：

```bash
# 1. 版本正确
cd /opt/autosync/repo/server/python && python3 -m autosync --version
#    -> AutoSync 1.0.0

# 2. 清单存在且是合法 JSON
cd /opt/autosync/repo/server && python3 -c "import json;print(len(json.load(open('client-dist/manifest.json'))['files']))"

# 3. 协议自检全过
python3 -m autosync --base . check
#    -> 结果：全部通过

# 4. 服务在跑
sudo systemctl is-active autosync
ss -lntp | grep 8123

# 5. 外网端口通
nc -vz mc.example.net 8123

# 6. 客户端实机验证：改好 mcpatch.yml + -javaagent 参数，启动游戏，看窗口日志
```

---

## 11. 常见坑

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `python3 -m autosync` 报 `No module named autosync` | 不在 `server/python` 目录下 | `cd /opt/autosync/repo/server/python` 再执行，或加 `PYTHONPATH` |
| `status` 显示 0 个文件 | `dist_dir` 配错 / 空目录 | 确认 `WorkingDirectory` 与 `base_dir`，`ls client-dist/mods` 有没有东西 |
| `build` 报 "分发目录为空" | 触发了 `allow_empty_dist=false` 的保护 | 这是**故意的**：检查目录配对了没有，别急着改成 true |
| 端口被占用 | 8123 被别的进程占了 | `ss -lntp \| grep 8123` 找到占用者，或改 `tcp_port`（同时改 frpc 的 `localPort`） |
| 客户端报 `Connection reset` | 服务端没起来 / frpc 没转发 / 安全组没放行 | 按 [常见问题.md](常见问题.md) 第 1 条逐步排查 |
| systemd 启动失败 | `ExecStart` 路径或 `WorkingDirectory` 写错 | `sudo journalctl -u autosync -n 50 --no-pager` 看真实报错 |
| 改了 `config.json` 不生效 | `serve` 只读一次配置 | `sudo systemctl restart autosync` |
| 日志里中文乱码 | 终端 locale | 单元文件里已经有 `NO_COLOR=1`；确认 `locale` 输出是 UTF-8 |
| 想关掉彩色输出 | —— | 环境变量 `NO_COLOR=1`（单元文件已设） |
