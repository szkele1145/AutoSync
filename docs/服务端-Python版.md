# 服务端 · 独立 Python 版

`server/python/` 是**独立运行**的服务端：**纯 Python 标准库**，不依赖 MCDR、不依赖 Minecraft 服务端进程，可以单独跑在一台机器上。

| 项 | 值 |
| --- | --- |
| 版本 | `1.0.0` |
| 运行环境 | Python **3.11+**（在 3.12 / 3.14 上实测通过） |
| 第三方依赖 | **无**（`requirements.txt` 里只有说明） |
| 支持系统 | Linux（推荐 Ubuntu 24.04）/ Windows 10+ |

---

## 1. 目录结构

```
server/python/
├── autosync/                程序本体（包名不能改）
│   ├── __init__.py          版本号
│   ├── __main__.py          命令行入口
│   ├── config.py            配置对象与默认值
│   ├── scanner.py           目录扫描 + SHA-256 / SHA-1
│   ├── manifest.py          清单结构 / 内容指纹 / 原子写入
│   ├── modrinth.py          Modrinth API 查询
│   ├── curseforge.py        CurseForge 官方 API / CFWidget
│   ├── speedtest.py         512KB 测速文件生成
│   ├── tcp_server.py        MSFP v1 服务端
│   ├── tcp_client.py        MSFP v1 客户端（自检用）
│   ├── side_report.py       ModSideDetector 上报接收（MSFP REPORT 命令，默认关闭）
│   ├── builder.py           构建编排 / 状态缓存 / 定时轮询
│   ├── theme.py             QBM 风格输出主题
│   ├── classify.py          模组分类与搬运
│   ├── deps.py              依赖检查（含 JiJ 嵌套层）
│   ├── deps_fix.py          缺失前置自动下载
│   └── shell.py             交互式 REPL
├── config.example.json      配置模板（含全部键与注释）
└── requirements.txt         空依赖说明
```

---

## 2. 安装

```bash
# 1. 拿到代码（或直接把整个 Autosync 目录复制过去）
cd /opt && git clone <你的仓库地址> autosync
cd /opt/autosync/server/python

# 2. 验证
python3 -m autosync --version
# 期望：AutoSync 1.0.0
```

不需要 `pip install` 任何东西。

> 详细的系统级部署（systemd / frp / 防火墙）见 [部署-Linux.md](部署-Linux.md) 与 [部署-Windows.md](部署-Windows.md)。

---

## 3. 配置

配置文件默认是**当前工作目录**下的 `config.json`；**不存在时会自动生成一份默认配置**（含全部键与中文注释）。也可以用 `--config <路径>` 指定别处。

```bash
cd /opt/autosync/server/python
python3 -m autosync --base .. status     # 第一次运行顺手生成 config.json
```

生成后编辑 `config.json`，最常改的几个：

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `dist_dir` | `client-dist` | 要分发给客户端的目录 |
| `base_dir` | `""` | `dist_dir` 的基准；空 = 当前工作目录（systemd 里由 `WorkingDirectory` 决定） |
| `tcp_host` / `tcp_port` | `0.0.0.0` / `8123` | MSFP 监听地址与端口 |
| `tcp_enabled` | `true` | 是否启用分发服务 |
| `speedtest_enabled` / `speedtest_size` | `true` / `524288` | 客户端多源测速用的 512KB 文件 |
| `poll_interval_seconds` | `0` | 定时检测重建（秒），0 = 关闭 |
| `auto_build_on_start` | `false` | 启动时自动构建一次 |
| `allow_empty_dist` | `false` | 目录为空时是否仍生成空清单；**保持 false** 防止误清空客户端 |
| `classify_server_mods_dir` | `../mods` | 服务端 mods 目录（相对 `dist_dir`） |
| `classify_side_report` | `""` | ModSideDetector 的 `side-report.json`：空 = 自动探测 `<dist_dir>/mods/side-report.json`；`none`/`off`/`disabled` = 关闭该功能；相对路径相对 `dist_dir` 解析 |
| `side_report_enabled` | `false` | 是否允许配套工具通过 MSFP 的 **`REPORT`** 命令网络上报 `side-report.json`（**默认关**，不新开端口，与分发共用 `tcp_host`/`tcp_port`） |
| `side_report_token` | `""` | 上报共享令牌（单行、不含空格）。**开关打开但留空时，`REPORT` 一律回 `ERR disabled`** |
| `side_report_path` | `""` | 上报报告的落盘路径：空 = `<data_dir>/side-report.json`；相对路径相对 `data_dir` 解析 |
| `deps_fix_game_version` / `deps_fix_loader` | `1.21.1` / `neoforge` | 自动补前置时筛版本 |
| `curseforge_api_key` | `""` | 留空则用免 key 的 CFWidget 兜底 |
| `http_proxy` | `""` | 出站 HTTP 代理（`http://host:port`） |

**完整键列表与逐项注释**见 `config.example.json`。

---

