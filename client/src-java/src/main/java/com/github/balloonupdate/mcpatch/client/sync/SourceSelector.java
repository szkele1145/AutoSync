package com.github.balloonupdate.mcpatch.client.sync;

import com.github.balloonupdate.mcpatch.client.config.AppConfig;
import com.github.balloonupdate.mcpatch.client.logging.Log;
import com.github.balloonupdate.mcpatch.client.utils.BytesUtils;
import org.json.JSONArray;
import org.json.JSONObject;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * 多更新源并发测速与选优（网络层走 MSFP，不再是 HTTP）<p>
 * 对配置里每一个更新源并发探测两个指标：<p>
 * 1. RTT：用 {@code PING} 请求测一次往返（含 TCP 建连时间）<p>
 * 2. 吞吐：先用 {@code SIZE} 问出测速文件大小，再下载 speedtest.bin 算实际 MB/s<p>
 * 若测速文件不存在（ERR not found）或下载失败，该源降级为只测 RTT，不影响它参与选优<p>
 * 综合评分公式（权重可配置，默认偏向吞吐，因为玩家痛点是下载慢）：<pre>
 *     score = speed_mbps * source-weight-speed + (1000.0 / max(rtt_ms, 1)) * source-weight-latency
 * </pre>
 * 测速结果会缓存到 baseDir/modsync/source-stats.json，TTL 由 source-cache-seconds 控制<p>
 * 单个源的探测超时由 source-probe-timeout 控制，且所有源是并发探测的，任何一个源超时都不会拖累其它源
 */
public class SourceSelector {
    /**
     * 清单文件名（只用于日志描述，MSFP 下 RTT 改用 PING 探测）
     */
    public static final String MANIFEST_NAME = "manifest.json";

    /**
     * 测速缓存文件的格式版本
     */
    static final int CACHE_FORMAT = 1;

    /**
     * 单次测速最多读取的字节数，防止更新源上放了一个超大文件把玩家的流量吃光
     */
    static final long MAX_SPEEDTEST_BYTES = 4L * 1024 * 1024;

    /**
     * 单个更新源的探测结果
     */
    public static class SourceStat implements Comparable<SourceStat> {
        /**
         * 更新源地址（已去掉结尾的斜杠）
         */
        public String url = "";

        /**
         * 该源在配置里的原始顺序，用于同分时保持稳定排序
         */
        public int index = 0;

        /**
         * 往返延迟，单位毫秒，-1 表示探测失败
         */
        public long rttMs = -1;

        /**
         * 吞吐速度，单位 MB/s，speedTested 为 false 时无意义
         */
        public double speedMbps = 0;

        /**
         * 是否成功测到了吞吐（测速文件不存在时为 false）
         */
        public boolean speedTested = false;

        /**
         * 该源是否可以用来更新（PING 能通）
         */
        public boolean available = false;

        /**
         * 综合得分，越大越优先
         */
        public double score = 0;

        /**
         * 探测失败的原因，成功时为 null
         */
        public String error = null;

        /**
         * 本次测速的时间戳（命中缓存时保留上次测速的时间，保证 TTL 从测速那一刻算起）
         */
        public long timestamp = 0;

        /**
         * 是否来自缓存（本次没有重新测速）
         */
        public boolean fromCache = false;

        @Override
        public int compareTo(SourceStat other) {
            // 可用的永远排在不可用的前面
            if (available != other.available) {
                return available ? -1 : 1;
            }

            int byScore = Double.compare(other.score, score);

            if (byScore != 0) {
                return byScore;
            }

            long a = rttMs < 0 ? Long.MAX_VALUE : rttMs;
            long b = other.rttMs < 0 ? Long.MAX_VALUE : other.rttMs;

            if (a != b) {
                return Long.compare(a, b);
            }

            return Integer.compare(index, other.index);
        }

