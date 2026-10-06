package com.github.balloonupdate.mcpatch.client.sync;

import com.github.balloonupdate.mcpatch.client.config.AppConfig;
import com.github.balloonupdate.mcpatch.client.logging.Log;
import com.github.balloonupdate.mcpatch.client.network.impl.HttpProtocol;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.ResponseBody;
import okio.BufferedSource;

import java.io.IOException;
import java.io.RandomAccessFile;
import java.net.URI;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;

/**
 * 清单里 CDN 直链（http/https 绝对地址）的下载器<p>
 * 与 MSFP 的下载语义保持一致：先探测文件大小，超过 download-threshold 就按 download-threads
 * 切成多段、每段一条独立的 HTTP 连接并发下载（依赖 CDN 的 Range 支持，Modrinth 支持），
 * 每个分段失败时从断点续传重试，整体失败时删除半成品文件并抛异常，由调用方回退到更新源<p>
 * 本类只负责「把 url 的内容完整地写进 target」，SHA-256 校验仍由上层 SyncEngine 统一做
 */
public class CdnDownloader {
    /**
     * 单次读取的缓冲区大小
     */
    static final int BUFFER_SIZE = 1 << 16;

    /**
     * 单个分段的最小字节数<p>
     * download-threads 默认 32，如果只按文件大小是否超过 download-threshold 切段，
     * 一个 1MB 的文件也会被切成 32 段、开 32 条连接，反而拖慢并且浪费连接；
     * 这里额外限制分段总数，保证每段至少有这么多字节
     */
    static final long MIN_SEGMENT_BYTES = 256 * 1024;

    /**
     * 每个分段的下载重试次数上限（与 MSFP 分块下载保持一致）
     */
    static final int FRAGMENT_RETRIES = 2;

    /**
     * 预分配文件长度用的全局锁，理由同 TcpFileClient.PREALLOC_LOCK
     */
    static final Object PREALLOC_LOCK = new Object();

    /**
     * 判断一个下载地址是不是可以直接走 HTTP(S) 的绝对地址
     */
    public static boolean isHttpUrl(String url) {
        if (url == null) {
            return false;
        }

        String value = url.trim().toLowerCase(java.util.Locale.ROOT);

        return value.startsWith("http://") || value.startsWith("https://");
    }

    /**
     * 从 URL 里取出主机名，取不到就原样返回，只用于日志
     */
    public static String hostOf(String url) {
        if (url == null) {
            return "";
        }

        try {
            String host = URI.create(url.trim()).getHost();

            if (host != null && !host.isEmpty()) {
                return host;
            }
        } catch (Exception ignored) {
        }

        return url.trim();
    }

    /**
     * 用 HTTP(S) 把一个文件完整下载到 target<p>
     * 失败时抛异常（调用方据此回退到更新源）；失败路径上不会留下半成品文件
     *
     * @param url      http/https 绝对地址
     * @param target   目标文件
     * @param config   配置（cdn-timeout、download-threads、ignore-ssl-cert、http-headers）
     * @param callback 进度回调，语义与 MSFP 完全一致
     */
    public static void download(String url, Path target, AppConfig config, TcpFileClient.Progress callback) throws Exception {
        download(url, target, config, Math.max(1, config.downloadThreads), callback);
    }