## 4. 完整命令表

`--base <目录>` 指定 `dist_dir` 相对路径的基准目录；不写就按配置里的 `base_dir` 解析，再退回当前目录。

### 4.1 常用动作

| 命令 | 作用 |
| --- | --- |
| `python3 -m autosync --base <目录> shell` | **交互式 REPL**（推荐）：启动即拉起 MSFP，然后进 `AutoSync> ` 提示符 |
| `python3 -m autosync --base <目录> serve` | 只跑 MSFP 服务（常驻，`Ctrl+C` 退出） |
| `python3 -m autosync --base <目录> build` | 只构建清单后退出 |
| `python3 -m autosync --base <目录> status` | 查看文件数 / 清单版本 / 端口与连接统计 |
| `python3 -m autosync --base <目录> check` | **协议自检**：临时起服务，跑 PING / SIZE / GET / 分块 / 并发 / 错误码 |
| `python3 -m autosync --version` | 打印版本号 |

### 4.2 模组分类

| 命令 | 作用 |
| --- | --- |
| `... classify [--refresh]` | **干跑**：分析 `client-dist/mods` 里哪些是纯客户端 / 双端 / 纯服务端 / 待定，**不改动任何文件** |
| `... classify-apply` | 按上一次 classify 的结果搬运（双端复制；纯服务端默认也只复制） |

> **优先采用 ModSideDetector 的判定**：配套工具 ModSideDetector 跑完会导出 `side-report.json`。
> 把它放到 `<dist_dir>/mods/side-report.json`（或用 `classify_side_report` 指定路径），`classify` 就会
> **优先**读它（先按 **sha1** 精确匹配、sha1 缺失时按文件名匹配），命中即采用它的
> `client`/`server`/`both`/`unknown` 结论，并**跳过 Modrinth 查询**（省请求、也比 mod 作者自填的
> `client_side` 准）。报告里会带上原始的 `confidence` 与 `notes`，`low` 置信或标了
> `needs_review`/`conflict` 的条目仍然采用、但标注「建议人工复核」。
> 文件不存在、损坏或字段不合法时会被忽略并记一条 warning，行为与没有这个功能时完全一致；
> 未命中的 mod 照旧走 Modrinth -> TOML 兜底。

#### 4.2.1 让 ModSideDetector 直接网络上报（可选，默认关闭）

工具在另一台机器上时，手工拷贝 `side-report.json` 很麻烦。打开 `side_report_enabled` 后，
ModSideDetector 可以**复用同一个 MSFP 端口**（`tcp_host`/`tcp_port`，不需要额外放行端口，
也不引入 HTTP —— 国内节点的 HTTP 会被备案拦截，裸 TCP 不受影响）用 `REPORT` 命令把报告推上来：

```
REPORT <token> <length>\n
<length 字节的 UTF-8 JSON（side-report.json 全文）>

-> OK 5\n                 成功，数字 = 报告里 mods 的条目数
-> ERR unauthorized\n     令牌不匹配
-> ERR disabled\n         功能关闭，或没配 side_report_token
-> ERR too large\n        超过 32 MB 上限
-> ERR bad request\n      格式错误 / JSON 不合法 / mods 不是数组
-> ERR internal <xxx>\n   落盘失败
```

三步用起来：

1. 在 `config.json` 里设 `"side_report_enabled": true` 和 `"side_report_token": "自己挑的长随机串"`，
   然后 `reload`（MCDR）或重启（独立版）。
2. 工具端按上面的报文发一次；报告会**原子写入** `side_report_path`
   （默认 `<data_dir>/side-report.json`，独立版 `data_dir` 就是 `--data` 指定的目录，
   例如 `./autosync-data/`）。
3. 跑 `classify`：`classify_side_report` 留空（自动探测）时，若 `<dist_dir>/mods/side-report.json`
   不存在，就会自动读上报来的那份 —— **上报完不需要手动改任何配置**。

> 安全默认：`side_report_enabled` 默认 `false`；即使打开，**`side_report_token` 为空时
> `REPORT` 一律回 `ERR disabled`**（绝不允许「没配令牌就能往磁盘写文件」）。
> 令牌用 `hmac.compare_digest` 比较，避免时序侧信道；报告只做最小校验（顶层是对象、`mods` 是数组），
> 校验不过**不落盘**。收到报告**不会自动跑 classify**，只写一条日志提示下次 classify 会用它。
>
> 与 `classify_side_report` 的关系：显式配置过 `classify_side_report` 时**只认它**，
> 上报路径只作为「自动探测」时的回退，绝不覆盖用户配置。

### 4.3 依赖检查与自动补前置

| 命令 | 作用 |
| --- | --- |
| `... deps` | **只读**：扫描 jar 元数据（含 JiJ 嵌套层），列出真缺失前置 / JiJ 已提供 / 无法解析 + 循环依赖 + 版本不匹配 |
| `... deps-fix` | 列出缺失前置的**编号清单与候选版本**（a/b/c），不下载任何文件 |
| `... deps-fix 1a 2a` | 只下载编号 `1a`、`2a` 这两项 |
| `... deps-fix apply` | 全选推荐候选（每项取 a；只有预发布版时取 b/c）并真正下载到 `<dist_dir>/mods/` |

