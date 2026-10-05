# AutoSync

**AutoSync 是一套 Minecraft 整合包的客户端模组自动同步方案**：服务端扫描一份"要发给玩家的文件"目录生成清单（`manifest.json`），客户端在**启动游戏之前**把本地文件对齐到这份清单（缺的下载、多的清掉、改动过的重下），玩家永远和服务端保持同一套 mod。

由两部分组成：

| 组件 | 位置 | 作用 |
| --- | --- | --- |
| **客户端** | `client/AutoSync-1.0.0.jar` | 以 **javaagent** 方式挂在游戏 JVM 上、或在游戏启动前同步文件的更新器 |
| **服务端** | `server/python/` 或 `server/mcdr/` | 扫描分发目录、生成清单、用裸 TCP 协议把文件发给客户端 |

两边的版本号统一为 **1.0.0**。

---

## 为什么是 javaagent，而不是普通 mod

普通做法是把更新器做成一个 mod 放进 `mods/`。问题在于：**JVM 启动后会把 `mods/` 里的 jar 全部锁住**（Windows 上尤其明显），此时更新器再去替换 `mods/` 里的文件就会失败——文件被占用、删不掉、写不进，只能提示"请重启游戏"，而重启之后又会被同样的问题卡住。

javaagent 是在 **JVM 启动参数阶段**就挂上去的：

```
-javaagent:AutoSync-1.0.0.jar
```

加载顺序上它比游戏主类和 mod 加载器都早，此时**没有任何 mod jar 被打开**，所以：

* 可以自由地新增 / 覆盖 / 删除 `mods/` 下的任何文件，不会被文件锁挡住；
* 同步完成后才继续启动游戏，玩家看到的永远是同步好的 mods；
* 同步失败时可以 **直接中止游戏启动**（`allow-error: false`），避免玩家带着残缺的 mods 进服然后崩在加载界面。

> 顺带一提：这也意味着启动器里必须给这个版本配上 `-javaagent` 参数（见 [docs/客户端配置.md](docs/客户端配置.md)）。

---

## 两条链路

AutoSync 的两条链路是**分开的**，互不干扰：

### 1. 管理链路：SSH 上传 + 重建清单（管理员用）

你在自己电脑上维护一份 `mods/` 目录，用 `tools/deploy.py` 通过 SSH 把它增量推到服务器的 `client-dist/mods/`，然后让服务端重建清单：

```
本地 mods/  --SSH/paramiko-->  服务器 client-dist/mods/  --build-->  manifest.json
```

这条链路只在**你改模组的时候**用，玩家不接触。见 [tools/deploy.py](tools/deploy.py) 与 [docs/部署-Linux.md](docs/部署-Linux.md)。

### 2. 分发链路：MSFP 裸 TCP（玩家用）

玩家启动游戏时，客户端从 `urls` 里配的更新源拉清单和文件：

```
manifest.json + 文件  --MSFP v1（裸 TCP）-->  玩家客户端 mods/
```

**为什么不用 HTTP**：服务端在国内云节点上，未备案域名的 HTTP 流量会被按 Host 头拦截（返回 403 / `Non-compliance ICP Filing`）；裸 TCP 不受影响。所以 AutoSync 直接用原始 TCP 自己定了一个极简协议 **MSFP v1**（`PING` / `SIZE` / `GET <start> <end> <path>`），由 frp 之类的 TCP 隧道暴露到公网。协议细节见 [docs/MSFP协议.md](docs/MSFP协议.md)。

客户端在同步开始时还会做两件事：

* **多源测速**：`urls` 里可以写多个源，客户端并发测速后选最快的（结果缓存 `source-cache-seconds` 秒）；
* **CDN 优先**：清单里的每个文件都带 Modrinth 官方 CDN 直链，客户端会把它和自己的服务端比一次速，按 `source-mode` 决定用谁。

---

## 快速开始

### 服务端（3 步）

**方式 A：独立 Python 版（推荐，不需要 MCDR）**

```bash
# 1. 把仓库放到服务器上
cd /opt && git clone <你的仓库地址> autosync && cd autosync/server/python

# 2. 生成配置（第一次运行会自动创建 config.json）
python3 -m autosync --version          # 应输出：AutoSync 1.0.0

# 3. 放好要分发的文件后启动（常驻）
mkdir -p ../client-dist/mods
cp /path/to/your/mods/*.jar ../client-dist/mods/
python3 -m autosync --base .. serve
```

**方式 B：MCDR 插件版（服务器已经在用 MCDR 时）**

```bash
# 1. 把插件丢进 MCDR 的 plugins/
cp server/mcdr/AutoSync-1.0.0.mcdr /path/to/mcdr/plugins/

# 2. 重载插件（MCDR 控制台）
!!MCDR plugin reload autosync

# 3. 构建清单 + 查看状态（MCDR 控制台）
!!autosync build
!!autosync status
```