        /**
         * 拼一行人能看的测速描述
         */
        public String describe() {
            StringBuilder sb = new StringBuilder(url);

            sb.append("  rtt=").append(rttMs < 0 ? "超时" : rttMs + "ms");
            sb.append("  速度=").append(speedTested ? String.format("%.2f MB/s", speedMbps) : "未测(仅RTT)");
            sb.append("  得分=").append(String.format("%.3f", score));

            if (fromCache) {
                sb.append("（缓存）");
            }

            if (!available) {
                sb.append("  不可用");
            }

            if (error != null && !error.isEmpty()) {
                sb.append("  原因=").append(error);
            }

            return sb.toString();
        }
    }

    /**
     * 选源结果
     */
    public static class Result {
        /**
         * 所有源的测速结果，已按优先级排序（可用的、得分高的在前）
         */
        public final List<SourceStat> stats = new ArrayList<>();

        /**
         * 按优先级排好的更新源地址列表，SyncEngine 按这个顺序尝试
         */
        public final List<String> orderedUrls = new ArrayList<>();

        /**
         * 最优源，没有可用源时为 null
         */
        public SourceStat best = null;

        /**
         * 是否所有结果都来自缓存
         */
        public boolean fromCache = false;

        /**
         * 是否使用了 manual-source（手动锁定更新源）
         */
        public boolean manual = false;

        /**
         * 是否关闭了 auto-select-source，此时 orderedUrls 就是配置里的原始顺序
         */
        public boolean disabled = false;
    }

    /**
     * 探测并选择最优更新源<p>
     * 任何异常都不会抛出，最差情况下会退回配置里的原始顺序，保证更新流程能继续走
     *
     * @param baseUrls  配置里的更新源地址列表（msfp://host:port 或裸 host:port）
     * @param config    配置对象
     * @param cacheFile 测速缓存文件（baseDir/modsync/source-stats.json）
     */
    public static Result select(List<String> baseUrls, AppConfig config, Path cacheFile) {
        Result result = new Result();

        List<String> bases = normalizeAll(baseUrls);

        if (bases.isEmpty()) {
            Log.warn("配置里没有任何有效的更新源地址");
            return result;
        }

        // ---------- 1. manual-source：手动锁定，跳过测速，只用这一个源 ----------
        String manual = config.manualSource == null ? "" : normalize(config.manualSource);

        if (!manual.isEmpty()) {
            result.manual = true;
            result.orderedUrls.add(manual);

            Log.info("已启用 manual-source，锁定更新源：" + manual + "（跳过自动测速，不尝试其它源）");

            SourceStat stat = new SourceStat();
            stat.url = manual;
            stat.available = true;
            stat.index = 0;
            result.stats.add(stat);
            result.best = stat;

            return result;
        }

        // ---------- 2. auto-select-source 关闭：退回原来的顺序尝试行为 ----------
        if (!config.autoSelectSource) {
            result.disabled = true;
            result.orderedUrls.addAll(bases);

            Log.info("auto-select-source 已关闭，不进行测速，按配置顺序依次尝试更新源");
            return result;
        }

        // ---------- 3. 并发测速 ----------
        long probeTimeout = Math.max(1000, config.sourceProbeTimeout);
        long ttlMs = Math.max(0, config.sourceCacheSeconds) * 1000L;

        Map<String, SourceStat> cached = readCache(cacheFile, bases, ttlMs);
        List<String> todo = new ArrayList<>();
        int cacheHits = 0;

        for (String base : bases) {
            SourceStat hit = cached.get(base);

            if (hit != null) {
                result.stats.add(hit);
                cacheHits += 1;
                Log.info("测速缓存命中（本次不再测速）：" + hit.describe());
            } else {
                todo.add(base);
            }
        }

        if (todo.isEmpty()) {
            result.fromCache = cacheHits > 0;
            Log.info("全部 " + cacheHits + " 个更新源都命中测速缓存（TTL " + config.sourceCacheSeconds + " 秒），跳过测速");
        } else {
            Log.info("开始并发测速，" + todo.size() + " 个更新源待测（单源超时 " + probeTimeout + "ms）："
                    + String.join("、", todo));

            long begin = System.currentTimeMillis();

            for (SourceStat stat : probeAll(todo, config, probeTimeout)) {
                result.stats.add(stat);
                Log.info("测速结果：" + stat.describe());
            }

            Log.info("并发测速完成，耗时 " + (System.currentTimeMillis() - begin) + "ms");
        }

        // ---------- 4. 算分排序 ----------
        for (SourceStat stat : result.stats) {
            stat.index = bases.indexOf(stat.url);
            stat.score = computeScore(stat, config);
        }

        result.stats.sort(null);

        for (SourceStat stat : result.stats) {
            result.orderedUrls.add(stat.url);
        }

        // 兜底：万一 stats 里少了谁（理论上不会），剩下的源按原顺序补到末尾
        for (String base : bases) {
            if (!result.orderedUrls.contains(base)) {
                result.orderedUrls.add(base);
            }
        }

        for (SourceStat stat : result.stats) {
            if (stat.available) {
                result.best = stat;
                break;
            }
        }

        // ---------- 5. 写缓存（只写这次真正测过的源，缓存命中的条目原样留着） ----------
        if (!todo.isEmpty()) {
            writeCache(cacheFile, result.stats);
        }

        if (result.best == null) {
            Log.warn("所有更新源都测速失败，将按配置顺序依次尝试");
        } else {
            Log.info("已选中更新源：" + result.best.url
                    + "（rtt=" + result.best.rttMs + "ms，"
                    + (result.best.speedTested ? String.format("%.2f MB/s", result.best.speedMbps) : "未测吞吐")
                    + "，得分 " + String.format("%.3f", result.best.score) + "）");
        }

        return result;
    }

