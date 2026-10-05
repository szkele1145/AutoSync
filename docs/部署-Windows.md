# 部署到 Windows

本文档在一台 **Windows 10 / 11** 上部署 AutoSync 独立 Python 版服务端，并给出两种开机自启方式（任务计划程序 / NSSM）。

如果你服务器跑的是 MCDR，请直接看 [服务端-MCDR版.md](服务端-MCDR版.md)——那种情况下不需要本文档。

---

## 0. 前置条件

| 项 | 要求 |
| --- | --- |
| 系统 | Windows 10 / 11 或 Windows Server 2019+ |
| Python | 3.11 及以上（推荐 3.12 / 3.14），**纯标准库**，无需 pip 装包 |
| 权限 | 安装 Python 需要管理员；之后运行不需要 |
| 端口 | 规划好 MSFP 端口（默认 `8123`），并确认防火墙 / 云安全组放行 |

---

## 1. 安装 Python

### 1.1 官方安装包（推荐）

1. 打开 <https://www.python.org/downloads/windows/>，下载 **Windows installer (64-bit)** 的 3.12 或更新版本；
2. 运行安装包，**务必勾选 `Add python.exe to PATH`**，然后点 `Install Now`；
3. 安装完成后开一个新的 PowerShell 窗口验证：

```powershell
python --version
# 期望：Python 3.12.x（或更高）
```

如果 `python --version` 打开的是微软商店：
**设置 → 应用 → 高级应用设置 → 应用执行别名**，把 `python.exe` / `python3.exe` 两个别名关掉，再重新验证。

### 1.2 免安装的绿色版（pythoncore 发行版）

如果不想装到系统里，可以下载 `pythoncore` 这类免安装发行版，解压后直接用完整路径调用，例如：

```powershell
& "$env:LOCALAPPDATA\Python\pythoncore-3.14-64\python.exe" --version
```

后面所有命令把 `python` 换成这个完整路径即可（自启脚本里同理）。

---

## 2. 放置代码

```powershell
# 假设放到 C:\AutoSync（路径不要带中文和空格，省掉一堆转义麻烦）
New-Item -ItemType Directory -Force -Path C:\AutoSync | Out-Null
cd C:\AutoSync

# 从 git 克隆（已装 Git 的话）
git clone <你的仓库地址> repo
# 或者：把 AutoSync 目录直接复制成 C:\AutoSync\repo
```

验证：

```powershell
cd C:\AutoSync\repo\server\python
python -m autosync --version
# 期望输出：AutoSync 1.0.0
```

> 如果中文输出变成乱码，先执行 `chcp 65001`，或设置 `$env:PYTHONIOENCODING="utf-8"`。

---

## 3. 准备分发目录

```powershell
cd C:\AutoSync\repo\server
New-Item -ItemType Directory -Force -Path client-dist\mods | Out-Null

# 把要发给客户端的模组复制进去（服务端 mods 里那些纯服务端模组不要复制）
Copy-Item .\mods\*.jar .\client-dist\mods\ -Force
```

> **硬链接省空间**：Windows 上可以用 `mklink /H` 建硬链接，让 `client-dist\mods` 和服务端 `mods` 指向同一份数据（同一磁盘分区内才行）：
> ```cmd
> cd /d C:\AutoSync\repo\server
> for %f in (mods\*.jar) do mklink /H "client-dist\mods\%~nxf" "%f"
> ```
> 注意：硬链接下改了其中一份，另一份也会变，这正是我们要的效果。

---

## 4. 生成并修改配置

```powershell
cd C:\AutoSync\repo\server
python -m autosync --base . status
```

第一次运行会在当前目录生成 `config.json`。用记事本或 VS Code 打开，确认/修改：

| 键 | 建议值 | 说明 |
| --- | --- | --- |
| `dist_dir` | `"client-dist"` | 要分发的目录 |
| `base_dir` | `""` | 空 = 相对当前工作目录解析 |
| `tcp_host` | `"0.0.0.0"` | 接受来自隧道 / 局域网的连接 |
| `tcp_port` | `8123` | MSFP 端口 |
| `tcp_enabled` | `true` | 关掉就不分发 |
| `speedtest_enabled` | `true` | 客户端靠它多源测速 |
| `poll_interval_seconds` | `0` | 关闭定时检测（手动 build 更可控） |
| `allow_empty_dist` | `false` | **保持 false**，避免目录配错时生成空清单清空客户端 |
| `classify_server_mods_dir` | `"../mods"` | 分类时服务端 mods 的位置 |
| `deps_fix_game_version` / `deps_fix_loader` | 你的版本 / 加载器 | 自动补前置时筛版本用 |

改完保存。

---

## 5. 构建清单并自检

```powershell
cd C:\AutoSync\repo\server
python -m autosync --base . build
python -m autosync --base . check
```

`check` 会临时起一个 MSFP 服务，跑 PING / SIZE / GET / 分块 / 并发 / 路径逃逸拦截，最后一行应是：

```
结果：全部通过
```

