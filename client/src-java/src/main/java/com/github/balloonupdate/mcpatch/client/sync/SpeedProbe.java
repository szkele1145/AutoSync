package com.github.balloonupdate.mcpatch.client.sync;

import com.github.balloonupdate.mcpatch.client.config.AppConfig;
import com.github.balloonupdate.mcpatch.client.logging.Log;
import okhttp3.OkHttpClient;
import okhttp3.Response;
import okhttp3.ResponseBody;
import okio.BufferedSource;

import java.io.BufferedOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;

/**
 * 单来源的吞吐测速：在限定的时间窗口里，用多条并发连接尽量多地下一点数据，换算出 MB/s<p>
 * 两个来源（CDN 直链、MSFP 服务端）各有一套实现，但测法完全对称，保证可以互相比较：<p>
 * 1. 每条连接读文件里互不重叠的一段（Range / MSFP 的 GET start end）<p>
 * 2. 每条连接最多读 {@link #PER_CONNECTION_BYTES}，所有连接加起来最多读 {@link #TOTAL_BYTES}，
 *    到 speedtest-duration-ms 也立即停手 —— 三个上限谁先到算谁，保证测速不会把玩家的流量吃光<p>
 * 3. 速度按「从发起测速到收完最后一个字节」这个窗口算平均吞吐（总字节 ÷ 窗口时长），
 *    两个来源用完全相同的口径与连接数，所以结果可以直接互相比较<p>
 * 4. 收到的数据写进临时文件（调用方保证临时目录会被删掉），绝不碰用户的任何真实文件<p>
 * 失败时抛异常，由调用方决定降级策略；只要还有字节被读到就不算失败<p>
 * 注意：窗口刻意不从「第一个字节到达」开始算。慢速源在 1.5 秒里可能每条连接只读满一个缓冲区，
 * 那样「首字节 → 末字节」会缩成几毫秒，算出来的速度会虚高几十倍；从发起请求算起则始终稳定，
 * 代价只是把建连与首字节等待（几十毫秒量级）也算进了分母，对两个来源是一视同仁的
 */
public class SpeedProbe {
    /**
     * 单条连接在测速窗口里最多读多少字节<p>
     * 有了它，一个非常快的 CDN 也不会因为「2 秒 × 32 条连接」被拉走几百 MB
     */
    public static final long PER_CONNECTION_BYTES = 1024 * 1024;

    /**
     * 一轮测速从某一个来源最多读多少字节（所有连接合起来）<p>
     * 默认配置下每次测速的额外流量上限：CDN 8MB + 服务端 8MB，提线程重测再加 8MB
     */
    public static final long TOTAL_BYTES = 8L * 1024 * 1024;

    /**
     * 读写缓冲区大小
     */
    static final int BUFFER_SIZE = 1 << 16;

    /**
     * 单次实际读多少字节<p>
     * 刻意取得比缓冲区小：速度窗口是「发起测速 → 最后一个字节」，如果一次读一大块，
     * 慢速源可能整轮只读满一块，字节计数就会太粗（大块头之间的空档时间被忽略掉）
     */
    static final int READ_CHUNK = 8 * 1024;

    /**
     * 多连接共用的额度与时间统计<p>
     * 额度是全局的：所有连接都从这里申请本次能读多少字节，申请不到就停下来，
     * 这样即使配了 64 条连接也不会把总额度读超
     */
    static class Budget {
        /**
         * 还没被申请走的字节数
         */
        final AtomicLong remaining;

        /**
         * 所有连接真正读到的字节总数
         */
        final AtomicLong received = new AtomicLong();

        /**
         * 测速开始的时间点（纳秒），速度窗口从这里算起
         */
        final long startNanos = System.nanoTime();

        /**
         * 最后一个字节到达的时间点（纳秒），一个字节都没收到时是 0
         */
        final AtomicLong lastByte = new AtomicLong(0);

        Budget(long total) {
            this.remaining = new AtomicLong(Math.max(1L, total));
        }

        /**
         * 申请 want 个字节的额度，返回实际批下来的字节数（0 表示额度用完，该收手了）
         */
        long take(long want) {
            while (true) {
                long available = remaining.get();

                if (available <= 0) {
                    return 0;
                }

                long grant = Math.min(available, Math.max(0L, want));

                if (grant <= 0) {
                    return 0;
                }

                if (remaining.compareAndSet(available, available - grant)) {
                    return grant;
                }
            }
        }

