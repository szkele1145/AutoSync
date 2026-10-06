package com.github.balloonupdate.mcpatch.client.sync;

import com.github.balloonupdate.mcpatch.client.config.AppConfig;
import com.github.balloonupdate.mcpatch.client.logging.Log;
import com.github.balloonupdate.mcpatch.client.utils.BytesUtils;
import com.github.balloonupdate.mcpatch.client.utils.PathUtility;
import org.json.JSONObject;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * CDN / 服务端 自动测速选源<p>
 * 每次同步（只要有文件要下）先挑一个大文件当样本，让 CDN 与服务端<b>并发</b>各下一段，
 * 各自算出一条 MB/s，再按下面的规则决定这次同步走哪边：<p>
 * 1. cdn_speed &gt;= server_speed × cdn-prefer-ratio → 选 CDN（默认比例 0.5，也就是差一倍以内都算够快，
 *    优先省服务端带宽）<p>
 * 2. 否则先把 CDN 的连接数翻倍重测一次（上限 cdn-max-threads），还是不如服务端才选服务端<p>
 * 3. 两边都测不出速度 → 回退到原有行为（先 CDN 后服务端依次尝试）<p>
 * 结果会按 source-cache-seconds（默认 3600 秒）缓存，缓存没过期时本次同步直接复用，不再测速<p>
 * 关于样本：优先用「本次待下载列表里最大的那个文件」；如果一个待下载的大文件都没有（都已是最新），
 * 就退而求其次用「本地已经是最新的那个大文件」当<b>影子样本</b> ——
 * 只从清单里读它的路径/大小/链接，CDN 与服务端各下一小段到<b>临时文件</b>测完即删，
 * 用户的真实文件从头到尾只被读过元信息，绝不会被删除、覆盖或改动
 */
public class SourceModeSelector {
    /**
     * 测速缓存的格式版本
     */
    static final int CACHE_FORMAT = 1;

    /**
     * 测速缓存的相对路径（相对于游戏目录），和更新源测速缓存放在同一个 modsync 目录下
     */
    public static final String CACHE_DIR = "modsync";

    /**
     * 测速缓存的文件名
     */
    public static final String CACHE_FILE_NAME = "source-choice.json";

    /**
     * 本次同步最终用哪一边下载
     */
    public enum Mode {
        /**
         * 走 CDN 直链（失败照旧回退服务端）
         */
        CDN,

        /**
         * 直接走服务端，连 CDN 都不试
         */
        SERVER,

        /**
         * 没测出结果（没有样本、两边都失败等），保持改动前的行为：先 CDN 后服务端
         */
        FALLBACK,
    }

    /**
     * 一次测速要用的样本文件
     */
    public static class Sample {
        /**
         * 清单里的相对路径
         */
        public String path = "";

        /**
         * 文件名，只用于日志
         */
        public String name = "";

        /**
         * 清单里声明的大小，-1 表示未知
         */
        public long size = -1;

        /**
         * 走 CDN 用的 http/https 直链
         */
        public String cdnUrl = "";

        /**
         * 走服务端用的主机端口 + 路径
         */
        public TcpFileClient.Endpoint server;

        /**
         * true = 影子样本（本地已是最新的文件，只用来测速，不参与本次下载）
         */
        public boolean shadow = false;
    }

    /**
     * 选源结果
     */
    public static class Choice {
        /**
         * 本次同步走哪边
         */
        public Mode mode = Mode.FALLBACK;

        /**
         * CDN 测出来的速度（MB/s），0 表示没测出来
         */
        public double cdnSpeed = 0;

        /**
         * 服务端测出来的速度（MB/s），0 表示没测出来
         */
        public double serverSpeed = 0;

        /**
         * 最终一次 CDN 测速用的连接数
         */
        public int cdnThreads = 0;

        /**
         * 是否因为 CDN 太慢而提线程重测过
         */
        public boolean cdnRetested = false;

        /**
         * 结果是否来自缓存（本次没有测速）
         */
        public boolean fromCache = false;

        /**
         * 是否由 source-mode 强制指定（没测速）
         */
        public boolean forced = false;

        /**
         * 样本文件名，只用于日志
         */
        public String sampleName = "";

        /**
         * 一句话说明，例如缓存命中的时间、没测速的原因
         */
        public String note = "";
    }

