package com.github.balloonupdate.mcpatch.client.sync;

import com.github.balloonupdate.mcpatch.client.config.AppConfig;
import com.github.balloonupdate.mcpatch.client.logging.Log;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.Closeable;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.RandomAccessFile;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;
import java.util.regex.Pattern;

/**
 * 自定义 TCP 文件传输协议（MSFP v1）客户端<p>
 * 协议跑在原始 TCP 上（不是 HTTP），因为阿里云大陆节点会拦截未备案域名的 HTTP 流量，
 * 但裸 TCP 不受影响，可以走 frp 之类的纯 TCP 隧道<p>
 * 请求与响应格式见 PROTOCOL.md：<pre>
 *     GET &lt;start&gt; &lt;end&gt; &lt;path&gt;\n   -&gt;  OK &lt;length&gt;\n + length 字节原始数据
 *     SIZE &lt;path&gt;\n               -&gt;  OK &lt;size&gt;\n
 *     PING\n                      -&gt;  OK 0\n
 *     失败                        -&gt;  ERR &lt;message&gt;\n
 * </pre>
 * 路径放在请求行末，所以可以包含空格、中文、方括号等字符
 */
public class TcpFileClient {
    /**
     * 默认端口
     */
    public static final int DEFAULT_PORT = 8123;

    /**
     * 协议前缀
     */
    public static final String SCHEME = "msfp://";

    /**
     * 单次读取的缓冲区大小
     */
    static final int BUFFER_SIZE = 1 << 16;

    /**
     * 状态行（OK/ERR 那一行）的最大长度，防止服务端乱发数据把内存撑爆
     */
    static final int MAX_LINE = 8192;

    /**
     * 裸 host:port 地址的识别规则，例如 127.0.0.1:8123、cn-gz-1.qwq.fan:8123/files/mods/a.jar
     */
    static final Pattern BARE_ADDRESS = Pattern.compile("^[A-Za-z0-9._\\-\\[\\]]+:\\d+(/.*)?$", Pattern.DOTALL);

    /**
     * 预分配文件长度用的全局锁<p>
     * setLength 是「先读后写」的复合操作，多线程同时扩充同一个文件时必须串行化，
     * 否则可能出现已经扩到 200 字节的线程又被另一个线程缩回 100 字节的情况
     */
    static final Object PREALLOC_LOCK = new Object();

    /**
     * 下载进度回调，语义与旧的 HTTP 实现保持一致<p>
     * 多个下载线程会并发回调，实现里已经用互斥锁串行化，回调方不需要再考虑线程安全
     */
    @FunctionalInterface
    public interface Progress {
        void on(long batch, long downloaded, long total);
    }

    /**
     * 分片写入时用的重试次数固定为 2<p>
     * 刻意不跟 cfg.reties 联动：reties 是给「下大文件时网络抽风」准备的宽松重试，
     * 而多更新源测速必须尽快出结果，不能因为某个慢源把整轮测速拖成 reties × tcp-timeout
     */
    static final int FRAGMENT_RETRIES = 2;

    /**
     * 服务端明确返回 ERR 时抛出的异常<p>
     * 这类错误是服务端的确定性答复（文件不存在、路径非法等），重试没有意义，直接向上抛
     */
    static class ServerErrorException extends IOException {
        ServerErrorException(String message) {
            super(message);
        }
    }

    /**
     * 一次可以重试的网络操作
     */
    @FunctionalInterface
    interface Request<T> {
        T run() throws IOException;
    }

    /**
     * 主机 + 端口
     */
    public static class HostPort {
        public final String host;
        public final int port;

        public HostPort(String host, int port) {
            this.host = host;
            this.port = port;
        }

        @Override
        public String toString() {
            return host + ":" + port;
        }

        @Override
        public boolean equals(Object o) {
            if (this == o) return true;
            if (!(o instanceof HostPort)) return false;

            HostPort other = (HostPort) o;

            return port == other.port && host.equals(other.host);
        }