---

## 6. 启动服务

### 6.1 前台运行（先验证）

```powershell
cd C:\AutoSync\repo\server
python -m autosync --base . serve
```

期望：

```
  ✔ MSFP    服务运行中：0.0.0.0:8123（协议 MSFP v1）· 目录 C:\AutoSync\repo\server\client-dist（Ctrl+C 退出）
```

`Ctrl+C` 停止。

### 6.2 交互式 shell

```powershell
cd C:\AutoSync\repo\server
python -m autosync --base . shell
```

进入 `AutoSync> ` 提示符后可以敲 `status` / `build` / `reload` / `deps fix` 等命令（**不带前缀**），`help` 看全部命令，`exit` 退出。

---

## 7. 防火墙与端口

### 7.1 本机防火墙放行

MSFP 端口要能被隧道 / 外网访问。**以管理员身份**打开 PowerShell：

```powershell
New-NetFirewallRule -DisplayName "AutoSync MSFP 8123" `
    -Direction Inbound -Protocol TCP -LocalPort 8123 -Action Allow
```

查看 / 删除规则：

```powershell
Get-NetFirewallRule -DisplayName "AutoSync MSFP 8123" | Format-Table DisplayName, Enabled, Direction, Action
Remove-NetFirewallRule -DisplayName "AutoSync MSFP 8123"   # 不再需要时删除
```

> **更安全的做法**：如果客户端是通过本机 frpc 隧道进来的（`localIP = 127.0.0.1`），那 8123 **完全不需要**对公网开放，只留 `127.0.0.1` 监听即可：
> ```powershell
> # config.json 里把 tcp_host 改成 "127.0.0.1"，然后就不用加防火墙规则了
> ```

### 7.2 云服务器安全组

如果这台机器在云上（阿里云 / 腾讯云等），还要在控制台的**安全组**里放行对外的那个端口（frps 的 `remotePort`）。这是"本机通、外网不通"的最常见原因。

### 7.3 验证端口

```powershell
# 本机看监听
Get-NetTCPConnection -LocalPort 8123 -State Listen | Format-Table LocalAddress, LocalPort, OwningProcess

# 从另一台机器测试（Windows 10+ 自带 Test-NetConnection）
Test-NetConnection mc.example.net -Port 8123
# 期望：TcpTestSucceeded : True
```

### 7.4 frp 隧道

和 Linux 完全一样，只是路径不同：下载 `frp_*_windows_amd64.zip`，解压后编辑 `frpc.toml`：

```toml
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

前台试跑：

```powershell
.\frpc.exe -c .\frpc.toml
```

能通就把它做成自启（见下面 NSSM 一节，`frpc.exe` 同样适合用 NSSM 托管）。

---

## 8. 开机自启

两种方式任选其一。**方式 A 不用装任何软件，推荐先试它。**

### 方式 A：任务计划程序（Task Scheduler）

思路：开机时用 `python.exe` 启动 `-m autosync --base . serve`，工作目录设成 `server` 目录。

**A-1. 先写一个启动脚本**（把路径换成你的实际路径）：

```powershell
New-Item -ItemType Directory -Force -Path C:\AutoSync | Out-Null
@'
@echo off
chcp 65001 >nul
cd /d C:\AutoSync\repo\server
set NO_COLOR=1
set PYTHONUNBUFFERED=1
"C:\Users\你的用户名\AppData\Local\Programs\Python\Python312\python.exe" -m autosync --base . serve >> C:\AutoSync\autosync.log 2>&1
'@ | Set-Content -Encoding ASCII C:\AutoSync\start-autosync.bat
```

> 把里面的 Python 路径换成 `where python` 输出的真实路径。

**A-2. 注册计划任务**（管理员 PowerShell，一次性）：

```powershell
$action  = New-ScheduledTaskAction -Execute "C:\AutoSync\start-autosync.bat"
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName "AutoSync" -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Description "AutoSync MSFP mod distribution server"
```

> `-ExecutionTimeLimit (New-TimeSpan -Days 0)` 表示**不限制运行时长**（默认为 3 天后强杀），长跑服务必须设。

**A-3. 验证与常用操作**：

```powershell
Start-ScheduledTask -TaskName "AutoSync"                 # 立即启动一次
Get-ScheduledTask -TaskName "AutoSync" | Format-List State, TaskName
Get-ScheduledTaskInfo -TaskName "AutoSync" | Format-List LastRunTime, LastTaskResult
Stop-ScheduledTask -TaskName "AutoSync"                  # 停止
Unregister-ScheduledTask -TaskName "AutoSync" -Confirm:$false   # 删除
Get-Content C:\AutoSync\autosync.log -Tail 30 -Encoding UTF8    # 看日志
```

**方式 A 的注意点**：

* 用 `SYSTEM` 身份运行时，`C:\AutoSync\repo\server` 必须有读权限（一般没问题）；
* 日志重定向到 `C:\AutoSync\autosync.log`，排查问题先看它；
* 改了 `config.json` 后要 `Stop-ScheduledTask` + `Start-ScheduledTask` 重启。