    /**
     * 综合评分，越大越优先<pre>
     * score = speed_mbps * wSpeed + (1000.0 / max(rtt_ms,1)) * wLatency
     * </pre>
     * 没有测到吞吐的源（测速文件不存在）只保留延迟项，等价于「降级为只测 RTT」
     */
    public static double computeScore(SourceStat stat, AppConfig config) {
        if (!stat.available) {
            return 0;
        }

        double speedTerm = stat.speedTested ? stat.speedMbps * config.sourceWeightSpeed : 0;
        double latencyTerm = (1000.0 / Math.max(stat.rttMs, 1L)) * config.sourceWeightLatency;

        return speedTerm + latencyTerm;
    }

    /**
     * 把某个源从测速缓存里踢掉，让下次启动重新测速<p>
     * 用于「缓存里排第一的源这次却拉不到清单」的场景，避免它一直霸占首位
     */
    public static void invalidate(Path cacheFile, String url) {
        if (cacheFile == null || url == null) {
            return;
        }

        String target = normalize(url);

        try {
            if (!Files.isRegularFile(cacheFile)) {
                return;
            }

            JSONObject root = new JSONObject(new String(Files.readAllBytes(cacheFile), java.nio.charset.StandardCharsets.UTF_8));
            JSONArray entries = root.optJSONArray("entries");

            if (entries == null) {
                return;
            }

            JSONArray kept = new JSONArray();
            boolean removed = false;

            for (int i = 0; i < entries.length(); i++) {
                JSONObject o = entries.optJSONObject(i);

                if (o == null) {
                    continue;
                }

                if (normalize(o.optString("url", "")).equals(target)) {
                    removed = true;
                    continue;
                }

                kept.put(o);
            }

            if (!removed) {
                return;
            }

            root.put("entries", kept);
            root.put("updated", System.currentTimeMillis());

            Path tmp = cacheFile.resolveSibling(cacheFile.getFileName() + ".tmp");
            Files.write(tmp, root.toString(2).getBytes(java.nio.charset.StandardCharsets.UTF_8));
            Files.move(tmp, cacheFile, StandardCopyOption.REPLACE_EXISTING);

            Log.debug("已把失效的更新源从测速缓存中移除，下次启动会重新测速：" + target);
        } catch (Exception e) {
            Log.debug("清理测速缓存失败（不影响更新）：" + e.getMessage());
        }
    }