        @Override
        public int hashCode() {
            return host.hashCode() * 31 + port;
        }
    }

    /**
     * 一个完整的下载目标：更新源地址 + 服务端分发目录里的相对路径
     */
    public static class Endpoint {
        public final HostPort hostPort;
        public final String path;

        public Endpoint(HostPort hostPort, String path) {
            this.hostPort = hostPort;
            this.path = path;
        }

        @Override
        public String toString() {
            return hostPort + "/" + path;
        }
    }

    // ------------------------------------------------------------------
    // 地址解析
    // ------------------------------------------------------------------

    /**
     * 解析更新源地址，支持 msfp://host:port 与裸 host:port 两种写法，端口缺省为 8123<p>
     * 地址后面可以跟路径（msfp://host:port/files/a.jar），这里只取主机和端口部分
     *
     * @throws IllegalArgumentException 地址格式不合法
     */
    public static HostPort parse(String url) {
        if (url == null) {
            throw new IllegalArgumentException("更新源地址为空");
        }

        String value = url.trim();

        if (value.isEmpty()) {
            throw new IllegalArgumentException("更新源地址为空");
        }

        if (value.regionMatches(true, 0, SCHEME, 0, SCHEME.length())) {
            value = value.substring(SCHEME.length());
        } else if (value.contains("://")) {
            throw new IllegalArgumentException("不支持的更新源协议，MSFP 只支持 msfp://host:port 或裸 host:port 写法：" + url);
        }

        // 去掉路径部分，只保留 host:port
        int slash = value.indexOf('/');

        if (slash >= 0) {
            value = value.substring(0, slash);
        }

        if (value.isEmpty()) {
            throw new IllegalArgumentException("更新源地址里没有主机名：" + url);
        }

        String host = value;
        int port = DEFAULT_PORT;

        // 兼容 [::1]:8123 这种 IPv6 写法
        int colon = value.lastIndexOf(':');

        if (colon > 0) {
            host = value.substring(0, colon);

            String portText = value.substring(colon + 1).trim();

            if (!portText.isEmpty()) {
                port = parsePort(portText, url);
            }
        }

        if (host.isEmpty()) {
            throw new IllegalArgumentException("更新源地址里没有主机名：" + url);
        }

        return new HostPort(host, port);
    }

    static int parsePort(String text, String url) {
        int port;

        try {
            port = Integer.parseInt(text);
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException("更新源地址里的端口不是数字：" + url);
        }

        if (port <= 0 || port > 65535) {
            throw new IllegalArgumentException("更新源地址里的端口超出范围：" + url);
        }

        return port;
    }

    /**
     * 判断清单里的一个下载地址是完整的 MSFP 地址，还是相对于清单所在源的相对路径<p>
     * 完整地址有两种：msfp://host:port[/path] 和 host:port[/path]<p>
     * 其余（例如 files/mods/a.jar）都当作相对路径，会拼到清单所在源上，与旧版 HTTP 的行为保持一致
     */
    public static boolean isAbsolute(String url) {
        if (url == null) {
            return false;
        }

        String value = url.trim();

        if (value.isEmpty()) {
            return false;
        }

        if (value.regionMatches(true, 0, SCHEME, 0, SCHEME.length())) {
            return true;
        }

        return BARE_ADDRESS.matcher(value).matches();
    }

    /**
     * 从一个完整 MSFP 地址里取出路径部分，没有路径时返回 null<p>
     * 相对路径不是完整地址，同样返回 null
     */
    public static String pathOf(String url) {
        if (!isAbsolute(url)) {
            return null;
        }

        String value = url.trim();

        if (value.regionMatches(true, 0, SCHEME, 0, SCHEME.length())) {
            value = value.substring(SCHEME.length());
        }

        int slash = value.indexOf('/');

        if (slash < 0) {
            return null;
        }

        return value.substring(slash);
    }