    /**
     * 测两家速度的结果（null 表示该源没测出来）
     */
    static class ProbeResult {
        TcpFileClient.SpeedSample cdn;
        TcpFileClient.SpeedSample server;
    }

    /**
     * 决定本次同步走 CDN 还是服务端<p>
     * 本方法不会抛异常：任何意外都会退化成 {@link Mode#FALLBACK}（先 CDN 后服务端），绝不影响更新流程
     *
     * @param config       配置
     * @param baseDir      游戏目录（测速缓存放在它下面的 modsync/ 里）
     * @param speedTestDir 测速临时目录（本方法负责在结束时把它整个删掉）
     * @param sample       测速样本，为 null 表示本次找不到合适的大文件
     */
    public static Choice select(AppConfig config, Path baseDir, Path speedTestDir, Sample sample) {
        Choice choice = new Choice();

        if (sample != null) {
            choice.sampleName = sample.name;
        }

        if (config == null) {
            choice.note = "配置缺失";
            return choice;
        }

        // 上一次异常退出（例如进程被杀）可能留下半个测速目录，先清干净，保证不会有临时文件残留
        purgeQuietly(speedTestDir);

        Thread hook = speedTestDir == null ? null : new Thread(() -> purgeQuietly(speedTestDir),
                "mcspeedtest-cleanup");

        if (hook != null) {
            try {
                Runtime.getRuntime().addShutdownHook(hook);
            } catch (IllegalStateException e) {
                hook = null;
            }
        }

        try {
            // ---------- 1. source-mode 强制指定：直接照办，不测速 ----------
            if (config.sourceMode == AppConfig.SourceMode.CDN) {
                choice.mode = Mode.CDN;
                choice.forced = true;
                choice.note = "source-mode 强制 CDN";
                Log.info("[测速] source-mode: cdn（来自 " + config.sourceModeSource + "），强制使用 CDN，跳过测速");
                return choice;
            }

            if (config.sourceMode == AppConfig.SourceMode.SERVER) {
                choice.mode = Mode.SERVER;
                choice.forced = true;
                choice.note = "source-mode 强制服务端";
                Log.info("[测速] source-mode: server（来自 " + config.sourceModeSource + "），强制使用服务端，跳过测速");
                return choice;
            }

            Path cacheFile = baseDir == null ? null : baseDir.resolve(CACHE_DIR).resolve(CACHE_FILE_NAME);
            String signature = signature(config, sample);

            // ---------- 2. 缓存：没过期就直接复用上次的结果 ----------
            Choice cached = readCache(cacheFile, config.sourceCacheSeconds, signature);

            if (cached != null) {
                Log.info("[测速] 命中测速缓存（" + cached.note + "），本次不再测速："
                        + (cached.mode == Mode.CDN ? "选 CDN" : "选服务端"));
                return cached;
            }

            // ---------- 3. 没有可用的大文件样本 ----------
            if (sample == null) {
                choice.mode = Mode.FALLBACK;
                choice.note = "没有可用的大文件样本";
                Log.info("[测速] 本次没有体积不小于 " + trimNumber(config.speedtestMinSizeMb) + " MB 的可用样本，"
                        + "跳过测速，按原有顺序（先 CDN 后服务端）尝试");
                return choice;
            }

            // ---------- 4. 并发测两家 ----------
            int threads = Math.max(1, config.downloadThreads);
            int maxThreads = Math.max(1, config.cdnMaxThreads);
            long durationNanos = Math.max(200, config.speedtestDurationMs) * 1_000_000L;

            Log.info("[测速] 样本 " + sample.name + "（" + BytesUtils.convertBytes(sample.size) + "，"
                    + (sample.shadow ? "本地已有）" : "本次待下载）"));

            if (sample.shadow) {
                Log.info("[测速] 影子样本（本地已有，仅下载临时片段，不影响原文件）");
            }

            Log.info("[测速] CDN 与 服务端 并发测速，各 " + threads + " 条连接，时长 "
                    + config.speedtestDurationMs + "ms，每个来源最多读 "
                    + BytesUtils.convertBytes(SpeedProbe.TOTAL_BYTES));

            long begin = System.currentTimeMillis();
            ProbeResult first = probeBoth(config, speedTestDir, sample, threads, durationNanos);

            double cdnSpeed = speedOf(first.cdn);
            double serverSpeed = speedOf(first.server);

            choice.cdnThreads = threads;

            // ---------- 5. 两边都测不出来：保持原有行为 ----------
            if (cdnSpeed <= 0 && serverSpeed <= 0) {
                choice.mode = Mode.FALLBACK;
                choice.note = "两个来源都没测出速度";
                Log.info("[测速] CDN 与 服务端 都没有测出速度，回退到原有行为（先 CDN 后服务端）");
                return choice;
            }

            double ratio = Math.max(0, config.cdnPreferRatio);
            boolean preferCdn = cdnSpeed > 0 && cdnSpeed >= serverSpeed * ratio;
            boolean shouldRetest = !preferCdn && cdnSpeed > 0 && threads < maxThreads;
            int retryThreads = Math.min(maxThreads, threads * 2);

            Log.info("[测速] " + summary(cdnSpeed, serverSpeed, false) + "  ->  "
                    + (preferCdn ? "选 CDN" + percentOf(cdnSpeed, serverSpeed)
                    : shouldRetest ? "CDN 太慢，提升线程到 " + retryThreads + " 重测"
                    : "选服务端"));

            // ---------- 6. CDN 太慢：提线程重测一次，还是不行才选服务端 ----------
            if (shouldRetest) {
                TcpFileClient.SpeedSample again = probeCdnOnly(config, speedTestDir, sample, retryThreads,
                        durationNanos);

                double retrySpeed = speedOf(again);

                if (retrySpeed > 0) {
                    cdnSpeed = retrySpeed;
                    choice.cdnThreads = retryThreads;
                    choice.cdnRetested = true;
                }

                preferCdn = cdnSpeed > 0 && cdnSpeed >= serverSpeed * ratio;

                Log.info("[测速] " + summary(cdnSpeed, serverSpeed, true) + "  ->  "
                        + (preferCdn ? "选 CDN" + percentOf(cdnSpeed, serverSpeed) : "选服务端"));
            }

            choice.cdnSpeed = cdnSpeed;
            choice.serverSpeed = serverSpeed;
            choice.mode = preferCdn ? Mode.CDN : Mode.SERVER;
            choice.note = "CDN " + format(cdnSpeed) + " MB/s / 服务端 " + format(serverSpeed) + " MB/s";

            Log.info("[测速] 本次同步耗时 " + (System.currentTimeMillis() - begin) + "ms，"
                    + "最终选择：" + (choice.mode == Mode.CDN ? "CDN" : "服务端"));

            // ---------- 7. 写缓存，下次同步直接用 ----------
            writeCache(cacheFile, signature, choice, sample);

            return choice;
        } catch (Exception e) {
            // 选源只是优化，任何意外都不能挡住更新
            Log.warn("[测速] 测速选源失败，回退到原有行为（先 CDN 后服务端）：" + describe(e));

            choice.mode = Mode.FALLBACK;
            choice.note = "测速异常：" + describe(e);

            return choice;
        } finally {
            if (hook != null) {
                try {
                    Runtime.getRuntime().removeShutdownHook(hook);
                } catch (IllegalStateException ignored) {
                    // JVM 正在关闭，钩子已经在跑了，不用管
                }
            }

            purgeQuietly(speedTestDir);
        }
    }

