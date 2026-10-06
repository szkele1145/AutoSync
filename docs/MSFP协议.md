# ModSync 文件传输协议 (MSFP) v1

## 为什么不用 HTTP

服务器位于**阿里云中国大陆节点**。阿里云的备案拦截系统会**识别并拦截 HTTP 流量**（依据 Host 头），
对未备案域名返回 403（响应头 `Server: Beaver`，页面标题 `Non-compliance ICP Filing`）。

但**裸 TCP 流量不受影响**。实测证据：用 Minecraft 协议连接同一条隧道的 `cn-gz-1.qwq.fan:14888`，
可正常握手并取回完整的 Server List Ping 响应。

因此本协议**不使用 HTTP**，直接跑在原始 TCP 上，由 frp TCP 隧道承载。

## 传输层

原始 TCP。服务端本地默认监听 **8123**，由 frp TCP 隧道映射成公网地址。

## 消息格式

整数一律为十进制 ASCII；字段之间用单个空格分隔；请求以 `\n` 结尾。
**路径放在行末**，因此可以包含空格、中文、方括号等字符（UTF-8 编码）。

### 请求

```
GET <start> <end> <path>\n
```

- `start` / `end`：闭区间字节偏移（十进制非负整数）。整文件下载写 `GET 0 -1 <path>\n`；
  `end` 超过文件末尾时，服务端按实际长度返回。
- `path`：相对分发目录根的路径，用 `/` 分隔，UTF-8 编码，取行末到 `\n` 之前的全部内容。

### 响应

成功：

```
OK <length>\n
<length 字节的原始二进制数据>
```

失败：

```
ERR <message>\n
```

`message` 为单行 UTF-8 文本。

### 辅助请求

```
PING\n            ->  OK 0\n              连通性与延迟(RTT)探测，不传数据
SIZE <path>\n     ->  OK <size>\n         查询文件大小，不传数据
```

### 上报请求（REPORT，默认关闭）

```
REPORT <token> <length>\n
<length 字节的 UTF-8 JSON>
```

- **用途**：配套工具 **ModSideDetector** 生成 `side-report.json` 后，直接用这条命令把报告推给
  服务端，省掉人工拷贝（工具与服务端不在同一台机器时尤其有用）。
- `token`：共享令牌，单行、**不含空格**，必须与服务端配置 `side_report_token` 完全一致
  （服务端用恒定时间比较，防时序侧信道）。
- `length`：紧随其后的 JSON 字节数，十进制非负整数，**上限 32 MB**。服务端**先看长度再决定
  读多少**：超限立刻回 `ERR too large` 并且不把数据读进内存。
- JSON 就是 `side-report.json` 全文。服务端只做**最小校验**（顶层是对象且 `mods` 是数组），
  校验不过回 `ERR bad request` 且**不落盘**；通过则**原子写入**（先写 `.tmp` 再 `os.replace`）
  `side_report_path`（默认 `<data_dir>/side-report.json`）。
- 是否接收由配置决定：`side_report_enabled` **默认 `false`**；即使打开，`side_report_token`
  为空时也一律回 `ERR disabled`（绝不能出现「没配令牌就能往磁盘写文件」）。
- 收到报告**不会自动触发** classify 分析（避免副作用），只写一条日志提示
  「下次 classify 将使用这份新报告」。

对应的响应（替代 `OK <length>\n + 数据`）：

```
OK <count>\n               # 成功。<count> = 报告里 mods 数组的条目数
ERR unauthorized\n         # 令牌不匹配
ERR disabled\n             # 功能被配置关闭，或未配置令牌
ERR too large\n            # length 超过上限（读取数据之前就拒绝）
ERR bad request\n          # 缺参数 / length 非法 / JSON 不合法 / 顶层不是对象 / mods 不是数组
ERR internal <detail>\n    # 落盘失败。<detail> 会截断到 200 字符，并把换行替换成空格
```

> **`ERR disabled` 有两个来源，排障时先分清**：
> 1. **服务端**没开（`side_report_enabled=false`）或没配令牌（`side_report_token` 为空）；
> 2. **客户端**侧 `autosync_token` 为空时**根本不会发请求** —— 它在本地就拦下并提示
>    「未配置令牌（autosync_token 为空），已跳过上报」。
>
> 所以真的收到 `ERR disabled`，说明是「客户端配了令牌、服务端没开或没配」。

连接语义：成功与 `GET` / `SIZE` 一样**保持 keep-alive**；任何错误响应后服务端**关闭连接**
（未读完的 body 会先被丢弃式读掉，避免它被当成下一个请求行，也避免 `close()` 的 RST
把 `ERR` 冲掉）。

> 配套工具 **ModSideDetector** 目前是**一连接一请求**：发完一条 `REPORT`、读到一行响应就主动
> 关闭，**不复用连接**。协议允许 keep-alive，这里只是说明工具端的实现现状。

### 两侧配置项对照

上报要在两边各配一半，键名不同，容易配错：

| 作用 | AutoSync 侧 | ModSideDetector 侧 |
|---|---|---|
| 启用上报 | `side_report_enabled`（默认 `false`） | `autosync_report_enabled`（默认 `false`） |
| 共享令牌 | `side_report_token`（默认 `""`） | `autosync_token`（默认 `""`） |
| 监听 / 连接地址 | `tcp_host`（默认 `0.0.0.0`） | `autosync_host`（默认 `""`） |
| 监听 / 连接端口 | `tcp_port`（默认 `8123`） | `autosync_port`（默认 `8123`） |
| 报告落盘路径 | `side_report_path`（默认 `<data_dir>/side-report.json`） | ——（只发送，不落盘） |
| 超时 | —— | `autosync_timeout`（默认 `30.0` 秒） |

**两侧令牌必须一致**，否则会回 `ERR unauthorized`；端口也要对上（默认都是 `8123`）。

## 连接语义

- 一个连接可连续发送多个请求（keep-alive），服务端逐个响应
- 客户端可**并发建立多个连接**实现分块下载（多线程聚合多条线路带宽的基础）
- 服务端空闲 **30 秒**后关闭连接
- v1 不做多路复用：单连接内发一个请求等一个响应，不要流水线

## 错误消息

`ERR not found` / `ERR forbidden` / `ERR bad request` / `ERR internal <detail>` /
`ERR unauthorized` / `ERR disabled` / `ERR too large`

## 安全要求

服务端**必须做路径规范化**，拒绝任何逃逸出分发目录根的路径（`..`、绝对路径、符号链接逃逸）。
客户端不应依赖服务端校验，但两侧都要校验。

`REPORT` 的令牌比较必须用恒定时间算法（`hmac.compare_digest`），不要用 `==`。

## 兼容性

第一字节不是 `GET` / `PING` / `SIZE` / `REPORT` 开头的连接，服务端应直接返回 `ERR bad request` 并关闭。
（`REPORT` 以 `R` 开头，与老客户端用的 `GET` / `PING` / `SIZE` 天然不冲突；`REPORT` 默认关闭时，
不认识它的老服务端也只会回 `ERR bad request`，不会破坏已有行为。）