    /**
     * 把清单里的一个下载地址解析成「主机端口 + 服务端路径」<p>
     * 1. 完整地址：主机端口取地址里的，地址里带了路径就用地址里的路径，否则用清单里的文件路径<p>
     * 2. 相对路径：主机端口用清单所在更新源，路径就是这个相对路径（与旧版 HTTP 的 join 行为一致）
     *
     * @param raw          清单里的一个下载地址，可以为空
     * @param base         清单所在更新源
     * @param fallbackPath 清单里声明的文件路径，地址里没写路径时用它
     */
    public static Endpoint resolve(String raw, HostPort base, String fallbackPath) {
        String value = raw == null ? "" : raw.trim();

        if (value.isEmpty()) {
            return new Endpoint(base, stripLeadingSlash(fallbackPath));
        }

        if (!isAbsolute(value)) {
            return new Endpoint(base, stripLeadingSlash(value));
        }

        HostPort hostPort = parse(value);
        String path = pathOf(value);

        if (path == null || path.isEmpty() || path.equals("/")) {
            path = fallbackPath;
        }

        return new Endpoint(hostPort, stripLeadingSlash(path));
    }

    static String stripLeadingSlash(String path) {
        if (path == null) {
            return "";
        }

        String value = path.replace('\\', '/').trim();

        while (value.startsWith("/")) {
            value = value.substring(1);
        }

        return value;
    }

    // ------------------------------------------------------------------
    // 辅助请求
    // ------------------------------------------------------------------

    /**
     * 发一个 PING 测往返延迟（含 TCP 建连时间），单位毫秒
     */
    public static long ping(HostPort hp, AppConfig config) throws IOException {
        long begin = System.nanoTime();

        try (Connection connection = new Connection(hp, config)) {
            connection.request("PING");

            long length = connection.readOkLength();

            if (length != 0) {
                throw new IOException("PING 的响应长度应该是 0，实际是 " + length);
            }
        }

        return Math.max(1L, (System.nanoTime() - begin) / 1_000_000L);
    }

    /**
     * 查询服务端上某个文件的大小（网络抖动时按 cfg.reties 重试）
     */
    public static long size(HostPort hp, String path, AppConfig config) throws IOException {
        return withRetries("查询文件大小 " + path, Math.max(1, config.reties), () -> {
            try (Connection connection = new Connection(hp, config)) {
                connection.request("SIZE " + path);

                return connection.readOkLength();
            }
        });
    }

    /**
     * 只查一次文件大小，网络抖动时不重试<p>
     * 用于「快速确认某个候选地址上到底有没有这个文件」（给测速挑服务端地址用）；
     * 走 withRetries 的话，一个不存在的路径会白白等上 3 次退避
     */
    public static long sizeOnce(HostPort hp, String path, AppConfig config) throws IOException {
        try (Connection connection = new Connection(hp, config)) {
            connection.request("SIZE " + path);

            return connection.readOkLength();
        }
    }

    /**
     * 拉取一个小文本文件（例如清单 manifest.json），整份读进内存后用 UTF-8 解码<p>
     * 中途断线时按 cfg.reties 重试，并且从已经收到的字节后面续传
     */
    public static String fetchText(HostPort hp, String path, AppConfig config) throws IOException {
        ByteArrayOutputStream buffer = new ByteArrayOutputStream();

        return withRetries("下载 " + path, Math.max(1, config.reties), () -> {
            try (Connection connection = new Connection(hp, config)) {
                connection.request("GET " + buffer.size() + " -1 " + path);

                long length = connection.readOkLength();
                connection.readAll(buffer, length);
            }

            return new String(buffer.toByteArray(), StandardCharsets.UTF_8);
        });
    }

    /**
     * 测速结果：实际收到的字节数与「第一个数据块到达 → 读完」这段传输耗时<p>
     * 建连和首字节等待不算进传输耗时里（那些已经体现在 PING 的 RTT 上了），避免重复计算
     */
    public static class SpeedSample {
        public final long bytes;
        public final long transferNanos;