    /**
     * CDN 与服务端并发测速，两边都各自兜住自己的异常（返回 null 表示这个源没测出来）
     */
    static ProbeResult probeBoth(AppConfig config, Path dir, Sample sample, int threads, long durationNanos) {
        ProbeResult result = new ProbeResult();
        ExecutorService pool = Executors.newFixedThreadPool(2, new ProbeThreadFactory());

        try {
            Future<TcpFileClient.SpeedSample> cdn = pool.submit(() -> {
                try {
                    return SpeedProbe.measureCdn(sample.cdnUrl, sample.size, threads, dir, "cdn", durationNanos, config);
                } catch (Exception e) {
                    Log.warn("[测速] CDN 没测出速度：" + describe(e));
                    return null;
                }
            });

            Future<TcpFileClient.SpeedSample> server = pool.submit(() -> {
                try {
                    return SpeedProbe.measureServer(sample.server.hostPort, sample.server.path, sample.size, threads,
                            dir, "server", durationNanos, config);
                } catch (Exception e) {
                    Log.warn("[测速] 服务端没测出速度：" + describe(e));
                    return null;
                }
            });

            long waitMs = Math.max(10_000L, durationNanos / 1_000_000L * 2 + 10_000L);

            result.cdn = await(cdn, waitMs, "CDN");
            result.server = await(server, waitMs, "服务端");
        } finally {
            pool.shutdownNow();
        }

        return result;
    }