### 方式 B：NSSM（把程序注册成 Windows 服务）

NSSM 能提供标准的服务管理（自动重启、崩溃拉起、`services.msc` 里可见）。

**B-1. 下载 NSSM**

打开 <https://nssm.cc/download>，下载 `nssm-2.24.zip`，解压后把 `win64\nssm.exe` 放到 `C:\AutoSync\nssm.exe`（或任意目录）。

**B-2. 安装服务**（管理员 PowerShell）：

```powershell
# 注意：NSSM 的 Application 必须填 python.exe，参数分开写
$python = (Get-Command python).Source
Write-Host "python: $python"

C:\AutoSync\nssm.exe install AutoSync "$python"
C:\AutoSync\nssm.exe set AutoSync AppParameters "-m autosync --base . serve"
C:\AutoSync\nssm.exe set AutoSync AppDirectory "C:\AutoSync\repo\server"
C:\AutoSync\nssm.exe set AutoSync DisplayName "AutoSync MSFP server"
C:\AutoSync\nssm.exe set AutoSync Description "AutoSync client mod distribution (MSFP raw TCP)"
C:\AutoSync\nssm.exe set AutoSync Start SERVICE_AUTO_START
C:\AutoSync\nssm.exe set AutoSync AppStdout "C:\AutoSync\autosync.out.log"
C:\AutoSync\nssm.exe set AutoSync AppStderr "C:\AutoSync\autosync.err.log"
C:\AutoSync\nssm.exe set AutoSync AppRotateFiles 1
C:\AutoSync\nssm.exe set AutoSync AppRotateBytes 10485760
C:\AutoSync\nssm.exe set AutoSync AppEnvironmentExtra NO_COLOR=1 PYTHONUNBUFFERED=1
C:\AutoSync\nssm.exe set AutoSync AppExit Default Restart
C:\AutoSync\nssm.exe set AutoSync AppRestartDelay 5000
```

**B-3. 启动与验证**：

```powershell
Start-Service AutoSync
Get-Service AutoSync | Format-List Name, Status, StartType
Get-Content C:\AutoSync\autosync.out.log -Tail 20 -Encoding UTF8
```

常用操作：

```powershell
Restart-Service AutoSync     # 改了 config.json 后重启
Stop-Service AutoSync
C:\AutoSync\nssm.exe remove AutoSync confirm    # 卸载服务
```

---

## 9. 日常更新模组

双击仓库里的 `tools\upload.bat`（增量，会删除服务器上多余的 jar）或 `tools\upload-full.bat`（全量，有二次确认）。

第一次用之前，用**系统环境变量**或同目录的 `set` 命令配好：

```cmd
set AUTOSYNC_HOST=198.51.100.10
set AUTOSYNC_PORT=22
set AUTOSYNC_USER=ubuntu
set AUTOSYNC_KEY=C:\Users\你的用户名\.ssh\id_ed25519
set AUTOSYNC_MODS=C:\AutoSync\repo\mods
set AUTOSYNC_ROOT=/opt/autosync/repo/server
set AUTOSYNC_PYTHON=python3
```

也可以直接命令行调用，先用 `--dry-run` 看会传什么：

```powershell
python tools\deploy.py --dry-run --host 198.51.100.10 --user ubuntu --mods C:\AutoSync\repo\mods
```

需要先 `pip install paramiko`。`deploy.py` 里**不存任何服务器地址或密码**。

---

## 10. 常见坑（Windows 特有）

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `python` 打开微软商店 | 应用执行别名没关 | 设置 → 应用 → 高级应用设置 → 应用执行别名，关掉 `python.exe` / `python3.exe` |
| 中文日志乱码 | 控制台代码页不是 UTF-8 | `chcp 65001`；或设 `$env:PYTHONIOENCODING="utf-8"` |
| 计划任务启动后立刻退出 | 脚本里 Python 路径写错 / 工作目录不对 | 先手动双击 `start-autosync.bat` 看报错，看 `autosync.log` |
| 计划任务跑 3 天后被杀 | 默认 `ExecutionTimeLimit` 是 3 天 | 注册时加 `-ExecutionTimeLimit (New-TimeSpan -Days 0)` |
| 外网连不上，本机 `Test-NetConnection` 通 | 云安全组没放行 | 控制台安全组放行 `remotePort` |
| `build` 报"分发目录为空" | `dist_dir` 配错（这是保护机制） | 检查 `config.json` 与工作目录，不要急着开 `allow_empty_dist` |
| 端口被占用 | 8123 被别的程序占了 | `Get-NetTCPConnection -LocalPort 8123` 找到进程，改 `tcp_port` 或停掉占用者 |
| 杀软误报 / 拦截 | 服务监听端口触发行为告警 | 给 `python.exe` 与 `C:\AutoSync` 加白名单 |
| 路径含中文导致异常 | 少见的编码问题 | 部署路径统一用纯 ASCII（如 `C:\AutoSync`） |