        public SpeedSample(long bytes, long transferNanos) {
            this.bytes = bytes;
            this.transferNanos = transferNanos;
        }

        /**
         * 换算成 MB/s，样本不足时返回 0
         */
        public double megabytesPerSecond() {
            if (bytes <= 0 || transferNanos <= 0) {
                return 0;
            }

            return (bytes / 1024.0 / 1024.0) / (transferNanos / 1_000_000_000.0);
        }
    }

    /**
     * 从某个更新源上拉一段数据用来测吞吐（给多源选优用）<p>
     * 用一条独立的连接发一个 GET 请求，最多读 maxBytes 字节，超过 deadline 或读满就停手；<p>
     * 返回 null 表示一个字节都没读到（调用方应当把该源降级为「只测 RTT」）
     *
     * @param maxBytes     本次最多读多少字节
     * @param timeoutNanos 这条件测速连接的总超时（建连 + 读数据），由 source-probe-timeout 决定
     */
    public static SpeedSample speedtest(HostPort hp, String path, long maxBytes, long timeoutNanos, AppConfig config)
            throws IOException {
        long limit = Math.max(1L, maxBytes);
        long deadline = System.nanoTime() + Math.max(1_000_000L, timeoutNanos);

        long firstByteAt = -1;
        long received = 0;

        try (Connection connection = new Connection(hp, config)) {
            connection.request("GET 0 " + (limit - 1) + " " + path);

            long length = connection.readOkLength();
            long want = Math.min(length, limit);

            byte[] buffer = new byte[BUFFER_SIZE];

            while (received < want) {
                // 到点了就用手上已有的数据算速度，避免慢源把整个测速拖死
                if (System.nanoTime() >= deadline) {
                    break;
                }

                int n = readSome(connection.in, buffer, (int) Math.min((long) buffer.length, want - received));

                if (n <= 0) {
                    break;
                }

                if (firstByteAt < 0) {
                    firstByteAt = System.nanoTime();
                }

                received += n;
            }
        }

        if (received <= 0 || firstByteAt < 0) {
            return null;
        }

        return new SpeedSample(received, System.nanoTime() - firstByteAt);
    }

    /**
     * 按最大尝试次数跑一个网络操作，两次尝试之间做一点退避<p>
     * 服务端明确返回 ERR（ServerErrorException）属于确定性答复，不重试，直接抛出
     */
    static <T> T withRetries(String what, int maxAttempts, Request<T> request) throws IOException {
        IOException lastError = null;

        for (int attempt = 1; attempt <= maxAttempts; attempt++) {
            try {
                return request.run();
            } catch (ServerErrorException e) {
                throw e;
            } catch (IOException e) {
                lastError = e;

                if (attempt < maxAttempts) {
                    Log.warn(what + " 第 " + attempt + " 次尝试失败，准备重试：" + e.getMessage());
                    sleepQuietly(200L * attempt);
                }
            }
        }

        throw new IOException(what + " 重试 " + maxAttempts + " 次后仍然失败", lastError);
    }

    static void sleepQuietly(long millis) throws IOException {
        try {
            Thread.sleep(millis);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IOException("等待重试时被中断", e);
        }
    }

    // ------------------------------------------------------------------
    // 下载
    // ------------------------------------------------------------------