    /**
     * 并发探测所有源，任何一个源超时都不会拖累其它源<p>
     * 线程池大小跟待测源数量一致（上限 16），保证多源时是真的并发而不是排队
     */
    static List<SourceStat> probeAll(List<String> todo, AppConfig config, long probeTimeout) {
        List<SourceStat> stats = new ArrayList<>();

        ExecutorService pool = Executors.newFixedThreadPool(Math.min(todo.size(), 16), new ProbeThreadFactory());
        Map<String, CompletableFuture<SourceStat>> futures = new LinkedHashMap<>();

        try {
            for (String base : todo) {
                futures.put(base, CompletableFuture.supplyAsync(() -> probe(base, config, probeTimeout), pool));
            }

            // 单个源最多花 probeTimeout 测 RTT，再花 probeTimeout 测吞吐，这里给一倍余量
            long waitMs = probeTimeout * 2L + 2000L;

            for (Map.Entry<String, CompletableFuture<SourceStat>> entry : futures.entrySet()) {
                SourceStat stat;

                try {
                    stat = entry.getValue().get(waitMs, TimeUnit.MILLISECONDS);
                } catch (Exception e) {
                    stat = new SourceStat();
                    stat.url = entry.getKey();
                    stat.available = false;
                    stat.error = e instanceof java.util.concurrent.TimeoutException
                            ? "测速总耗时超过 " + waitMs + "ms"
                            : "测速异常：" + shortError(e);

                    Log.warn("更新源测速失败：" + entry.getKey() + "（" + stat.error + "）");
                }

                stat.timestamp = System.currentTimeMillis();
                stats.add(stat);
            }
        } finally {
            pool.shutdownNow();
        }

        return stats;
    }

    /**
     * 探测单个更新源：先用 PING 测 RTT（含建连），再用测速文件测吞吐<p>
     * 测速文件缺失或下载失败时不算失败，只是降级为「只测 RTT」，并打一条 debug 日志
     */
    static SourceStat probe(String base, AppConfig config, long probeTimeout) {
        SourceStat stat = new SourceStat();
        stat.url = base;

        TcpFileClient.HostPort hostPort;

        try {
            hostPort = TcpFileClient.parse(base);
        } catch (Exception e) {
            stat.rttMs = -1;
            stat.available = false;
            stat.error = "地址不合法：" + shortError(e);
            Log.warn("更新源地址不可用：" + base + "（" + stat.error + "）");
            return stat;
        }

        // ---------- 1. RTT：PING 一次，含 TCP 建连 ----------
        try {
            stat.rttMs = TcpFileClient.ping(hostPort, config);
            stat.available = true;
        } catch (Exception e) {
            stat.rttMs = -1;
            stat.available = false;
            stat.error = shortError(e);
            Log.warn("更新源不可用：" + base + "（" + stat.error + "）");
            return stat;
        }

        Log.debug("更新源 " + base + " PING 成功，rtt=" + stat.rttMs + "ms");

        // ---------- 2. 吞吐：SIZE 拿大小，再实际下一段测速度 ----------
        String speedtestFile = config.sourceSpeedtestFile == null || config.sourceSpeedtestFile.trim().isEmpty()
                ? "speedtest.bin"
                : config.sourceSpeedtestFile.trim();

        long knownSize;

        try {
            knownSize = TcpFileClient.size(hostPort, speedtestFile, config);
        } catch (Exception e) {
            // 服务端明确回了 ERR not found 之类，或者这条连接出了问题，都只降级不失败
            Log.debug("更新源 " + base + " 上没有可用的测速文件 " + speedtestFile + "（" + shortError(e)
                    + "），该源降级为只测 RTT");
            return stat;
        }

        if (knownSize <= 0) {
            Log.debug("更新源 " + base + " 的测速文件 " + speedtestFile + " 大小为 " + knownSize
                    + "，该源降级为只测 RTT");
            return stat;
        }

        try {
            measureSpeed(stat, hostPort, speedtestFile, knownSize, probeTimeout);
        } catch (Exception e) {
            Log.debug("更新源 " + base + " 下载测速文件失败（" + shortError(e) + "），该源降级为只测 RTT");
        }

        return stat;
    }

