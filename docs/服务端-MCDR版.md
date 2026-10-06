# 服务端 · MCDR 版

`server/mcdr/AutoSync-1.0.0.mcdr` 是给 **MCDReforged（MCDR）** 用的插件包：服务器已经在用 MCDR 管服时，装上它就能用 `!!autosync` 完成构建清单、分发文件、模组分类、依赖修复的全部操作。

| 项 | 值 |
| --- | --- |
| 插件 id | `autosync` |
| 版本 | `1.0.0` |
| 依赖 | MCDR **2.10+**（在 2.16.0 上实测通过） |
| 入口点 | `autosync.entry` |
| 安装形态 | 单文件 `.mcdr`（本质是 zip） |

---

## 1. 安装

### 1.1 三步安装

```bash
# 1. 把插件放进 MCDR 的 plugins 目录
cp server/mcdr/AutoSync-1.0.0.mcdr /path/to/mcdr/plugins/

# 2. 让 MCDR 加载（旧版 MCDR 用 !!MCDR reload plugin，或直接重启 MCDR）
!!MCDR plugin load autosync

# 3. 在 MCDR 控制台里执行（控制台命令不带 !! 前缀也认）
autosync help
autosync status
```

> 控制台里可以直接输 `autosync build`（不带 `!!`），游戏内必须写 `!!autosync build`。

### 1.2 验证加载成功

看 MCDR 日志，应该出现：

```
Plugin autosync@1.0.0 loaded
```

以及插件自己打的：

```
AutoSync 正在加载 v1.0.0 ...
AutoSync 已就绪：分发目录 .../server/client-dist · 配置 config/autosync/config.json
AutoSync ✔ MSFP    服务运行中：0.0.0.0:8123（协议 MSFP v1）· 分发目录 ...
```

如果看到 `Fail to load plugin ... autosync-1.0.0.mcdr`，参考 [常见问题.md](常见问题.md) 与本文第 6 节。

---

## 2. 配置文件在哪里

| 项 | 位置 |
| --- | --- |
| 配置 | `<MCDR 根目录>/config/autosync/config.json` |
| 数据 / 缓存 / 报告 | `<MCDR 根目录>/config/autosync/autosync-data/` |
| 分发目录（默认） | `<MCDR 的 working_directory>/client-dist`（一般是 `server/client-dist`） |

* **第一次加载会自动生成** `config/autosync/config.json`（含全部键与中文注释）。
* 兼容旧版：如果 MCDR 根目录下已经有一个 `config.json` 而 `config/autosync/config.json` 还不存在，插件会**沿用根目录那份**（方便从 ModSync 0.0.x 升级）。
* 改完配置后执行 `!!autosync reload` 生效（不用重启 MCDR）。