    /**
     * 下载一个字节区间，并写到目标文件的指定偏移<p>
     * 用 RandomAccessFile.setLength() 预分配空间，再用 FileChannel 的定位写（position 写）写入，
     * 定位写是线程安全的，所以多个线程可以同时往同一个文件的不同偏移写而互不干扰<p>
     * 失败时会向上抛异常，由调用方决定是否重试或者清理文件
     *
     * @param start       闭区间起始偏移
     * @param end         闭区间结束偏移，-1 表示一直到文件末尾
     * @param writeOffset 要写到目标文件的哪个偏移
     */
    public static void fetch(HostPort hp, String path, long start, long end, Path target, long writeOffset,
                             AppConfig config, Progress callback) throws Exception {
        createParentDirectories(target);

        try (RandomAccessFile file = new RandomAccessFile(target.toFile(), "rw")) {
            long[] cursor = {start};
            long[] position = {writeOffset};
            AtomicLong downloaded = new AtomicLong();

            // 回调里报的 total 是本次区间的长度；这里不传整文件大小，避免 writeOffset 与 start 不一致时误判越界
            long expected = end >= 0 ? end - start + 1 : -1L;
            Progress adapter = callback == null ? null
                    : (batch, received, ignored) -> callback.on(batch, received, expected);

            transferRange(hp, path, config, file, file.getChannel(), cursor, position, end,
                    downloaded, adapter, new Object(), -1L);
        }
    }

    /**
     * 下载整个文件<p>
     * 先问服务端要文件大小：小于 downloadThreshold 或者线程数为 1 时用单连接整文件下载，
     * 否则切成 min(downloadThreads, size) 段，每段一条独立的 TCP 连接并发下载<p>
     * 单段失败会重试 cfg.reties 次（从断点续传）；整体失败时删除目标文件并抛异常，
     * 让 SyncEngine 能够切换到下一个下载来源
     *
     * @return 实际下载的字节数
     */
    public static long download(HostPort hp, String path, Path target, AppConfig config, Progress callback) throws Exception {
        createParentDirectories(target);

        // reties 配成 1 时说明用户明确要求「只试一次」，这里就退回到老行为。
        // 否则按 FRAGMENT_RETRIES 覆盖：多更新源测速时默认的 3 次重试（含退避）会让一次
        // 144KB 的失败测速拖到 3 秒以上，把整轮选源都拖慢
        int fragmentRetries = Math.max(1, config.reties);

        if (fragmentRetries > 1) {
            fragmentRetries = FRAGMENT_RETRIES;
        }

        AppConfig effective = fragmentRetries == Math.max(1, config.reties)
                ? config
                : shallowCopy(config, fragmentRetries);

        long total = size(hp, path, effective);

        int threads = Math.max(1, effective.downloadThreads);
        long threshold = Math.max(0L, effective.downloadThreshold);

        // 分段数不能超过文件本身的字节数，否则会出现空分段
        int segments = total > 0 ? (int) Math.min((long) threads, total) : 1;

        if (total <= threshold || segments <= 1) {
            Log.debug("使用单连接下载：" + target.getFileName()
                    + "，文件大小 " + total + " 字节，阈值 " + threshold + " 字节 <- " + hp + "/" + path);

            deleteQuietly(target);

            int maxAttempts = Math.max(1, effective.reties);

            try (RandomAccessFile file = new RandomAccessFile(target.toFile(), "rw")) {
                if (total > 0) {
                    file.setLength(total);
                }

                AtomicLong downloaded = new AtomicLong();

                // 整文件也算"一个分段"，因此同样享受重试，失败时从断点续传
                downloadSegment(hp, path, effective, file, file.getChannel(), 0, total - 1, downloaded,
                        callback, new Object(), total, maxAttempts);
            } catch (Exception e) {
                deleteQuietly(target);
                throw e;
            }

            return total;
        }

        Log.debug("启用多线程分块下载：" + target.getFileName()
                + "，文件大小 " + total + " 字节，分段数 " + segments + "，线程数 " + segments
                + "，阈值 " + threshold + " 字节 <- " + hp + "/" + path);

        return downloadSegmented(hp, path, target, effective, callback, total, segments);
    }

    /**
     * 复制一份配置只改 reties，避免动到调用方共享的配置对象
     */
    static AppConfig shallowCopy(AppConfig config, int reties) {
        AppConfig copy = new AppConfig(new java.util.HashMap<>());

        copy.urls = config.urls;
        copy.tcpTimeout = config.tcpTimeout;
        copy.reties = reties;
        copy.downloadThreads = config.downloadThreads;
        copy.downloadThreshold = config.downloadThreshold;
        copy.concurrentFiles = config.concurrentFiles;
        copy.testMode = config.testMode;
        copy.preferCdn = config.preferCdn;
        copy.cdnExclude = config.cdnExclude;
        copy.cdnTimeout = config.cdnTimeout;

        return copy;
    }

