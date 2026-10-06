# AutoSync Java 客户端

服务端清单（`manifest.json`）驱动的自动更新器，支持 javaagent（`-javaagent:AutoSync-1.0.0.jar`）、
独立进程（`java -jar AutoSync-1.0.0.jar`）与 modloader 三种启动方式。

## 构建

需要 JDK 17（编译）/ JRE 21（运行）：

```bash
JAVA_HOME=~/.jdks/ms-17.0.19 ./gradlew clean shadowJar
```

产物：`build/libs/AutoSync-1.0.0.jar`（版本号在 `build.gradle.kts` 的 `debugVersion`，发布时由 tag 覆盖）。

配置文件仍叫 **`mcpatch.yml`**（放在 jar 同级目录），内部数据目录
`.modsync-temp/`、`.modsync-removed/`、`modsync/` 也保持不变，与既有部署兼容。

## 下载来源：`source-mode`（默认 `auto`）

清单里每个文件条目都带 `urls`，例如：

```json
{ "path": "mods/x.jar", "size": 123, "sha256": "..",
  "urls": ["files/mods/x.jar", "https://cdn.modrinth.com/data/.../x.jar"] }
```

相对地址（`files/mods/x.jar`）会拼到更新源上、走 MSFP；`http(s)://` 绝对地址是 CDN 直链。
本次同步到底用哪一种，由 `source-mode` 决定：

| `source-mode` | 行为 |
| --- | --- |
| `auto`（默认） | 同步开始时先测速一次，CDN 与服务端谁快用谁（详见下一节） |
| `cdn` | 不测速，所有文件直接用 CDN 直链；CDN 失败照旧自动回退服务端 |
| `server` | 不测速，完全不碰 CDN，所有文件都走 `urls` 里的更新源 |

`prefer-cdn` 作为旧写法继续兼容：`true` → `cdn`，`false` → `server`；
两个都写时**以 `source-mode` 为准**，`prefer-cdn` 被忽略。

无论哪种模式，`cdn-exclude` 命中的文件都强制走更新源（连 CDN 都不试）。

## CDN / 服务端 自动测速选源（`source-mode: auto`）

每次同步（只要这次有文件要下）先测一次速，**本次同步内所有文件共用这一个结果**，不会每个文件都测：

1. **挑样本**：优先从**本次待下载列表**里挑最大的、体积 ≥ `speedtest-min-size-mb`（默认 10MB）的文件；
2. **影子样本**：如果一个够大的待下载文件都没有（大文件都已是最新），就挑一个**本地已经有的大文件**，
   **只读它在清单里的路径 / 大小 / 下载链接**，CDN 与服务端各下一小段到**临时文件**，测完立即删除；
3. **并发测速**：CDN 与服务端**同时**各下一段，时长 `speedtest-duration-ms`（默认 2000ms），
   各自算出 MB/s（只统计「第一个字节到达 → 读完」，建连与首字节等待不算进带宽）；
4. **选择规则**：`cdn_speed >= server_speed × cdn-prefer-ratio`（默认 0.5）→ **选 CDN**（省服务端带宽，
   差距可以接受）；否则先考虑提线程；
5. **提线程重测**：判定 CDN 太慢时，把 CDN 的分段连接数翻倍（上限 `cdn-max-threads`，默认 64）重测一次，
   因为很多 CDN 是按单连接限速的，加连接就能提速；重测还是不如服务端才最终选服务端。
   如果重测后判定 CDN 够快，**真实下载也会用这个更高的线程数**，不会出现「测出来快、下载还是慢」；
6. **缓存**：测速结果按 `source-cache-seconds`（默认 3600 秒）缓存到 `modsync/source-choice.json`，
   缓存没过期时下次同步直接复用，不再测速（配置项或 CDN 主机 / 服务端地址变了会自动作废重测）；
7. **兜底**：两个来源都测不出速度（没有合适样本、请求全部失败等）→ 回退到改动前的行为：
   先 CDN 后服务端依次尝试，同步流程不受影响。

### 关于「影子测速」的取舍（重要）

需求原话是「把本地那个大文件删了重新下，用下载速度来测」。**没有这么做**，原因是：
那意味着要删掉用户一个几百 MB、而且已经是最新的真实文件，一旦测完速之后下载失败、断网、
断电或者进程被杀，用户就白白丢了一个文件，换来的只是省下几百 MB 的本地复制/下载量。所以实现改成了影子测速：

- 只从清单里读该文件的路径、大小、下载链接，**不读取、不移动、不删除、不改名、不覆盖**本地文件；
- CDN 与服务端各下一小段到 `.modsync-temp/speedtest/` 下的临时文件，测完立刻把整个目录删掉；
- 正常结束、抛异常、JVM 退出（shutdown hook）都会清理；上次异常退出留下的残留目录，
  会在**下次同步开始时**被先清干净，所以不会越积越多。

代价是：如果本地那个大文件其实和服务端上的不一致（正常流程下不会发生，因为影子样本只从
「本次被判定为最新」的文件里挑），测出来的速度对应的是清单里声明的那份文件。
这不影响「CDN 和服务端谁更快」这个判断，因为两边测的本来就是同一份清单条目。

### 日志示例

