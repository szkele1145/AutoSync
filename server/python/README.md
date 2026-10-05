# AutoSync · 独立 Python 版服务端

**纯标准库**（Python 3.11+）的 AutoSync 服务端：扫描分发目录 → 生成客户端清单 `manifest.json` → 用 **MSFP v1（裸 TCP）** 把文件发给客户端。
不依赖 MCDR，不依赖任何第三方包，可以单独跑在一台机器上。

完整文档见仓库根目录的 [docs/服务端-Python版.md](../../docs/服务端-Python版.md)；
系统级部署见 [docs/部署-Linux.md](../../docs/部署-Linux.md) / [docs/部署-Windows.md](../../docs/部署-Windows.md)。

---

## 最短上手

```bash
cd server/python

# 1. 验证程序（第一次运行会在当前目录自动生成 config.json）
python3 -m autosync --version
# -> AutoSync 1.0.0

# 2. 放好要分发给客户端的文件（约定成 ../client-dist）
mkdir -p ../client-dist/mods
cp /path/to/your/mods/*.jar ../client-dist/mods/

# 3. 构建清单 + 协议自检
python3 -m autosync --base .. build
python3 -m autosync --base .. check      # 期望最后一行：结果：全部通过

# 4. 常驻运行（Ctrl+C 退出）
python3 -m autosync --base .. serve
```

也可以进交互式 shell（启动即拉起服务，再进 `AutoSync> ` 提示符）：

```bash
python3 -m autosync --base .. shell
```

---

## 目录里有什么

| 路径 | 说明 |
| --- | --- |
| `autosync/` | 程序本体（包名不能改） |
| `config.example.json` | 配置模板；不写 `config.json` 时程序会按这些默认值自动生成一份带注释的配置 |
| `requirements.txt` | 空依赖说明（只用标准库） |

运行时会在工作目录产生：

| 路径 | 说明 |
| --- | --- |
| `config.json` | 配置（首次运行自动生成） |
| `autosync-data/` | 状态与缓存（`state.json`、各种 JSON 报告） |
| `<dist_dir>/manifest.json` | 给客户端的清单 |
| `<dist_dir>/speedtest.bin` | 512KB 测速文件 |

这些都不该提交进 git（仓库 `.gitignore` 已排除）。

---

## 常用命令

```bash
python3 -m autosync --base .. shell                # 交互式 REPL（推荐）
python3 -m autosync --base .. serve                # 只跑 MSFP 服务（常驻）
python3 -m autosync --base .. build                # 只构建后退出
python3 -m autosync --base .. status               # 文件数 / 清单版本 / 端口与连接统计
python3 -m autosync --base .. check                # 协议自检
python3 -m autosync --base .. classify             # 干跑：纯客户端 / 双端 / 纯服务端 / 待定
python3 -m autosync --base .. classify-apply       # 按上次结果搬运
python3 -m autosync --base .. deps                 # 只读依赖检查
python3 -m autosync --base .. deps-fix             # 列出缺失前置编号与候选
python3 -m autosync --base .. deps-fix 1a 2a       # 只下载指定编号
python3 -m autosync --base .. deps-fix apply       # 全选推荐候选并下载
```

全局参数：`--base` / `--data` / `--config` / `--dist` / `--port` / `--connections` / `--refresh` / `--json` / `--quiet`。

---

## 配置文件

默认读**当前工作目录**下的 `config.json`，不存在就自动生成。最常改的几项：

```json
{
  "dist_dir": "client-dist",
  "base_dir": "",
  "tcp_host": "0.0.0.0",
  "tcp_port": 8123,
  "tcp_enabled": true,
  "speedtest_enabled": true,
  "poll_interval_seconds": 0,
  "allow_empty_dist": false,
  "deps_fix_game_version": "1.21.1",
  "deps_fix_loader": "neoforge"
}
```

完整键与逐项注释见 `config.example.json`。

---

## 与 MCDR 版的代码关系

`server/mcdr/autosync/core/` 是本目录 `autosync/` 的**逐字节副本**，由 `tools/build-mcdr.ps1` 在打包 `.mcdr` 前同步并做 SHA-256 比对。
**业务逻辑只改这里**，然后重新跑一次打包脚本即可。