    /**
     * 多线程分块下载：把文件切成 segments 段，每段一条独立的 TCP 连接并发写进同一个文件的不同偏移
     */
    static long downloadSegmented(HostPort hp, String path, Path target, AppConfig config, Progress callback,
                                  long total, int segments) throws Exception {
        long[] starts = new long[segments];
        long[] ends = new long[segments];

        long base = total / segments;
        long extra = total % segments;
        long cursor = 0;

        for (int i = 0; i < segments; i++) {
            long length = base + (i < extra ? 1 : 0);

            starts[i] = cursor;
            ends[i] = cursor + length - 1;
            cursor += length;
        }

        AtomicLong downloaded = new AtomicLong();
        AtomicReference<Exception> failure = new AtomicReference<>();
        Object callbackLock = new Object();

        int maxAttempts = Math.max(1, config.reties);

        ExecutorService pool = Executors.newFixedThreadPool(segments, new DownloadThreadFactory());
        CountDownLatch latch = new CountDownLatch(segments);

        try (RandomAccessFile file = new RandomAccessFile(target.toFile(), "rw")) {
            // 预先撑到最终大小，各线程再用 FileChannel 的定位写各写各的偏移
            file.setLength(total);

            FileChannel channel = file.getChannel();

            for (int i = 0; i < segments; i++) {
                final long start = starts[i];
                final long end = ends[i];

                pool.execute(() -> {
                    try {
                        downloadSegment(hp, path, config, file, channel, start, end, downloaded, callback,
                                callbackLock, total, maxAttempts);
                    } catch (Exception e) {
                        failure.compareAndSet(null, e);
                    } finally {
                        latch.countDown();
                    }
                });
            }

            latch.await();

            Exception error = failure.get();

            if (error != null) {
                // 整体失败必须清理掉已经写了一半的文件，让上层能换下一个来源重下
                deleteQuietly(target);
                throw error;
            }

            long received = downloaded.get();

            if (received != total) {
                deleteQuietly(target);
                throw new IOException("分块下载的字节数不符，期望 " + total + "，实际 " + received);
            }

            Log.debug("分块下载完成：" + target.getFileName() + "，共 " + received + " 字节，分段数 " + segments);

            return received;
        } finally {
            pool.shutdownNow();
        }
    }

    /**
     * 下载一个分段，失败时最多重试 maxAttempts 次（每次从断点继续）
     */
    static void downloadSegment(HostPort hp, String path, AppConfig config, RandomAccessFile file, FileChannel channel,
                                long start, long end, AtomicLong downloaded, Progress callback, Object callbackLock,
                                long total, int maxAttempts) throws Exception {
        // cursor[0] 是这一段已经写到的位置，失败重试时从它继续
        long[] cursor = {start};
        long[] position = {start};
        Exception lastError = null;

        for (int attempt = 1; attempt <= maxAttempts; attempt++) {
            try {
                transferRange(hp, path, config, file, channel, cursor, position, end,
                        downloaded, callback, callbackLock, total);

                if (cursor[0] != end + 1) {
                    throw new IOException("分段下载不完整，写到 " + cursor[0] + "，应该写到 " + (end + 1));
                }

                return;
            } catch (Exception e) {
                lastError = e;

                // 注意：这里不能回退进度。重试是从 cursor[0] 断点续传的，
                // 已经写进文件的字节不会被重新下载，回退会让计数比实际少，导致最后的字节数校验失败
                if (attempt < maxAttempts) {
                    Log.warn("分段 " + start + "-" + end + " 第 " + attempt + " 次尝试失败，准备重试："
                            + e.getMessage());

                    try {
                        Thread.sleep(200L * attempt);
                    } catch (InterruptedException ie) {
                        Thread.currentThread().interrupt();
                        throw ie;
                    }
                }
            }
        }

        throw new IOException("分段 " + start + "-" + end + " 重试 " + maxAttempts + " 次后仍然失败", lastError);
    }