        /**
         * 批下来的额度没用完（对端这次只送来这么多），把差额还回去
         */
        void refund(long bytes) {
            if (bytes > 0) {
                remaining.addAndGet(bytes);
            }
        }

        /**
         * 记一笔真正收到的数据
         */
        void commit(long bytes) {
            if (bytes <= 0) {
                return;
            }

            received.addAndGet(bytes);
            lastByte.set(System.nanoTime());
        }

        /**
         * 速度窗口的长度（纳秒）：从发起测速到最后一个字节到达
         */
        long elapsedNanos() {
            long last = lastByte.get();

            if (last <= 0 || last <= startNanos) {
                return 0;
            }

            return last - startNanos;
        }

        TcpFileClient.SpeedSample toSample() {
            return new TcpFileClient.SpeedSample(received.get(), elapsedNanos());
        }
    }

    // ------------------------------------------------------------------
    // CDN（HTTP Range）
    // ------------------------------------------------------------------

    /**
     * 对一条 CDN 直链测速<p>
     * 先探测它支不支持 Range：支持就用 connections 条连接并发读不同区段；
     * 不支持就退化成单连接读一小段（同样有时间与字节上限），不会把整个文件拉下来
     *
     * @param url           http/https 绝对地址
     * @param fileSize      清单里声明的文件大小，&lt;= 0 时用 Content-Range 现场探测
     * @param connections   并发连接数（等于本次实际要用的分块线程数）
     * @param tempDir       临时目录（调用方负责删除）
     * @param prefix        临时文件前缀
     * @param durationNanos 测速时长
     * @return 收到的字节数与传输耗时；一个字节都没读到且出过错时抛异常
     */
    public static TcpFileClient.SpeedSample measureCdn(String url, long fileSize, int connections, Path tempDir,
                                                       String prefix, long durationNanos, AppConfig config)
            throws Exception {
        if (!CdnDownloader.isHttpUrl(url)) {
            throw new IOException("不是 http/https 地址：" + url);
        }

        int threads = Math.max(1, connections);
        long deadline = System.nanoTime() + Math.max(1_000_000L, durationNanos);
        OkHttpClient client = CdnDownloader.newProbeClient(config, durationNanos);

        // 先探测 Range 支持与真实大小：支持 Range 的 CDN 会回 206 + Content-Range: bytes 0-0/总大小
        boolean ranged;

        Response probe = client.newCall(CdnDownloader.buildRequest(url, 0, 0, config)).execute();

        try {
            CdnDownloader.checkStatus(probe, url);

            ranged = probe.code() == 206;

            if (ranged && fileSize <= 0) {
                fileSize = CdnDownloader.parseTotal(probe.header("Content-Range"));
            }
        } finally {
            probe.close();
        }

        if (!ranged) {
            Log.debug("测速：CDN 不支持 Range，退化成单连接读一段：" + CdnDownloader.hostOf(url));

            return readWholeBody(client, url, new Budget(TOTAL_BYTES), deadline,
                    tempDir.resolve(prefix + "-0.seg"), config);
        }

        if (fileSize <= 0) {
            throw new IOException("无法确定 CDN 上文件的大小，无法测速：" + url);
        }

        // 每条连接负责文件里独立的一段：步长按文件大小平分，个别连接的实际读取量再压到单连接上限
        long stride = Math.max(1L, fileSize / threads);
        long perConnection = Math.max(1L, Math.min(PER_CONNECTION_BYTES, stride));

        // 计时从真正开始拉数据算起，前面那次 Range 探测不算进窗口
        Budget budget = new Budget(TOTAL_BYTES);

        AtomicReference<Exception> failure = new AtomicReference<>();
        ExecutorService pool = Executors.newFixedThreadPool(threads, new CdnDownloader.DownloadThreadFactory());
        CountDownLatch latch = new CountDownLatch(threads);

        try {
            for (int i = 0; i < threads; i++) {
                final int index = i;
                final long start = (long) i * stride;

                // 文件比连接数还小的时候，多出来的连接直接空转
                if (start >= fileSize) {
                    latch.countDown();
                    continue;
                }

                final long end = Math.min(start + perConnection - 1, fileSize - 1);

                pool.execute(() -> {
                    try {
                        readRange(client, url, tempDir.resolve(prefix + "-" + index + ".seg"), start, end,
                                budget, deadline, config);
                    } catch (Exception e) {
                        failure.compareAndSet(null, e);
                    } finally {
                        latch.countDown();
                    }
                });
            }

            latch.await();
        } finally {
            pool.shutdownNow();
        }

        return finish(budget, failure.get());
    }