```text
[测速] 样本 Flashback-0.39.10-for-MC1.21.1.jar（201.00 MB，本次待下载）
[测速] CDN 与 服务端 并发测速，各 32 条连接，时长 2000ms，每个来源最多读 8.00 MB
[测速] CDN 3.21 MB/s  |  服务端 1.42 MB/s  ->  选 CDN（达服务端 226%）
[CDN] Flashback-0.39.10-for-MC1.21.1.jar 从 cdn.modrinth.com 下载成功
```

```text
[测速] 样本 xyz-plus-1.4.jar（64.00 MB，本地已有）
[测速] 影子样本（本地已有，仅下载临时片段，不影响原文件）
[测速] CDN 0.31 MB/s  |  服务端 2.80 MB/s  ->  CDN 太慢，提升线程到 64 重测
[测速] 重测 CDN 0.55 MB/s  |  服务端 2.80 MB/s  ->  选服务端
[服务端] xyz-plus-1.4.jar（本次测速选了服务端，跳过 CDN）
```

```text
[测速] source-mode: cdn（来自 source-mode），强制使用 CDN，跳过测速
[测速] 命中测速缓存（123 秒前测的，...），本次不再测速：选 CDN
[测速] 本次没有体积不小于 10 MB 的可用样本，跳过测速，按原有顺序（先 CDN 后服务端）尝试
[测速] CDN 与 服务端 都没有测出速度，回退到原有行为（先 CDN 后服务端）
```

### 流量与连接数代价

- 一次测速**每个来源最多读 8MB**（单条连接最多 1MB），时长与线程数拉满也不会失控；
  最坏情况（判定 CDN 太慢、要提线程重测）一次同步的额外流量约 24MB。
  不想付这个代价就用 `source-mode: cdn` / `server`，或者把 `speedtest-duration-ms` 调小；
- 测速时 CDN 与服务端是并发的，峰值连接数约 `2 × download-threads`（默认 64），
  和下载阶段的峰值（`concurrent-files × download-threads`）是同一个量级，只持续 2 秒左右。

## 连接数权衡：`concurrent-files` × `download-threads`

**峰值连接数 ≈ `concurrent-files` × `download-threads`**（小于 `download-threshold` 的小文件只占 1 条）。
连接数开太大时，frp 之类的隧道会直接把连接 RST 掉（实测 200+ 并发连接会被打回），反而全面变慢。

默认值按「隧道友好」给：

```yaml
download-threads: 32   # 单个大文件的最大分段数（走 CDN 时同样生效）
concurrent-files: 2    # 同时下载的文件数 → 峰值 2 × 32 = 64 条连接
download-threshold: 1048576
cdn-timeout: 15000     # CDN 直链的连接/读超时（毫秒）
```

- 只有大文件（如几百 MB 的 Flashback）才会吃满 32 段，小文件依旧只开 1 条连接；
- 隧道/家宽条件一般时保持 `concurrent-files: 2`；
- 网络确实很好可以试 `3~4`（96~128 条连接），再高容易触发隧道 RST 与服务端限流。

## `cdn-exclude`：强制走更新源的文件

有些文件本来就不在 CDN 上（自托管文件），每次白试一次 CDN 既慢又吵。用 `cdn-exclude` 跳过：

```yaml
cdn-exclude: []            # 默认空列表，所有文件都先试 CDN
# cdn-exclude:
#   - Flashback-*.jar      # 只写文件名 → 按文件名通配匹配
#   - *.jar                # 所有 jar 都走更新源
#   - mods/xxx.jar         # 带 / → 按清单里的完整相对路径匹配
#   - mods/lib/*.jar       # 路径通配也可以
```

支持 `*` 与 `?`，匹配不区分大小写；命中时日志会打印 `[服务端] xxx.jar（命中 cdn-exclude）`。
命中 `cdn-exclude` 的文件也不会被选作测速样本（没有 CDN 可测）。

## 其它相关配置

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `urls` | — | 更新源列表（MSFP），CDN 未能使用时的下载来源 |
| `source-mode` | `auto` | `auto` 测速选源 / `cdn` 强制 CDN / `server` 强制服务端 |
| `prefer-cdn` | — | 旧写法，等价于 `source-mode: cdn` / `server`；两个都写时以 `source-mode` 为准 |
| `speedtest-min-size-mb` | `10` | 测速样本的最小体积（MB） |
| `speedtest-duration-ms` | `2000` | 每个来源的测速时长（毫秒） |
| `cdn-prefer-ratio` | `0.5` | CDN 速度达到服务端的这个比例即选 CDN |
| `cdn-max-threads` | `64` | CDN 测速太慢时提线程重测的上限 |
| `source-cache-seconds` | `3600` | 测速结果缓存时间（秒），更新源测速与 CDN/服务端测速共用 |
| `cdn-exclude` | `[]` | 强制走更新源的文件名单（也不参与测速采样） |
| `cdn-timeout` | `15000` | CDN 直链的连接/读超时（毫秒） |
| `download-threads` | `32` | 单文件最大分段数，也是测速时的初始连接数 |
| `concurrent-files` | `2` | 文件级并发数 |
| `download-threshold` | `1048576` | 超过该大小才分块（字节） |
| `retries` | `3` | 分块失败的重试次数 |
| `ignore-ssl-cert` | `false` | 是否忽略 https 证书校验（CDN 与更新源共用） |