配置键与独立版**完全一致**，见 [服务端-Python版.md](服务端-Python版.md#3-配置) 与 `server/mcdr/config.example.json`。两边仅有两处默认行为不同：

| 键 | 独立版 | MCDR 版 |
| --- | --- | --- |
| `base_dir: ""` | `dist_dir` 相对**当前工作目录**解析 | `dist_dir` 相对 MCDR 的 `working_directory`（通常是 `server/`）解析 |
| `base_dir: "mcdr_root"` | 等价于当前工作目录 | 等价于 MCDR 根目录 |

---

## 3. 完整命令表

游戏内前缀 `!!autosync`（`!!as` 是等价别名）。控制台里可以直接省略 `!!`。

| 命令 | 权限 | 作用 |
| --- | --- | --- |
| `!!autosync` / `!!autosync help` | 所有人 | 显示帮助（**每条命令都是可点按钮**，点击后按回车执行） |
| `!!autosync status` | 所有人 | 文件数 / 清单版本 / MSFP 端口与连接统计 + 快捷按钮 |
| `!!autosync build` | 等级 2 | 重建清单与测速文件（后台线程，会重写 `manifest.json`） |
| `!!autosync build refresh` | 等级 2 | 忽略 Modrinth 缓存强制重查后重建（更慢、请求更多） |
| `!!autosync reload` | 等级 2 | 重载 `config.json`，重建核心并重启 MSFP 与定时轮询 |
| `!!autosync tcp start` | 等级 2 | 启动 MSFP（裸 TCP）分发服务 |
| `!!autosync tcp stop` | 等级 2 | 停止 MSFP 服务 |
| `!!autosync tcp restart` | 等级 2 | 重启 MSFP 服务 |
| `!!autosync classify` | 所有人 | **干跑**分类：纯客户端 / 双端 / 纯服务端 / 待定（不改动任何文件） |
| `!!autosync classify apply` | 等级 2 | 按上次结果搬运（双端复制；纯服务端默认也只复制） |

> `classify` 会**优先**采用配套工具 ModSideDetector 导出的 `side-report.json`
> （默认自动探测 `<dist_dir>/mods/side-report.json`，可用配置项 `classify_side_report` 指定或关闭），
> 命中即跳过 Modrinth 查询；命中数会显示在报告的「依据来源」一行。
> 详见 [服务端-Python版.md 的 4.2 模组分类](服务端-Python版.md#42-模组分类)（两边配置键与行为完全一致）。
>
> 报告还可以**通过网络上报**（`side_report_enabled`，默认关闭）：工具用**同一个 MSFP 端口**
> 发 `REPORT <token> <length>\n<JSON>`，服务端原子写入 `side_report_path`
> （MCDR 版默认 `config/autosync/autosync-data/side-report.json`），
> 之后 classify 会自动读到它。开关打开但没配 `side_report_token` 时，`REPORT` 一律回
> `ERR disabled`（不配令牌绝不接收写入）。完整报文与响应见
> [服务端-Python版.md 4.2.1](服务端-Python版.md#421-让-modsidedetector-直接网络上报可选默认关闭)。
| `!!autosync deps` | 所有人 | **只读**依赖检查（解析 jar 元数据，含 JiJ 嵌套层） |
| `!!autosync deps fix` | 所有人 | 列出缺失前置编号与候选版本（**不下载任何文件**） |
| `!!autosync deps fix 1a 2a` | 等级 2 | 只下载指定编号的前置 |
| `!!autosync deps fix apply` | 等级 2 | 全选推荐候选并下载到 `<dist_dir>/mods/` |

**权限说明**：等级 2 = MCDR 的 `PermissionLevel.HELPER`。只读命令（`status` / `classify` / `deps` / `deps fix` / `help`）任何玩家都能用；会写盘或联网下载的命令需要等级 2。低权限玩家执行会收到一行 `✘ 权限不足 该操作需要权限等级 2`。

### 3.1 可点击按钮

`!!autosync help` 的输出里，底部有一排快捷按钮：

```
[status] [build] [build refresh] [tcp restart] [classify] [deps] [deps fix] [help]
```

* **游戏内**：每个按钮都能点，鼠标悬停会显示用途与影响；点击后命令会**填入聊天输入框**（不是自动执行），**按一次回车**才会执行。
* **控制台**：按钮退化成 `[标签]` 纯文本，点不了但完全可读、不报错。

> 为什么是"填入聊天框"而不是"直接执行"：MC 1.19.1+ 的 `tellraw` 只接受以 `/` 开头的 `run_command` 值，而 `!!autosync xxx` 不以 `/` 开头，用 `run_command` 会**静默失效**（点了没反应）；`suggest_command` 是 1.19.1+ 上唯一可行的方案。

---

## 4. 典型工作流（MCDR）

```text
# 1. 第一次：把服务端 mods 里要发的模组硬链接/复制进分发目录
#    （在 MCDR 所在机器上操作）
mkdir -p server/client-dist/mods
for f in server/mods/*.jar; do ln -f "$f" "server/client-dist/mods/$(basename "$f")"; done

# 2. 游戏内 / 控制台执行构建
!!autosync build

# 3. 看一眼结果
!!autosync status

# 4. 想让插件顺手把纯服务端模组挑出来（先干跑，确认无误再 apply）
!!autosync classify
!!autosync classify apply

# 5. 玩家报告"缺前置"时
!!autosync deps
!!autosync deps fix
!!autosync deps fix apply
```

改了配置之后：

```text
!!autosync reload
```

---

## 5. 与独立 Python 版的差异汇总

| 项 | [独立 Python 版](服务端-Python版.md) | MCDR 版 |
| --- | --- | --- |
| 安装 | 复制代码即可，只需 Python | 把 `.mcdr` 丢进 `plugins/` |
| 依赖 | 纯标准库 | MCDR 2.10+ |
| 操作入口 | `python3 -m autosync <动作>` / 交互 shell | `!!autosync <子命令>` |
| 配置位置 | 工作目录 `config.json` | `config/autosync/config.json` |
| 数据位置 | 工作目录 `autosync-data/` | `config/autosync/autosync-data/` |
| 权限控制 | 系统用户权限 | MCDR 权限等级（写操作需要等级 2） |
| 命令命名 | `classify-apply` / `deps-fix`（kebab-case） | `classify apply` / `deps fix`（带空格） |
| 输出 | 终端 ANSI 彩色 | MC 聊天栏 RText 彩色 + 可点按钮 |
| `base_dir: ""` | 当前工作目录 | MCDR 的 `working_directory`（`server/`） |
| 核心业务代码 | `autosync/` 本体 | `autosync/core/`（**同一份代码的副本**） |

清单格式、MSFP 协议、配置键**完全一致**，两边可以随时互换。

### 代码复用说明

MCDR 版**不复制业务逻辑**：

```
server/python/autosync/      ← 唯一的一份核心代码（独立版本体）
server/mcdr/autosync/core/   ← 构建时由 tools/build_mcdr.py 同步过来的逐字节副本
server/mcdr/autosync/entry.py← MCDR 入口（唯一依赖 mcdreforged 的模块）
```

* `tools/build_mcdr.py`（跨平台，推荐）或 `tools/build-mcdr.ps1`（Windows）在打包前会把 `server/python/autosync` 复制到 `server/mcdr/autosync/core`，并逐个文件比对 SHA-256；**任何一处不一致都会报错中止打包**。
* 入口模块用相对导入 `from .core.builder import AutoSyncCore` 使用核心，并直接复用 `from .core.shell import AutoSyncShell` 的交互层——MCDR 版只是把 shell 的输出通道换成了 RText。
* 因此**修 bug 只改 `server/python/autosync`**，然后执行一次打包脚本即可。

---

## 6. 自己重新打包 `.mcdr`

如果你改了核心代码或入口，需要重新生成插件包：

```bash
# 跨平台（Windows / Linux / macOS / CI 都推荐这个，纯标准库，无需 PowerShell）
python tools/build_mcdr.py

# 指定带 mcdreforged 的解释器 —— 会用它做真实的加载 + 命令注册验证
python tools/build_mcdr.py --python /path/to/python

# 改了版本号
python tools/build_mcdr.py --version 1.0.1

# 跳过入口校验（不推荐）
python tools/build_mcdr.py --skip-verify
```

Windows 上也可以沿用 PowerShell 版（做的事完全一样）：

```powershell
pwsh -File tools/build-mcdr.ps1
pwsh -File tools/build-mcdr.ps1 -SkipVerify
```

> 两个脚本在没装 mcdreforged 时都会自动退回 `tests/mcdr_stub` 桩做结构性校验。

脚本会依次做：同步核心代码 → 比对 SHA-256 → 清 `__pycache__` → 校验入口 → 打成 zip → **命名为 `.mcdr`**（`.mcdr` 本身就是 zip，只是后缀不同；zip 内路径必须是正斜杠）→ 用 `tools/check-mcdr-package.py` 检查包内结构与 `mcdreforged.plugin.json` 元数据 → 打印包内文件清单。

手工验证包结构：

```bash
python tools/check-mcdr-package.py server/mcdr/AutoSync-1.0.0.mcdr
```

用真实 MCDR 验证入口（不启动服务端，只跑加载 + 命令注册 + `help` / `status` 的实际执行）：

```bash
pip install mcdreforged
python tools/verify-mcdr-entry.py
# 期望最后一行：结果：全部通过（真实 mcdreforged，8 个可点按钮）
```

### 包内结构

```
AutoSync-1.0.0.mcdr   (zip)
├── mcdreforged.plugin.json     id=autosync, version=1.0.0, entrypoint=autosync.entry
├── autosync/
│   ├── __init__.py             版本号 / 包说明
│   ├── entry.py                MCDR 入口
│   └── core/                   独立版核心代码的副本（17 个模块，含 side_report.py）
└── config.example.json         默认配置参考
```

> **为什么入口点在 `autosync.entry` 而不是 `autosync_mcdr.entry`**：MCDR 会校验入口点必须位于**与插件 id 同名的包**内，否则加载时报
> `ValueError: Invalid entry point 'autosync_mcdr.entry' for plugin id 'autosync'`。
> 所以核心代码被放在 `autosync/core/` 子包里，入口直接在 `autosync` 包根上。

---

## 7. 卸载

```text
!!MCDR plugin unload autosync
```

然后删掉 `plugins/AutoSync-1.0.0.mcdr`。插件卸载时会自动停掉 MSFP 服务与定时轮询线程，不会留下后台线程。

配置与数据在 `config/autosync/` 下，按需删除。