    /**
     * 读 CDN 的一个区段并写进临时文件，读到额度用完或到点为止
     */
    static void readRange(OkHttpClient client, String url, Path temp, long start, long end, Budget budget,
                          long deadline, AppConfig config) throws IOException {
        Files.createDirectories(temp.getParent());

        try (Response rsp = client.newCall(CdnDownloader.buildRequest(url, start, end, config)).execute();
             OutputStream out = new BufferedOutputStream(Files.newOutputStream(temp), BUFFER_SIZE)) {
            CdnDownloader.checkStatus(rsp, url);

            if (rsp.code() != 206) {
                throw new IOException("CDN 没有按 Range 返回（HTTP " + rsp.code() + "）：" + url);
            }

            try (ResponseBody body = rsp.body()) {
                transfer(body.source(), out, end - start + 1, budget, deadline);
            }
        }
    }

    /**
     * CDN 不支持 Range 时的兜底：单连接从头读一小段
     */
    static TcpFileClient.SpeedSample readWholeBody(OkHttpClient client, String url, Budget budget, long deadline,
                                                   Path temp, AppConfig config) throws IOException {
        Files.createDirectories(temp.getParent());

        try (Response rsp = client.newCall(CdnDownloader.buildRequest(url, -1, -1, config)).execute();
             OutputStream out = new BufferedOutputStream(Files.newOutputStream(temp), BUFFER_SIZE)) {
            CdnDownloader.checkStatus(rsp, url);

            try (ResponseBody body = rsp.body()) {
                transfer(body.source(), out, PER_CONNECTION_BYTES, budget, deadline);
            }
        }

        return budget.toSample();
    }

    /**
     * 从 HTTP 响应体里搬数据到临时文件，最多 limit 字节，同时受全局额度与截止时间约束
     */
    static void transfer(BufferedSource source, OutputStream out, long limit, Budget budget, long deadline)
            throws IOException {
        byte[] buffer = new byte[BUFFER_SIZE];
        long remaining = limit;

        while (remaining > 0) {
            if (System.nanoTime() >= deadline) {
                break;
            }

            long grant = budget.take(Math.min(READ_CHUNK, remaining));

            if (grant <= 0) {
                break;
            }

            int n = source.read(buffer, 0, (int) grant);

            if (n <= 0) {
                break;
            }

            // 批多了没用完的额度还回去，免得某个慢连接白占额度拖慢其它连接
            budget.refund(grant - n);

            out.write(buffer, 0, n);
            budget.commit(n);

            remaining -= n;
        }
    }

    // ------------------------------------------------------------------
    // 服务端（MSFP）
    // ------------------------------------------------------------------