    /**
     * CDN 提线程重测（服务端的结果直接复用，不再打扰服务端）
     */
    static TcpFileClient.SpeedSample probeCdnOnly(AppConfig config, Path dir, Sample sample, int threads,
                                                  long durationNanos) {
        try {
            return SpeedProbe.measureCdn(sample.cdnUrl, sample.size, threads, dir, "cdn-retry", durationNanos, config);
        } catch (Exception e) {
            Log.warn("[测速] CDN 重测也没测出速度：" + describe(e));
            return null;
        }
    }

    static TcpFileClient.SpeedSample await(Future<TcpFileClient.SpeedSample> future, long waitMs, String label) {
        try {
            return future.get(waitMs, TimeUnit.MILLISECONDS);
        } catch (Exception e) {
            Log.warn("[测速] " + label + " 测速超时或异常：" + describe(e));
            return null;
        }
    }

    /**
     * 把测速样本换算成 MB/s，没测出来时返回 0
     */
    static double speedOf(TcpFileClient.SpeedSample sample) {
        if (sample == null) {
            return 0;
        }

        double speed = sample.megabytesPerSecond();

        return Double.isFinite(speed) && speed > 0 ? speed : 0;
    }

    /**
     * 拼测速汇总行里的「CDN x MB/s | 服务端 y MB/s」，重测时给 CDN 加个前缀
     */
    static String summary(double cdnSpeed, double serverSpeed, boolean retest) {
        return (retest ? "重测 CDN " : "CDN ") + format(cdnSpeed) + " MB/s  |  服务端 " + format(serverSpeed) + " MB/s";
    }

    /**
     * 选 CDN 时附一句「达服务端百分之多少」，服务端没测出速度时不附
     */
    static String percentOf(double cdnSpeed, double serverSpeed) {
        if (serverSpeed <= 0) {
            return "（服务端未测出速度）";
        }

        return "（达服务端 " + Math.round(cdnSpeed / serverSpeed * 100) + "%）";
    }

    static String format(double speed) {
        return String.format(Locale.ROOT, "%.2f", Math.max(0, speed));
    }

    static String trimNumber(double value) {
        if (value == Math.rint(value)) {
            return String.valueOf((long) value);
        }

        return String.format(Locale.ROOT, "%.1f", value);
    }

    // ------------------------------------------------------------------
    // 测速缓存
    // ------------------------------------------------------------------

    /**
     * 缓存签名：配置里任何一项影响测速结果的改动、或者 CDN 主机/服务端地址变了，都会让旧缓存作废
     */
    static String signature(AppConfig config, Sample sample) {
        return String.join("|",
                config.sourceMode.name(),
                format(config.cdnPreferRatio),
                trimNumber(config.speedtestMinSizeMb),
                String.valueOf(config.downloadThreads),
                String.valueOf(config.cdnMaxThreads),
                String.valueOf(config.speedtestDurationMs),
                String.valueOf(Math.max(0, config.sourceCacheSeconds)),
                sample == null ? "-" : CdnDownloader.hostOf(sample.cdnUrl),
                sample == null || sample.server == null ? "-" : sample.server.hostPort.toString());
    }