    /**
     * 用一条 MSFP 连接实际拉一段测速数据，算出吞吐<p>
     * 速度只统计「第一个数据块到达 → 读完」这段时间，建连和首字节等待已经体现在 RTT 里了，避免重复计算
     */
    static void measureSpeed(SourceStat stat, TcpFileClient.HostPort hostPort, String speedtestFile,
                             long knownSize, long probeTimeout) throws IOException {
        long limit = Math.min(knownSize, MAX_SPEEDTEST_BYTES);

        TcpFileClient.SpeedSample sample = TcpFileClient.speedtest(hostPort, speedtestFile,
                limit, probeTimeout * 1_000_000L, newTimeoutConfig(probeTimeout));

        if (sample == null) {
            Log.debug("更新源 " + stat.url + " 没有取到测速数据，该源降级为只测 RTT");
            return;
        }

        stat.speedMbps = sample.megabytesPerSecond();
        stat.speedTested = true;

        Log.debug("更新源 " + stat.url + " 测速文件读了 " + BytesUtils.convertBytes(sample.bytes)
                + "（服务端报告 " + BytesUtils.convertBytes(knownSize) + "），传输耗时 "
                + String.format("%.0f", sample.transferNanos / 1_000_000.0) + "ms，速度 "
                + String.format("%.2f MB/s", stat.speedMbps));
    }

    /**
     * 造一个只改了读超时的配置副本给测速连接用，这样测速不会因为 tcp-timeout 设得很大而卡很久
     */
    static AppConfig newTimeoutConfig(long probeTimeout) {
        AppConfig cfg = new AppConfig(new HashMap<>());
        cfg.tcpTimeout = (int) Math.max(1000, probeTimeout);
        return cfg;
    }

    /**
     * 读取测速缓存，只有没过期、且还在当前 urls 列表里的条目才会被复用
     */
    static Map<String, SourceStat> readCache(Path cacheFile, List<String> bases, long ttlMs) {
        Map<String, SourceStat> result = new HashMap<>();

        if (cacheFile == null || !Files.isRegularFile(cacheFile)) {
            return result;
        }

        try {
            JSONObject root = new JSONObject(new String(Files.readAllBytes(cacheFile), java.nio.charset.StandardCharsets.UTF_8));
            JSONArray entries = root.optJSONArray("entries");

            if (entries == null) {
                return result;
            }

            long now = System.currentTimeMillis();

            for (int i = 0; i < entries.length(); i++) {
                JSONObject o = entries.optJSONObject(i);

                if (o == null) {
                    continue;
                }

                String url = normalize(o.optString("url", ""));

                if (url.isEmpty() || !bases.contains(url) || result.containsKey(url)) {
                    continue;
                }

                long timestamp = o.optLong("timestamp", 0L);
                long age = now - timestamp;

                if (ttlMs <= 0 || timestamp <= 0 || age > ttlMs) {
                    Log.debug("测速缓存已过期：" + url + "（" + (age / 1000) + " 秒前测的）");
                    continue;
                }

                SourceStat stat = new SourceStat();
                stat.url = url;
                stat.rttMs = o.optLong("rtt", -1L);
                stat.speedMbps = o.optDouble("speed", 0);
                stat.speedTested = o.optBoolean("speed-tested", false);
                stat.available = true;
                stat.timestamp = timestamp;
                stat.fromCache = true;

                result.put(url, stat);
            }
        } catch (Exception e) {
            Log.warn("读取测速缓存失败，将重新测速：" + e.getMessage());
            result.clear();
        }

        return result;
    }