    /**
     * 对 MSFP 更新源上一个文件测速，测法与 CDN 侧完全对称（同连接数、同时长、同样的字节上限）
     *
     * @param fileSize 清单里声明的文件大小，&lt;= 0 时先发 SIZE 问服务端
     */
    public static TcpFileClient.SpeedSample measureServer(TcpFileClient.HostPort hp, String path, long fileSize,
                                                          int connections, Path tempDir, String prefix,
                                                          long durationNanos, AppConfig config) throws Exception {
        int threads = Math.max(1, connections);
        long deadline = System.nanoTime() + Math.max(1_000_000L, durationNanos);

        // 测速连接用完就该断，读超时压到测速时长的量级（Connection 内部另有 5 秒的下限保护）
        AppConfig probeConfig = timeoutConfig(config, durationNanos);

        if (fileSize <= 0) {
            fileSize = TcpFileClient.size(hp, path, probeConfig);
        }

        if (fileSize <= 0) {
            throw new IOException("服务端上的文件大小为 " + fileSize + "，无法测速：" + path);
        }

        long stride = Math.max(1L, fileSize / threads);
        long perConnection = Math.max(1L, Math.min(PER_CONNECTION_BYTES, stride));

        // 计时从真正开始拉数据算起，前面那次 SIZE 查询不算进窗口
        Budget budget = new Budget(TOTAL_BYTES);

        AtomicReference<Exception> failure = new AtomicReference<>();
        ExecutorService pool = Executors.newFixedThreadPool(threads, new ProbeThreadFactory());
        CountDownLatch latch = new CountDownLatch(threads);

        try {
            for (int i = 0; i < threads; i++) {
                final int index = i;
                final long start = (long) i * stride;

                if (start >= fileSize) {
                    latch.countDown();
                    continue;
                }

                final long end = Math.min(start + perConnection - 1, fileSize - 1);

                pool.execute(() -> {
                    try {
                        readSegment(hp, path, probeConfig, start, end, budget, deadline,
                                tempDir.resolve(prefix + "-" + index + ".seg"));
                    } catch (Exception e) {
                        failure.compareAndSet(null, e);
                    } finally {
                        latch.countDown();
                    }
                });
            }

            latch.await();
        } finally {
            pool.shutdownNow();
        }

        return finish(budget, failure.get());
    }

    /**
     * 用一条 MSFP 连接读一个区段并写进临时文件
     */
    static void readSegment(TcpFileClient.HostPort hp, String path, AppConfig config, long start, long end,
                            Budget budget, long deadline, Path temp) throws IOException {
        Files.createDirectories(temp.getParent());

        try (TcpFileClient.Connection connection = new TcpFileClient.Connection(hp, config);
             OutputStream out = new BufferedOutputStream(Files.newOutputStream(temp), BUFFER_SIZE)) {
            connection.request("GET " + start + " " + end + " " + path);

            long length = connection.readOkLength();

            if (length <= 0) {
                return;
            }

            // 服务端返回越界数据会污染测速结果，直接判失败
            if (start + length - 1 > end) {
                throw new IOException("服务端返回的数据超出请求区间：请求 " + start + "-" + end + "，实际 " + length + " 字节");
            }

            byte[] buffer = new byte[BUFFER_SIZE];
            long remaining = length;

            while (remaining > 0) {
                if (System.nanoTime() >= deadline) {
                    break;
                }

                long grant = budget.take(Math.min(READ_CHUNK, remaining));

                if (grant <= 0) {
                    break;
                }

                int n = TcpFileClient.readSome(connection.in, buffer, (int) grant);

                if (n <= 0) {
                    break;
                }

                budget.refund(grant - n);

                out.write(buffer, 0, n);
                budget.commit(n);

                remaining -= n;
            }
        }
    }

    // ------------------------------------------------------------------
    // 公共辅助
    // ------------------------------------------------------------------

    /**
     * 收尾：读到了数据就用它算速度，一个字节都没读到且有过异常时把异常抛出去，
     * 让上层日志能写出「为什么测不出来」（连不上 / 404 / 超时……）
     */
    static TcpFileClient.SpeedSample finish(Budget budget, Exception failure) throws Exception {
        if (budget.received.get() <= 0) {
            if (failure != null) {
                throw failure;
            }

            return new TcpFileClient.SpeedSample(0, 0);
        }

        return budget.toSample();
    }

    /**
     * 造一个只改了超时的配置副本给测速连接用，避免测速被 tcp-timeout（默认 15 秒）拖住
     */
    static AppConfig timeoutConfig(AppConfig config, long durationNanos) {
        AppConfig copy = new AppConfig(new HashMap<>());

        long millis = Math.max(1000L, durationNanos / 1_000_000L);

        copy.tcpTimeout = (int) Math.min(Math.max(1000, config.tcpTimeout), millis);

        return copy;
    }

    /**
     * 测速线程工厂，全部起成守护线程，避免拖住 JVM 退出
     */
    static class ProbeThreadFactory implements java.util.concurrent.ThreadFactory {
        final java.util.concurrent.atomic.AtomicInteger counter = new java.util.concurrent.atomic.AtomicInteger();

        @Override
        public Thread newThread(Runnable runnable) {
            Thread thread = new Thread(runnable, "msfp-speedtest-" + counter.incrementAndGet());
            thread.setDaemon(true);

            return thread;
        }
    }
}