    /**
     * 读缓存：签名必须完全一致、且没过 source-cache-seconds，否则当作没有缓存
     */
    static Choice readCache(Path cacheFile, int ttlSeconds, String signature) {
        if (cacheFile == null || ttlSeconds <= 0 || !Files.isRegularFile(cacheFile)) {
            return null;
        }

        try {
            JSONObject root = new JSONObject(new String(Files.readAllBytes(cacheFile), StandardCharsets.UTF_8));

            if (root.optInt("format", 0) != CACHE_FORMAT) {
                return null;
            }

            if (!signature.equals(root.optString("signature", ""))) {
                Log.debug("测速缓存与当前配置不匹配，重新测速");
                return null;
            }

            long timestamp = root.optLong("timestamp", 0L);
            long age = System.currentTimeMillis() - timestamp;

            if (timestamp <= 0 || age > ttlSeconds * 1000L) {
                Log.debug("测速缓存已过期（" + (age / 1000) + " 秒前测的），重新测速");
                return null;
            }

            String mode = root.optString("mode", "");

            Choice choice = new Choice();
            choice.fromCache = true;
            choice.cdnSpeed = root.optDouble("cdn-speed", 0);
            choice.serverSpeed = root.optDouble("server-speed", 0);
            choice.cdnThreads = root.optInt("cdn-threads", 0);
            choice.cdnRetested = root.optBoolean("cdn-retested", false);
            choice.sampleName = root.optString("sample", "");
            choice.note = (age / 1000) + " 秒前测的，样本 " + (choice.sampleName.isEmpty() ? "未知" : choice.sampleName)
                    + (root.optBoolean("sample-shadow", false) ? "（影子）" : "（待下载）")
                    + "，CDN " + format(choice.cdnSpeed) + " MB/s / 服务端 " + format(choice.serverSpeed) + " MB/s";

            if ("cdn".equals(mode)) {
                choice.mode = Mode.CDN;
            } else if ("server".equals(mode)) {
                choice.mode = Mode.SERVER;
            } else {
                return null;
            }

            return choice;
        } catch (Exception e) {
            Log.warn("读取测速缓存失败，将重新测速：" + e.getMessage());
            return null;
        }
    }

    /**
     * 写缓存：只有真正测出结果并做出选择时才写
     */
    static void writeCache(Path cacheFile, String signature, Choice choice, Sample sample) {
        if (cacheFile == null || choice == null || choice.mode == Mode.FALLBACK) {
            return;
        }

        try {
            JSONObject root = new JSONObject();

            root.put("format", CACHE_FORMAT);
            root.put("updated", System.currentTimeMillis());
            root.put("signature", signature);
            root.put("mode", choice.mode == Mode.CDN ? "cdn" : "server");
            root.put("cdn-speed", choice.cdnSpeed);
            root.put("server-speed", choice.serverSpeed);
            root.put("cdn-threads", choice.cdnThreads);
            root.put("cdn-retested", choice.cdnRetested);
            root.put("sample", sample == null ? "" : sample.name);
            root.put("sample-shadow", sample != null && sample.shadow);
            root.put("timestamp", System.currentTimeMillis());

            Path parent = cacheFile.getParent();

            if (parent != null) {
                Files.createDirectories(parent);
            }

            // 先写临时文件再原子替换，避免写到一半断电留下半个 json
            Path tmp = cacheFile.resolveSibling(cacheFile.getFileName() + ".tmp");

            Files.write(tmp, root.toString(2).getBytes(StandardCharsets.UTF_8));
            Files.move(tmp, cacheFile, StandardCopyOption.REPLACE_EXISTING);

            Log.debug("测速选择已写入缓存 " + cacheFile);
        } catch (Exception e) {
            // 缓存只是优化，写失败绝不能影响更新
            Log.warn("写入测速缓存失败（不影响更新）：" + e.getMessage());
        }
    }

    /**
     * 删掉整个测速临时目录（递归），任何失败都只记 debug 日志
     */
    static void purgeQuietly(Path dir) {
        if (dir == null) {
            return;
        }

        try {
            PathUtility.delete(dir);
        } catch (IOException e) {
            Log.debug("清理测速临时目录失败：" + dir + "（" + e.getMessage() + "）");
        }
    }

    /**
     * 取异常的简短描述，用于日志
     */
    static String describe(Throwable e) {
        if (e == null) {
            return "未知原因";
        }

        Throwable cause = e;

        while (cause.getCause() != null && cause.getCause() != cause) {
            cause = cause.getCause();
        }

        String message = cause.getMessage();

        return message == null || message.isEmpty() ? cause.getClass().getSimpleName() : message;
    }

    /**
     * 测速线程工厂，全部起成守护线程
     */
    static class ProbeThreadFactory implements ThreadFactory {
        final AtomicInteger counter = new AtomicInteger();

        @Override
        public Thread newThread(Runnable runnable) {
            Thread thread = new Thread(runnable, "mcspeedtest-probe-" + counter.incrementAndGet());
            thread.setDaemon(true);

            return thread;
        }
    }
}