    /**
     * 用一个 GET 请求把 [cursor, end] 这段数据写进文件的对应偏移<p>
     * 每调用一次都会新建一条 TCP 连接（v1 不做多路复用，一个连接一次只处理一个请求）<p>
     * 只有把数据完整写进文件之后才推进 cursor 和 position，这样中途出错重试时不会丢字节
     */
    static void transferRange(HostPort hp, String path, AppConfig config, RandomAccessFile file, FileChannel channel,
                              long[] cursor, long[] position, long end, AtomicLong downloaded, Progress callback,
                              Object callbackLock, long total) throws IOException {
        try (Connection connection = new Connection(hp, config)) {
            connection.request("GET " + cursor[0] + " " + end + " " + path);

            long length = connection.readOkLength();

            // 服务端返回越界的数据会污染相邻分段，必须拦住
            if (end >= 0 && length > 0 && cursor[0] + length - 1 > end) {
                throw new IOException("服务端返回的数据超出请求区间：请求 " + cursor[0] + "-" + end
                        + "，实际返回 " + length + " 字节");
            }

            // 整文件下载时再兜一层，防止服务端虚报长度把文件撑坏
            if (total > 0 && length > 0 && position[0] + length > total) {
                throw new IOException("服务端返回的数据超过文件总大小：" + (position[0] + length) + " > " + total);
            }

            if (length > 0) {
                ensureLength(file, position[0] + length);
            }

            byte[] buffer = new byte[BUFFER_SIZE];
            long remaining = length;

            while (remaining > 0) {
                int want = (int) Math.min((long) buffer.length, remaining);
                int n = readSome(connection.in, buffer, want);

                if (n <= 0) {
                    throw new IOException("连接在收完数据前就断了，还应该收到 " + remaining + " 字节");
                }

                ByteBuffer byteBuffer = ByteBuffer.wrap(buffer, 0, n);
                long writePosition = position[0];

                while (byteBuffer.hasRemaining()) {
                    writePosition += channel.write(byteBuffer, writePosition);
                }

                position[0] += n;
                cursor[0] += n;
                remaining -= n;

                if (callback != null) {
                    long now = downloaded.addAndGet(n);

                    // 多个线程会并发回调，这里串行化一下，保证上层（UI、测速统计）不用考虑线程安全
                    synchronized (callbackLock) {
                        callback.on(n, now, total);
                    }
                }
            }
        }
    }

    /**
     * 保证文件至少有 length 这么长（只扩不缩）<p>
     * 定位写本身会自动扩展文件，这里预分配是为了避免边写边扩导致的碎片
     */
    static void ensureLength(RandomAccessFile file, long length) throws IOException {
        synchronized (PREALLOC_LOCK) {
            if (file.length() < length) {
                file.setLength(length);
            }
        }
    }

    /**
     * 读满 len 个字节，遇到流结束就提前返回实际读到的字节数
     */
    static int readSome(InputStream in, byte[] buffer, int len) throws IOException {
        int offset = 0;

        while (offset < len) {
            int n = in.read(buffer, offset, len - offset);

            if (n < 0) {
                break;
            }

            offset += n;
        }

        return offset;
    }

    static void createParentDirectories(Path target) throws IOException {
        Path parent = target.getParent();

        if (parent != null) {
            Files.createDirectories(parent);
        }
    }

    static void deleteQuietly(Path path) {
        try {
            Files.deleteIfExists(path);
        } catch (IOException ignored) {
        }
    }

    // ------------------------------------------------------------------
    // 连接
    // ------------------------------------------------------------------