### 客户端（3 步）

```bash
# 1. 把 jar 和配置放进版本目录（版本隔离时就是 versions/<版本名>/）
#    client/AutoSync-1.0.0.jar
#    client/mcpatch.yml          <- 把 urls 改成你的更新源

# 2. 启动器里给这个版本加上 JVM 参数
-javaagent:AutoSync-1.0.0.jar

# 3. 启动游戏：同步完成后才会进游戏
```

`mcpatch.yml` 的最小可用内容：

```yaml
urls:
  - mc.example.net:8123
base-path: '.'
allow-error: false
```

---

## 目录结构

```
AutoSync/
├── README.md                     本文件
├── LICENSE                       MIT
├── .gitignore
├── client/
│   ├── AutoSync-1.0.0.jar        客户端 javaagent（javaagent / 独立进程 / modloader 三种启动方式）
│   └── mcpatch.yml               客户端配置模板（含全部配置项与注释）
├── server/
│   ├── python/                   独立 Python 版服务端（纯标准库，Python 3.11+）
│   │   ├── autosync/             程序本体
│   │   ├── config.example.json   配置模板（含全部键）
│   │   ├── requirements.txt      空依赖说明
│   │   └── README.md             本版精简说明
│   └── mcdr/                     MCDR 插件版服务端
│       ├── AutoSync-1.0.0.mcdr   打包好的插件（zip 结构，直接丢 plugins/）
│       ├── autosync/             插件包（entrypoint = autosync.entry）
│       │   ├── entry.py          MCDR 入口：命令树 / 生命周期 / RText 输出
│       │   └── core/             独立版核心代码的同步副本（构建时由 scripts 同步）
│       ├── mcdreforged.plugin.json
│       └── config.example.json
├── docs/
│   ├── 部署-Linux.md             Ubuntu 24 从零部署 + systemd + frp
│   ├── 部署-Windows.md           Windows 部署 + 防火墙 + 两种自启方式
│   ├── 客户端配置.md             mcpatch.yml 每个键的含义/默认值/建议值
│   ├── 服务端-Python版.md        独立版命令表与配置说明
│   ├── 服务端-MCDR版.md          MCDR 版安装、命令表、与独立版的差异
│   ├── 常见问题.md               7 类常见问题的排查步骤
│   └── MSFP协议.md               MSFP v1 协议规范
├── tests/
│   ├── run_tests.py              测试入口（164 项）
│   ├── test_autosync.py          独立版核心测试（148 项）
│   ├── test_mcdr_entry.py        MCDR 入口测试（16 项）
│   └── mcdr_stub/                mcdreforged 测试桩（没装 MCDR 也能跑入口测试）
└── tools/
    ├── deploy.py                 SSH 增量上传 + 远端重建清单
    ├── upload.bat / upload-full.bat   Windows 上的 deploy.py 包装
    ├── build-mcdr.ps1            同步核心代码 + 校验 + 打包 .mcdr
    ├── verify-mcdr-entry.py      MCDR 入口校验（有真 MCDR 用真的，没有用桩）
    └── check-mcdr-package.py     检查 .mcdr 包结构与元数据
```

> **代码复用**：`server/mcdr/autosync/core/` 是 `server/python/autosync/` 的**逐字节副本**，
> 由 `tools/build-mcdr.ps1` 在每次打包前同步并做 SHA-256 比对（不一致就报错中止）。
> 业务逻辑只写一份，MCDR 版只多了一个入口模块 `autosync/entry.py`。

---

## 配置速查表

### 服务端（`config.json`，两边共用同一套键）

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `dist_dir` | `client-dist` | 要分发给客户端的目录（相对 `base_dir`） |
| `base_dir` | `""` | `dist_dir` 的基准目录；空 = 自动（独立版取工作目录，MCDR 版取 `server/`） |
| `tcp_host` | `0.0.0.0` | MSFP 监听地址 |
| `tcp_port` | `8123` | MSFP 监听端口（frp 隧道指向它） |
| `tcp_enabled` | `true` | 是否启用分发服务 |
| `tcp_idle_timeout_seconds` | `30` | 连接空闲多久断开 |
| `auto_build_on_start` | `false` | 启动时是否自动构建一次清单 |
| `poll_interval_seconds` | `0` | 定时检测重建（秒），0 = 关闭 |
| `manifest_name` | `manifest.json` | 清单文件名 |
| `speedtest_enabled` | `true` | 是否生成测速文件 |
| `speedtest_size` | `524288` | 测速文件大小（字节，512 KiB） |
| `exclude_globs` | `manifest.json` 等 | 不纳入分发的文件名（glob） |
| `allow_empty_dist` | `false` | 分发目录扫不到文件时是否仍生成空清单（保持 false，避免误清空客户端） |
| `classify_enabled` | `true` | 是否启用模组分类 |
| `classify_server_mods_dir` | `../mods` | 服务端 mods 目录（相对 `dist_dir`） |
| `classify_move_pure_server` | `false` | 纯服务端模组是否从分发目录**移走**（默认只复制） |
| `deps_check_after_build` | `false` | 构建后是否自动跑一次依赖检查 |
| `deps_fix_enabled` | `true` | 是否允许自动下载缺失前置 |
| `deps_fix_game_version` | `1.21.1` | 目标游戏版本（选前置版本用） |
| `deps_fix_loader` | `neoforge` | 目标加载器 |
| `deps_fix_max_size_mb` | `50` | 单个前置的下载体积上限 |
| `curseforge_api_key` | `""` | CurseForge 官方 API key（留空则用免 key 的 CFWidget 兜底） |
| `http_proxy` | `""` | 出站 HTTP 代理（`http://host:port`） |