    /**
     * 写测速缓存，只缓存可用的源（不可用的源下次启动重新测，避免临时抽风被缓存一小时）
     */
    static void writeCache(Path cacheFile, List<SourceStat> stats) {
        if (cacheFile == null) {
            return;
        }

        try {
            JSONArray entries = new JSONArray();

            for (SourceStat stat : stats) {
                if (!stat.available) {
                    continue;
                }

                JSONObject o = new JSONObject();
                o.put("url", stat.url);
                o.put("rtt", stat.rttMs);
                o.put("speed", stat.speedMbps);
                o.put("speed-tested", stat.speedTested);
                o.put("score", stat.score);
                o.put("timestamp", stat.timestamp > 0 ? stat.timestamp : System.currentTimeMillis());
                o.put("from-cache", stat.fromCache);
                entries.put(o);
            }

            JSONObject root = new JSONObject();
            root.put("format", CACHE_FORMAT);
            root.put("updated", System.currentTimeMillis());
            root.put("entries", entries);

            Path parent = cacheFile.getParent();

            if (parent != null) {
                Files.createDirectories(parent);
            }

            // 先写临时文件再原子替换，避免写到一半断电留下半个 json
            Path tmp = cacheFile.resolveSibling(cacheFile.getFileName() + ".tmp");
            Files.write(tmp, root.toString(2).getBytes(java.nio.charset.StandardCharsets.UTF_8));
            Files.move(tmp, cacheFile, StandardCopyOption.REPLACE_EXISTING);

            Log.debug("测速缓存已写入 " + cacheFile);
        } catch (Exception e) {
            // 缓存只是优化，写失败绝不能影响更新
            Log.warn("写入测速缓存失败（不影响更新）：" + e.getMessage());
        }
    }

    /**
     * 把地址列表规范化：去空格、去掉结尾的斜杠、去重且保持原顺序
     */
    static List<String> normalizeAll(List<String> urls) {
        LinkedHashSet<String> set = new LinkedHashSet<>();

        if (urls != null) {
            for (String url : urls) {
                if (url == null) {
                    continue;
                }

                String value = normalize(url);

                if (!value.isEmpty()) {
                    set.add(value);
                }
            }
        }

        return new ArrayList<>(set);
    }

    /**
     * 去空格并去掉结尾的斜杠，方便缓存里的地址和配置里的地址对得上
     */
    static String normalize(String url) {
        if (url == null) {
            return "";
        }

        String value = url.trim();

        while (value.endsWith("/") && value.length() > 1) {
            value = value.substring(0, value.length() - 1);
        }

        return value;
    }

    /**
     * 把异常压成一行短文本，方便打日志
     */
    static String shortError(Throwable e) {
        if (e == null) {
            return "";
        }

        Throwable cause = e;

        while (cause.getCause() != null && cause.getCause() != cause) {
            cause = cause.getCause();
        }

        String message = cause.getMessage();
        String name = cause.getClass().getSimpleName();

        return message == null || message.isEmpty() ? name : name + ": " + message;
    }

    /**
     * 探测线程工厂，全部起成守护线程，避免拖住 JVM 退出
     */
    static class ProbeThreadFactory implements ThreadFactory {
        final AtomicInteger counter = new AtomicInteger();

        @Override
        public Thread newThread(Runnable runnable) {
            Thread thread = new Thread(runnable, "mcpatch-probe-" + counter.incrementAndGet());
            thread.setDaemon(true);
            return thread;
        }
    }
}