> 下载源策略：**Modrinth 优先**；定位失败时兜底 CurseForge（配置了 `curseforge_api_key` 就走官方 API，否则用免 key 的第三方源 CFWidget）。
> 会做哈希校验；**同名不同内容绝不覆盖**；清单缓存 30 分钟，过期请重新运行 `deps-fix`。

### 4.4 全局参数

| 参数 | 说明 |
| --- | --- |
| `--base <目录>` | `dist_dir` 相对路径的基准目录 |
| `--data <目录>` | 状态 / 缓存目录（默认 `./autosync-data`） |
| `--config <路径>` | JSON 配置文件路径（默认 `./config.json`，不存在则自动生成） |
| `--dist <目录>` | 覆盖配置里的 `dist_dir` |
| `--port <端口>` | 覆盖配置里的 `tcp_port`（`check` 默认 `0` = 随机端口） |
| `--connections <N>` | `check` 的并发连接数（默认 8） |
| `--refresh` | 忽略 Modrinth 缓存强制重查 |
| `--json` | 以 JSON 输出结果（便于脚本处理） |
| `--quiet` | 只输出错误日志 |

### 4.5 交互式 shell 的内部命令

进入 `shell` 后，命令**不带前缀**：

```
status                 查看文件数 / 清单版本 / MSFP 端口与连接统计
build                  重建客户端清单与测速文件
build refresh          忽略 Modrinth 缓存强制重查后重建
reload                 重载 config.json 并重启 MSFP 服务与定时轮询
tcp start|stop|restart 控制 MSFP（裸 TCP）分发服务
classify               干跑分类（只读）
classify apply         按上次结果搬运
deps                   只读依赖检查
deps fix               列出缺失前置编号清单（不下载）
deps fix 1a 2a         只下载指定编号
deps fix apply         全选推荐候选并下载
help                   查看全部命令
exit | quit            退出（MSFP 服务一起停掉）
```

---

## 5. 典型工作流

```bash
# ---- 一次性准备 ----
cd /opt/autosync/server
mkdir -p client-dist/mods
# 把要发的 mod 放进 client-dist/mods（或从 mods/ 建硬链接）
python3 -m autosync --base . build          # 生成 manifest.json + speedtest.bin
python3 -m autosync --base . check          # 确认协议与文件都对

# ---- 常驻运行 ----
python3 -m autosync --base . serve          # 或用 shell 进交互模式

# ---- 日常改模组 ----
# 在本地用 tools/deploy.py 上传 -> 远端自动 build
python3 tools/deploy.py --host <服务器地址> --user <用户> --mods <本地mods>

# ---- 手工检查依赖 ----
python3 -m autosync --base . deps           # 看有没有缺前置
python3 -m autosync --base . deps-fix       # 看候选版本
python3 -m autosync --base . deps-fix apply # 一键补齐
```

---

## 6. 产出物

| 路径 | 说明 |
| --- | --- |
| `<dist_dir>/manifest.json` | 客户端要拉取的清单（含每个文件的 path / size / sha256 / urls） |
| `<dist_dir>/speedtest.bin` | 512KB 测速文件，客户端多源测速用 |
| `./autosync-data/state.json` | 上一次构建的文件快照、Modrinth 查询缓存等 |
| `./autosync-data/*.json` | `deps` / `classify` 等命令的 JSON 报告 |
| `./config.json` | 配置文件（首次运行自动生成） |

**不要**把这些提交进 git：`autosync-data/`、`config.json`、`client-dist/`（`.gitignore` 里已经排除）。

---

## 7. 与 MCDR 版的差异

| 项 | 独立 Python 版 | [MCDR 版](服务端-MCDR版.md) |
| --- | --- | --- |
| 依赖 | 只要 Python 3.11+ | 需要 MCDR 2.10+ |
| 操作方式 | 命令行 / 交互式 shell | 游戏内 `!!autosync` 命令 |
| 配置文件位置 | 工作目录下的 `config.json` | `config/autosync/config.json` |
| 数据目录 | 工作目录下的 `autosync-data/` | `config/autosync/autosync-data/` |
| `base_dir` 为空时 | 取**当前工作目录** | 取 MCDR 的 `working_directory`（一般是 `server/`） |
| 核心业务代码 | `autosync/` 本体 | 同一份代码的副本（`autosync/core/`） |
| 命令名 | `build` / `classify-apply` / `deps-fix` | `!!autosync build` / `classify apply` / `deps fix` |
| 适用场景 | 独立部署、无 MCDR、给其它机器分发 | 已经在用 MCDR 管服的场景 |

两边**读写的清单格式、MSFP 协议、配置键完全一致**，可以随时互换。