    /**
     * 一条 MSFP 连接<p>
     * v1 不做多路复用：一次只发一个请求，收到完整响应后再发下一个
     */
    static class Connection implements Closeable {
        final Socket socket;
        final BufferedInputStream in;
        final BufferedOutputStream out;

        Connection(HostPort hp, AppConfig config) throws IOException {
            int timeout = Math.max(5000, config.tcpTimeout);

            socket = new Socket();

            try {
                socket.setTcpNoDelay(true);
                socket.connect(new InetSocketAddress(hp.host, hp.port), timeout);
                socket.setSoTimeout(timeout);

                in = new BufferedInputStream(socket.getInputStream(), BUFFER_SIZE);
                out = new BufferedOutputStream(socket.getOutputStream(), 1 << 12);
            } catch (IOException e) {
                close();
                throw new IOException("连接更新服务器失败 " + hp + "：" + e.getMessage(), e);
            }
        }

        /**
         * 发一行请求（UTF-8，以 \n 结尾）
         */
        void request(String line) throws IOException {
            out.write(line.getBytes(StandardCharsets.UTF_8));
            out.write('\n');
            out.flush();
        }

        /**
         * 读一行响应文本（不含换行符），连接提前关闭时返回 null
         */
        String readLine() throws IOException {
            ByteArrayOutputStream buffer = new ByteArrayOutputStream(64);
            int b;

            while ((b = in.read()) >= 0) {
                if (b == '\n') {
                    return new String(buffer.toByteArray(), StandardCharsets.UTF_8);
                }

                if (b != '\r') {
                    buffer.write(b);
                }

                if (buffer.size() > MAX_LINE) {
                    throw new IOException("服务端返回的状态行过长，可能不是 MSFP 协议");
                }
            }

            if (buffer.size() == 0) {
                return null;
            }

            return new String(buffer.toByteArray(), StandardCharsets.UTF_8);
        }

        /**
         * 读取 OK 响应头，返回后面的数据长度；ERR 会抛异常
         */
        long readOkLength() throws IOException {
            String line = readLine();

            if (line == null) {
                throw new IOException("服务端没有响应就关闭了连接");
            }

            if (line.equals("ERR") || line.startsWith("ERR ")) {
                String message = line.length() > 4 ? line.substring(4).trim() : "";
                throw new ServerErrorException("服务端返回错误：" + (message.isEmpty() ? "未知原因" : message));
            }

            if (!line.startsWith("OK")) {
                throw new IOException("无法识别的响应：" + line);
            }

            String value = line.substring(2).trim();

            long length;

            try {
                length = Long.parseLong(value);
            } catch (NumberFormatException e) {
                throw new IOException("响应里的长度不合法：" + line);
            }

            if (length < 0) {
                throw new IOException("响应里的长度是负数：" + line);
            }

            return length;
        }

        /**
         * 把接下来的 length 个字节全部读进输出流
         */
        void readAll(OutputStream target, long length) throws IOException {
            byte[] buffer = new byte[BUFFER_SIZE];
            long remaining = length;

            while (remaining > 0) {
                int want = (int) Math.min((long) buffer.length, remaining);
                int n = readSome(in, buffer, want);

                if (n <= 0) {
                    throw new IOException("连接在收完数据前就断了，还应该收到 " + remaining + " 字节");
                }

                target.write(buffer, 0, n);
                remaining -= n;
            }
        }

        @Override
        public void close() {
            try {
                socket.close();
            } catch (IOException ignored) {
            }
        }
    }

    /**
     * 下载线程工厂，线程都起成守护线程，避免拖住 JVM 退出
     */
    static class DownloadThreadFactory implements ThreadFactory {
        final AtomicInteger counter = new AtomicInteger();

        @Override
        public Thread newThread(Runnable runnable) {
            Thread thread = new Thread(runnable, "msfp-download-" + counter.incrementAndGet());
            thread.setDaemon(true);

            return thread;
        }
    }
}