完整键与注释见 `server/python/config.example.json`（第一次运行会自动生成一份带注释的 `config.json`）。

### 客户端（`mcpatch.yml`）

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `urls` | — | 更新源列表（`主机:端口`），会并发测速选最快 |
| `base-path` | `""` | 更新起始目录；版本隔离必须写 `'.'` |
| `allow-error` | `false` | 同步失败时是否继续启动游戏 |
| `mirror-mode` | `false` | **镜像模式**：清单外的文件是否清掉 |
| `mirror-backup` | `true` | 清理时先备份到 `.modsync-removed/` |
| `source-mode` | `auto` | `auto` / `cdn` / `server` 三选一 |
| `concurrent-files` | `2` | 同时下载的文件数 |
| `download-threads` | `32` | 单文件分块线程数 |
| `tcp-timeout` | `15000` | MSFP 连接/读超时（毫秒） |
| `retries` | `3` | 网络重试次数 |
| `auto-select-source` | `true` | 是否自动测速选源 |
| `source-cache-seconds` | `3600` | 测速结果缓存时间 |
| `detect-mod-conflicts` | `true` | 自动禁用与服务器模组 modId 冲突的玩家自加模组 |

**每个键的完整含义、默认值、建议值见 [docs/客户端配置.md](docs/客户端配置.md)。**

---

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [部署-Linux.md](docs/部署-Linux.md) | Ubuntu 24 从零部署：Python 环境、解压、配置、systemd 开机自启、frp 端口转发、验证命令、常见坑 |
| [部署-Windows.md](docs/部署-Windows.md) | Windows 上跑服务端：Python 安装、启动、防火墙放行、任务计划程序 / NSSM 两种自启方式 |
| [客户端配置.md](docs/客户端配置.md) | `mcpatch.yml` 每个键的含义 / 默认值 / 建议值，重点讲镜像模式、来源模式与连接数 |
| [服务端-Python版.md](docs/服务端-Python版.md) | 独立版安装步骤 + 完整命令表 + 配置差异 |
| [服务端-MCDR版.md](docs/服务端-MCDR版.md) | MCDR 版安装步骤 + 完整命令表 + 与独立版的差异 |
| [常见问题.md](docs/常见问题.md) | Connection reset、缺前置崩溃、下载慢、玩家 mod 被清、镜像误删恢复、CDN 流量代价、Java 版本 |
| [MSFP协议.md](docs/MSFP协议.md) | MSFP v1 协议规范（给二次开发者） |

---

## 环境要求

| 组件 | 要求 |
| --- | --- |
| 服务端 Python | Python **3.11+**（在 3.12 / 3.14 上实测通过），**纯标准库**，无第三方依赖 |
| 服务端操作系统 | Linux（推荐 Ubuntu 24.04）或 Windows 10/11 |
| MCDR 版 | MCDR **2.10+**（在 2.16.0 上实测通过） |
| 客户端 Java | **Java 17 或更高**（编译用 JDK 17，运行推荐 JRE 21） |
| 客户端游戏版本 | 需要 javaagent 支持的环境；**1.16 及以下不支持** |

---

## 测试与自检

```bash
# 全部测试（独立版核心 148 项 + MCDR 入口 16 项，共 164 项，仅需标准库）
python tests/run_tests.py

# 服务端协议自检：起一个临时 MSFP 服务，跑 PING / SIZE / GET / 分块 / 并发 / 错误码
cd server/python && python3 -m autosync --base .. check

# MCDR 入口校验（装了 mcdreforged 就用真的，否则用 tests/mcdr_stub 桩）
python tools/verify-mcdr-entry.py

# 检查 .mcdr 包结构与元数据
python tools/check-mcdr-package.py
```

---

## 许可证

[MIT](LICENSE)