    /**
     * 同上，但显式指定分段线程数<p>
     * 测速时如果因为 CDN 慢而提过线程数并据此判定 CDN 够快，真实下载要用同一个线程数，
     * 否则会「测出来快、下载还是慢」
     *
     * @param threads 分段线程数（&lt;=1 表示单连接）
     */
    public static void download(String url, Path target, AppConfig config, int threads,
                                TcpFileClient.Progress callback) throws Exception {
        if (!isHttpUrl(url)) {
            throw new IOException("不是 http/https 地址：" + url);
        }

        Path parent = target.getParent();

        if (parent != null) {
            Files.createDirectories(parent);
        }

        OkHttpClient client = newClient(config);

        // 探测文件大小：请求第 0 个字节，支持 Range 的 CDN 会返回 206 + Content-Range: bytes 0-0/总大小；
        // 不支持 Range 的会返回 200 并且响应体就是整个文件，那正好直接用这一条连接写完
        Response probe = null;

        try {
            probe = client.newCall(buildRequest(url, 0, 0, config)).execute();

            checkStatus(probe, url);

            if (probe.code() != 206) {
                deleteQuietly(target);

                try (ResponseBody body = probe.body(); RandomAccessFile file = new RandomAccessFile(target.toFile(), "rw")) {
                    file.setLength(0);

                    long expected = body.contentLength();
                    long received = copy(body.source(), file, expected, callback);

                    Log.debug("CDN 不支持 Range，已用单连接下载：" + target.getFileName() + "，共 " + received + " 字节");

                    return;
                }
            }

            long total = parseTotal(probe.header("Content-Range"));

            if (total < 0) {
                throw new IOException("无法从 Content-Range 解析文件大小：" + probe.header("Content-Range"));
            }

            if (total == 0) {
                deleteQuietly(target);
                Files.createFile(target);

                return;
            }

            int segments = planSegments(total, threads, config);

            if (segments <= 1) {
                Log.debug("使用单连接下载（CDN）：" + target.getFileName() + "，文件大小 " + total
                        + " 字节，阈值 " + Math.max(0L, config.downloadThreshold) + " 字节 <- " + url);
            } else {
                Log.debug("启用多线程分块下载（CDN）：" + target.getFileName() + "，文件大小 " + total
                        + " 字节，分段数 " + segments + " <- " + url);
            }

            deleteQuietly(target);

            try (RandomAccessFile file = new RandomAccessFile(target.toFile(), "rw")) {
                file.setLength(total);

                if (segments <= 1) {
                    AtomicLong downloaded = new AtomicLong();

                    // 整文件也算一个分段，同样享受重试与断点续传
                    downloadSegment(client, url, config, file.getChannel(), new long[]{0}, total - 1,
                            downloaded, callback, new Object(), total, maxAttempts(config));
                } else {
                    downloadSegmented(client, url, target, file, config, total, segments, callback);
                }
            } catch (Exception e) {
                deleteQuietly(target);
                throw e;
            }
        } finally {
            if (probe != null) {
                probe.close();
            }
        }
    }

    /**
     * 按文件大小和分段线程数算出分段数，同时保证每段不少于 MIN_SEGMENT_BYTES
     */
    static int planSegments(long total, int threads, AppConfig config) {
        if (total <= Math.max(0L, config.downloadThreshold)) {
            return 1;
        }

        long bySize = Math.max(1L, total / MIN_SEGMENT_BYTES);

        return (int) Math.max(1L, Math.min(Math.max(1L, threads), Math.min(total, bySize)));
    }

    static int maxAttempts(AppConfig config) {
        int reties = Math.max(1, config.reties);

        return reties > 1 ? FRAGMENT_RETRIES : 1;
    }

    /**
     * 多线程分块下载：把文件切成 segments 段，每段一条独立的 HTTP 连接并发写进同一个文件的不同偏移
     */
    static void downloadSegmented(OkHttpClient client, String url, Path target, RandomAccessFile file, AppConfig config,
                                  long total, int segments, TcpFileClient.Progress callback) throws Exception {
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

        int maxAttempts = maxAttempts(config);

        ExecutorService pool = Executors.newFixedThreadPool(segments, new DownloadThreadFactory());
        CountDownLatch latch = new CountDownLatch(segments);

        try {
            FileChannel channel = file.getChannel();

            for (int i = 0; i < segments; i++) {
                final long start = starts[i];
                final long end = ends[i];

                pool.execute(() -> {
                    try {
                        downloadSegment(client, url, config, channel, new long[]{start}, end, downloaded, callback,
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
                deleteQuietly(target);
                throw error;
            }

            long received = downloaded.get();

            if (received != total) {
                deleteQuietly(target);
                throw new IOException("分块下载的字节数不符，期望 " + total + "，实际 " + received);
            }

            Log.debug("分块下载完成（CDN）：" + target.getFileName() + "，共 " + received + " 字节，分段数 " + segments);
        } finally {
            pool.shutdownNow();
        }
    }

    /**
     * 下载 [cursor[0], end] 这一段，失败时最多重试 maxAttempts 次（每次从断点继续）
     */
    static void downloadSegment(OkHttpClient client, String url, AppConfig config, FileChannel channel, long[] cursor,
                                long end, AtomicLong downloaded, TcpFileClient.Progress callback, Object callbackLock,
                                long total, int maxAttempts) throws Exception {
        Exception lastError = null;

        for (int attempt = 1; attempt <= maxAttempts; attempt++) {
            try {
                transferRange(client, url, config, channel, cursor, end, downloaded, callback, callbackLock, total);

                if (cursor[0] != end + 1) {
                    throw new IOException("分段下载不完整，写到 " + cursor[0] + "，应该写到 " + (end + 1));
                }

                return;
            } catch (Exception e) {
                lastError = e;

                // 与 MSFP 一致：不回退进度计数，重试是断点续传，已经写进文件的字节不会重下
                if (attempt < maxAttempts) {
                    Log.warn("CDN 分段 " + cursor[0] + "-" + end + " 第 " + attempt + " 次尝试失败，准备重试："
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

        throw new IOException("CDN 分段重试 " + maxAttempts + " 次后仍然失败", lastError);
    }

    /**
     * 发一个 Range 请求，把 [cursor[0], end] 的数据写进文件的对应偏移<p>
     * 每调用一次新建一条 HTTP 连接；只有把数据真正写进文件之后才推进 cursor
     */
    static void transferRange(OkHttpClient client, String url, AppConfig config, FileChannel channel, long[] cursor,
                              long end, AtomicLong downloaded, TcpFileClient.Progress callback, Object callbackLock,
                              long total) throws IOException {
        try (Response rsp = client.newCall(buildRequest(url, cursor[0], end, config)).execute()) {
            checkStatus(rsp, url);

            if (rsp.code() != 206) {
                throw new IOException("CDN 没有按 Range 返回（HTTP " + rsp.code() + "）：" + url);
            }

            try (ResponseBody body = rsp.body()) {
                BufferedSource source = body.source();
                byte[] buffer = new byte[BUFFER_SIZE];
                long remaining = end - cursor[0] + 1;

                while (remaining > 0) {
                    int want = (int) Math.min((long) buffer.length, remaining);
                    int n = source.read(buffer, 0, want);

                    if (n <= 0) {
                        throw new IOException("连接在收完数据前就断了，还应该收到 " + remaining + " 字节");
                    }

                    ByteBuffer byteBuffer = ByteBuffer.wrap(buffer, 0, n);
                    long writePosition = cursor[0];

                    while (byteBuffer.hasRemaining()) {
                        writePosition += channel.write(byteBuffer, writePosition);
                    }

                    cursor[0] += n;
                    remaining -= n;

                    if (callback != null) {
                        long now = downloaded.addAndGet(n);

                        // 多个分段会并发回调，这里串行化，保证上层不用考虑线程安全
                        synchronized (callbackLock) {
                            callback.on(n, now, total);
                        }
                    }
                }
            }
        }
    }

    /**
     * 顺序把整个响应体写进文件，返回写入的字节数
     */
    static long copy(BufferedSource source, RandomAccessFile file, long expected, TcpFileClient.Progress callback)
            throws IOException {
        byte[] buffer = new byte[BUFFER_SIZE];
        long received = 0;
        long lastReported = 0;

        while (true) {
            int n = source.read(buffer, 0, buffer.length);

            if (n <= 0) {
                break;
            }

            file.write(buffer, 0, n);
            received += n;

            if (callback != null) {
                long batch = received - lastReported;

                lastReported = received;
                callback.on(batch, received, expected);
            }
        }

        if (expected >= 0 && received != expected) {
            throw new IOException("下载的字节数不符，期望 " + expected + "，实际 " + received);
        }

        return received;
    }

    /**
     * 从 Content-Range 头（bytes 0-0/12345）里解析文件总大小，解析不出来返回 -1
     */
    static long parseTotal(String contentRange) {
        if (contentRange == null) {
            return -1;
        }

        int slash = contentRange.lastIndexOf('/');

        if (slash < 0) {
            return -1;
        }

        String value = contentRange.substring(slash + 1).trim();

        if (value.isEmpty() || value.equals("*")) {
            return -1;
        }

        try {
            return Long.parseLong(value);
        } catch (NumberFormatException e) {
            return -1;
        }
    }

    /**
     * 状态码不是 2xx 时抛出异常（4xx/5xx 都属于这里），调用方会回退到更新源
     */
    static void checkStatus(Response rsp, String url) throws IOException {
        int code = rsp.code();

        if (code < 200 || code >= 300) {
            throw new IOException("CDN 返回 HTTP " + code + "：" + url);
        }
    }

    /**
     * 构造请求，start &gt;= 0 时带上 Range 头（闭区间），并附加配置里的自定义 headers
     */
    static Request buildRequest(String url, long start, long end, AppConfig config) {
        Request.Builder builder = new Request.Builder().url(url);

        if (start >= 0 && end >= start) {
            builder.addHeader("Range", "bytes=" + start + "-" + end);
        }

        if (config.httpHeaders != null) {
            for (java.util.Map.Entry<String, String> e : config.httpHeaders.entrySet()) {
                builder.addHeader(e.getKey(), e.getValue());
            }
        }

        return builder.build();
    }

    /**
     * 按 CDN 下载的超时与证书配置建一个 client<p>
     * 只设连接/读/写超时，不设 callTimeout：大文件只要还在持续收数据就不该被整条请求的超时打断
     */
    static OkHttpClient newClient(AppConfig config) {
        int timeout = Math.max(1000, config.cdnTimeout);

        return newBuilder(config)
                .connectTimeout(timeout, TimeUnit.MILLISECONDS)
                .readTimeout(timeout, TimeUnit.MILLISECONDS)
                .writeTimeout(timeout, TimeUnit.MILLISECONDS)
                .build();
    }

    /**
     * 建一个专门给测速用的 client<p>
     * 和下载用的 client 只差在超时：测速只跑 speedtest-duration-ms 那么久，读超时也压到同一量级，
     * 免得某个卡住的连接把整轮测速拖到 cdn-timeout（默认 15 秒）那么长
     */
    static OkHttpClient newProbeClient(AppConfig config, long durationNanos) {
        long probeMillis = Math.max(1000L, durationNanos / 1_000_000L);
        int readTimeout = (int) Math.max(1000L, Math.min(Math.max(1000, config.cdnTimeout), probeMillis));

        return newBuilder(config)
                .connectTimeout((int) Math.max(1000L, Math.min(Math.max(1000, config.cdnTimeout), probeMillis)),
                        TimeUnit.MILLISECONDS)
                .readTimeout(readTimeout, TimeUnit.MILLISECONDS)
                .writeTimeout((int) Math.max(1000L, Math.min(Math.max(1000, config.cdnTimeout), probeMillis)),
                        TimeUnit.MILLISECONDS)
                .build();
    }

    /**
     * 公共的 client 构造器（证书配置在这一层统一处理）
     */
    static OkHttpClient.Builder newBuilder(AppConfig config) {
        OkHttpClient.Builder builder = new OkHttpClient.Builder();

        if (config.ignoreSSLCertificate) {
            HttpProtocol.IgnoreSSLCert ignore = new HttpProtocol.IgnoreSSLCert();

            builder.sslSocketFactory(ignore.context.getSocketFactory(), ignore.trustManager);
            builder.hostnameVerifier((hostname, session) -> true);
        }

        return builder;
    }

    static void deleteQuietly(Path path) {
        try {
            Files.deleteIfExists(path);
        } catch (IOException ignored) {
        }
    }

    /**
     * 下载线程工厂，线程都起成守护线程，避免拖住 JVM 退出
     */
    static class DownloadThreadFactory implements ThreadFactory {
        final AtomicInteger counter = new AtomicInteger();

        @Override
        public Thread newThread(Runnable runnable) {
            Thread thread = new Thread(runnable, "cdn-download-" + counter.incrementAndGet());
            thread.setDaemon(true);

            return thread;
        }
    }
}
