package com.github.balloonupdate.mcpatch.client;

import com.github.balloonupdate.mcpatch.client.config.AppConfig;
import com.github.balloonupdate.mcpatch.client.exceptions.McpatchBusinessException;
import com.github.balloonupdate.mcpatch.client.logging.Log;
import com.github.balloonupdate.mcpatch.client.sync.CdnDownloader;
import com.github.balloonupdate.mcpatch.client.sync.HashUtil;
import com.github.balloonupdate.mcpatch.client.sync.Manifest;
import com.github.balloonupdate.mcpatch.client.sync.ManifestFile;
import com.github.balloonupdate.mcpatch.client.sync.ModIdReader;
import com.github.balloonupdate.mcpatch.client.sync.SourceModeSelector;
import com.github.balloonupdate.mcpatch.client.sync.SourceSelector;
import com.github.balloonupdate.mcpatch.client.sync.TcpFileClient;
import com.github.balloonupdate.mcpatch.client.sync.TcpFileClient.HostPort;
import com.github.balloonupdate.mcpatch.client.ui.McPatchWindow;
import com.github.balloonupdate.mcpatch.client.utils.BytesUtils;
import com.github.balloonupdate.mcpatch.client.utils.Env;
import com.github.balloonupdate.mcpatch.client.utils.PathUtility;
import com.github.balloonupdate.mcpatch.client.utils.SpeedStat;

import javax.swing.*;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Date;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.regex.Pattern;

/**
 * 同步主逻辑：清单驱动 + 多来源下载 + SHA-256 校验
 */
public class SyncEngine {
    /**
     * 清单文件名，相对于每个更新源地址
     */
    public static final String MANIFEST_NAME = "manifest.json";

    /**
     * 临时目录名，位于游戏目录下
     */
    public static final String TEMP_DIR = ".modsync-temp";

    /**
     * 镜像模式下清理清单外文件时的备份目录名，位于游戏目录下<p>
     * 目录内保留被清理文件相对于游戏目录的路径结构，例如 mods/a.jar -&gt; .modsync-removed/mods/a.jar
     */
    public static final String MIRROR_BACKUP_DIR = ".modsync-removed";

    /**
     * cdn-exclude 通配符的编译缓存（key 就是配置里写的原始通配串）
     */
    static final Map<String, Pattern> GLOB_CACHE = new ConcurrentHashMap<>();

    public McPatchWindow window;
    public AppConfig config;
    public Path baseDir;
    public Path progDir;
    public Path logFilePath;
    public boolean graphicsMode;
    public Main.StartMethod startMethod;

    /**
     * 本次镜像扫描是否因为「清单里没有 mods/ 条目」而触发了安全兜底（跳过清理）<p>
     * 只用来决定收尾日志的措辞：兜底跳过时绝不能输出「已与服务端清单一致」，那会误导用户
     */
    boolean mirrorScanSkipped = false;

    /**
     * 本次同步的 CDN / 服务端测速选源结果，null 表示没测（按原有行为：先 CDN 后服务端）<p>
     * 每次同步只测一次，由 {@link #sync()} 在下载开始前填好，所有文件的下载共用这一个结果
     */
    public SourceModeSelector.Choice speedChoice = null;

    public boolean run() throws McpatchBusinessException {
        if (window != null && !config.silentMode) {
            window.show();
        }

        try {
            return sync();
        } catch (McpatchBusinessException e) {
            throw e;
        } catch (Exception e) {
            throw new McpatchBusinessException(e);
        }
    }

    boolean sync() throws Exception {
        Path versionFile = progDir.resolve(config.versionFilePath);
        String localVersion = (!config.testMode && Files.exists(versionFile))
                ? new String(Files.readAllBytes(versionFile), StandardCharsets.UTF_8).trim()
                : "";

        Log.info("正在检查更新");

        if (window != null) {
            window.setLabelText("正在获取清单");
        }

        // ---------- 1. 挑选更新源 ----------
        // 先并发测速选出最优源，再按排名依次尝试拉清单：manual-source 只用指定的那一个，
        // auto-select-source 关闭时退回「按配置顺序尝试」的老行为
        Path sourceCacheFile = baseDir.resolve("modsync").resolve("source-stats.json");

        Log.openIndent("更新源选择");
        SourceSelector.Result selection;

        try {
            selection = SourceSelector.select(config.urls, config, sourceCacheFile);
        } catch (Exception e) {
            // 选源只是优化，任何意外都不能挡住更新
            Log.warn("更新源测速选优失败，将按配置顺序依次尝试：" + e);
            selection = new SourceSelector.Result();
        }

        if (selection.orderedUrls.isEmpty()) {
            selection.orderedUrls.addAll(config.urls);
        }

        Log.info("更新源尝试顺序：" + String.join(" -> ", selection.orderedUrls));
        Log.closeIndent();

        // ---------- 2. 拉取清单 ----------
        // 更新源地址是 MSFP 地址（msfp://host:port 或裸 host:port），走自定义 TCP 协议而不是 HTTP
        String manifestText = null;
        HostPort manifestBase = null;
        String manifestSourceUrl = null;
        Exception lastError = null;

        // 按测速排名依次尝试，第一个能拿到清单的源就是本次的清单源。
        // 注意这里是「依次尝试」而不是「循环重试」：每个源只试一次，全都失败就报错退出
        for (String base : selection.orderedUrls) {
            try {
                Log.debug("尝试更新源: " + base);

                HostPort hostPort = TcpFileClient.parse(base);

                manifestText = TcpFileClient.fetchText(hostPort, MANIFEST_NAME, config);
                manifestBase = hostPort;
                manifestSourceUrl = base;
                break;
            } catch (Exception e) {
                lastError = e;
                Log.warn("更新源不可用: " + base + " (" + e.getMessage() + ")");

                // 测速排名里第一的源这次却拉不到清单，说明缓存的测速结果已经不准了，把它踢掉，下次启动重新测速
                if (selection.best != null && selection.best.url.equals(base)) {
                    SourceSelector.invalidate(sourceCacheFile, base);
                }
            }
        }

        if (manifestText == null || manifestBase == null) {
            throw new McpatchBusinessException("无法从任何更新源获取清单文件 " + MANIFEST_NAME, lastError);
        }

        // 后面的下载任务跑在别的线程里，捕获的变量必须是 final，这里定死清单所在源
        final HostPort selectedBase = manifestBase;

        // 文件下载按「刚拉清单成功的源优先，其余按测速排名兜底」的顺序尝试
        final List<String> downloadBases = rankWithFirst(manifestSourceUrl, selection.orderedUrls, config.urls);

        Manifest manifest = Manifest.parse(manifestText);

        Log.info("清单版本: " + manifest.version + "，收录 " + manifest.files.size() + " 个文件");

        // ---------- 2. 计算差异 ----------
        List<ManifestFile> downloads = new ArrayList<>();
        List<Path> deletes = new ArrayList<>();

        long scanned = 0;

        for (ManifestFile f : manifest.files) {
            Path local = baseDir.resolve(f.path).normalize();

            if (!config.testMode && Files.isRegularFile(local)) {
                try {
                    if (f.size < 0 || Files.size(local) == f.size) {
                        if (window != null) {
                            window.setLabelSecondaryText(PathUtility.getFilename(f.path));
                        }

                        scanned += 1;

                        if (HashUtil.sha256(local).equalsIgnoreCase(f.sha256)) {
                            continue;
                        }
                    }
                } catch (IOException ignored) {
                    // 读取失败就当作需要重新下载
                }
            }

            downloads.add(f);
        }

        for (String d : manifest.deletes) {
            Path p = baseDir.resolve(d).normalize();
            if (Files.exists(p)) {
                deletes.add(p);
            }
        }

        if (window != null) {
            window.setLabelSecondaryText("");
        }

        Log.debug("本地已校验 " + scanned + " 个文件，需要下载 " + downloads.size() + " 个，删除 " + deletes.size() + " 个");

        // ---------- 2.5 镜像模式：规划 mods 目录的清理 ----------
        // 这里只规划不执行：真正的清理必须等下载应用完成之后再做，
        // 否则会先把马上要下回来的文件删掉，然后再下载一遍
        List<Path> mirrorRemovals = planMirrorRemovals(manifest);

        boolean hasUpdate = !downloads.isEmpty() || !deletes.isEmpty() || !mirrorRemovals.isEmpty();

        if (!hasUpdate) {
            if (config.mirrorMode) {
                if (mirrorScanSkipped) {
                    Log.info("镜像模式：本次因清单里没有 mods/ 条目而跳过了清理，mods 目录保持原样（详见上面的警告）");
                } else {
                    Log.info("镜像模式：mods 目录已与服务端清单一致，没有需要清理的文件");
                }
            }

            Log.info("暂时没有更新");

            if (window != null) {
                window.setLabelText("暂时没有更新");

                if (config.showNoUpdateMessage) {
                    JOptionPane.showMessageDialog(null,
                            "暂时没有更新，当前版本：" + (manifest.version.isEmpty() ? localVersion : manifest.version),
                            config.windowTitle, JOptionPane.INFORMATION_MESSAGE);
                }
            }

            writeVersion(versionFile, manifest.version);

            // 即使本次没有任何文件更新，也要检查一次玩家自加的模组有没有和服务器模组撞 modId
            detectModConflicts(manifest);

            return false;
        }

        if (window != null && config.silentMode) {
            window.show();
        }

        // ---------- 3. 安全检查：绝不更新自己和日志文件 ----------
        Path currentJar = Env.getJarPath();

        if (currentJar != null) {
            Path jar = currentJar.normalize();
            downloads.removeIf(f -> baseDir.resolve(f.path).normalize().equals(jar));
            deletes.removeIf(p -> p.equals(jar));
        }

        if (logFilePath != null) {
            Path log = logFilePath.normalize();
            downloads.removeIf(f -> baseDir.resolve(f.path).normalize().equals(log));
            deletes.removeIf(p -> p.equals(log));
        }

        // ---------- 3.5 CDN / 服务端 自动测速选源 ----------
        // 每次同步只测一次：优先从待下载列表里挑最大的文件当样本，一个够大的都没有就用本地已有的大文件
        // 做影子样本（只下载临时片段，绝不碰用户的真实文件），CDN 与服务端并发测速后决定这次走哪边；
        // 结果按 source-cache-seconds 缓存，没过期时下次同步直接用，不再测速
        try {
            SourceModeSelector.Sample sample = pickSpeedtestSample(manifest, downloads, selectedBase, downloadBases);

            speedChoice = SourceModeSelector.select(config, baseDir,
                    baseDir.resolve(TEMP_DIR).resolve("speedtest"), sample);
        } catch (Exception e) {
            // 选源只是优化，任何意外都不能挡住更新
            Log.warn("测速选源失败，按原有顺序（先 CDN 后服务端）尝试：" + e);
            speedChoice = null;
        }

        // ---------- 4. 下载（文件级并发，单个文件的失败不会打断其它文件） ----------
        final Path tempDir = baseDir.resolve(TEMP_DIR);

        long totalBytesAcc = 0;
        for (ManifestFile f : downloads) {
            totalBytesAcc += Math.max(0, f.size);
        }
        final long totalBytes = totalBytesAcc;

        // 并发度：concurrent-files 上限受待下载文件数约束，config 里已经把它夹在 1~16 之间
        final int concurrency = Math.max(1, Math.min(config.concurrentFiles, downloads.size()));

        Log.info("需要下载 " + downloads.size() + " 个文件，共 " + BytesUtils.convertBytes(totalBytes)
                + "，文件级并发度 " + concurrency + "（单个大文件再按 download-threads="
                + Math.max(1, config.downloadThreads) + " 分段"
                + (cdnDownloadThreads() != Math.max(1, config.downloadThreads)
                ? "，本次测速把 CDN 分段提到 " + cdnDownloadThreads() : "")
                + "，最大并发连接数约 "
                + (concurrency * Math.max(1, config.downloadThreads)) + "，小文件只占 1 条连接）");

        if (window != null) {
            window.setLabelText("正在下载更新文件");
            window.setProgressBarValue(0);
        }

        final AtomicLong done = new AtomicLong();
        final SpeedStat speed = new SpeedStat(1500);
        final AtomicLong uiTimer = new AtomicLong(System.currentTimeMillis() - 600);

        // 进度统计相关的对象（AtomicLong、SpeedStat、UI）都不是为并发设计的，
        // 所有下载线程都必须在这个锁里更新它们，不然 SpeedStat 的内部队列会被并发写坏
        final Object progressLock = new Object();

        // 每个文件下载失败时最多换几个来源重试
        final List<String> baseCandidates = downloadBases;

        // 失败记录（明细由 downloadOne 直接写进来，因为 Future 的返回值在这里已经被日志吞掉了）
        final List<String> failures = java.util.Collections.synchronizedList(new ArrayList<>());

        ExecutorService filePool = Executors.newFixedThreadPool(concurrency, new FileDownloadThreadFactory());
        List<Future<?>> futures = new ArrayList<>();

        try {
            for (ManifestFile f : downloads) {
                // lambda 里用到的局部变量必须是 final / 事实上的 final，这里复制一份
                final ManifestFile target = f;

                futures.add(filePool.submit(() -> downloadOne(target, tempDir, selectedBase, baseCandidates,
                        config.urls, done, totalBytes, speed, uiTimer, progressLock, failures)));
            }

            // 各个文件的失败互不影响：这里先等全部文件跑完，再统一看有没有失败
            for (Future<?> future : futures) {
                try {
                    future.get();
                } catch (java.util.concurrent.ExecutionException e) {
                    Log.debug("文件下载任务异常结束：" + e.getCause());
                }
            }
        } finally {
            filePool.shutdown();
        }

        if (!failures.isEmpty()) {
            Log.error("有 " + failures.size() + " 个文件下载失败，本次更新中止（不会应用任何文件）：");

            for (String failure : failures) {
                Log.error("  " + failure);
            }

            // 整体失败时清理掉临时目录，避免残留一堆半个文件把磁盘占满
            PathUtility.delete(tempDir);

            throw new McpatchBusinessException("有 " + failures.size() + " 个文件下载失败：" + failures.get(0), lastError);
        }

        if (window != null) {
            window.setLabelSecondaryText("");
            window.setProgressBarValue(1000);
        }

        // ---------- 5. 应用 ----------
        if (window != null) {
            window.setLabelText("正在应用更新，请不要关闭程序");
        }

        for (Path p : deletes) {
            Log.debug("删除旧文件 " + p);
            PathUtility.delete(p);
        }

        for (ManifestFile f : downloads) {
            Path from = tempDir.resolve(f.path + ".temp");
            Path to = baseDir.resolve(f.path);

            if (!Files.exists(from)) {
                throw new McpatchBusinessException("临时文件丢失：" + from);
            }

            Files.createDirectories(to.getParent());

            if (Files.exists(to)) {
                PathUtility.delete(to);
            }

            Files.move(from, to, StandardCopyOption.REPLACE_EXISTING);
        }

        PathUtility.delete(tempDir);

        // ---------- 5.5 镜像模式清理 ----------
        // 位置很重要：必须等所有清单内文件都已经落地之后才清理，
        // 否则会把马上要下回来的文件当成「清单外的多余文件」删掉
        applyMirrorRemovals(mirrorRemovals);

        // ---------- 6. 收尾 ----------
        writeVersion(versionFile, manifest.version);

        // 清单内文件都已应用、清单外文件也已清理完毕，最后再检查一次模组 modId 冲突
        // （镜像模式下此时通常已经无冲突可查，这里保留作为兜底）
        detectModConflicts(manifest);

        Log.info("更新完成：" + (localVersion.isEmpty() ? "(首次)" : localVersion) + " -> " + manifest.version);

        if (window != null) {
            window.setLabelText("更新完成，正在启动游戏");

            if (config.showHasUpdateMessage) {
                String content = String.format("已完成更新\r\n\r\n版本：%s -> %s\r\n下载：%d 个文件\r\n删除：%d 个文件",
                        localVersion.isEmpty() ? "(首次)" : localVersion,
                        manifest.version,
                        downloads.size(),
                        deletes.size());

                if (config.mirrorMode) {
                    content += String.format("\r\n镜像清理：%d 个清单外文件", mirrorRemovals.size());
                }

                JOptionPane.showMessageDialog(null, content, config.windowTitle, JOptionPane.INFORMATION_MESSAGE);
            }
        }

        return true;
    }

    /**
     * 把某个地址提到列表最前面，其余地址按原顺序跟在后面（自动去重）<p>
     * 用于文件下载的来源顺序：刚拉清单成功的源优先，其余按测速排名兜底
     */
    static List<String> rankWithFirst(String first, List<String> ranked, List<String> fallback) {
        List<String> result = new ArrayList<>();

        if (first != null && !first.trim().isEmpty()) {
            result.add(first);
        }

        if (ranked != null) {
            for (String url : ranked) {
                if (url != null && !url.trim().isEmpty() && !result.contains(url)) {
                    result.add(url);
                }
            }
        }

        if (fallback != null) {
            for (String url : fallback) {
                if (url != null && !url.trim().isEmpty() && !result.contains(url)) {
                    result.add(url);
                }
            }
        }

        return result;
    }

    /**
     * 下载单个文件到临时目录，整个过程跑在文件级并发池里的一个线程上<p>
     * 同一个文件的多个来源是串行尝试的，但不同文件之间互不影响：<p>
     * 这里只在所有来源都失败时才返回异常（由上层汇总），成功的文件不受别人连累<p>
     * 失败时会把自己已经记进总进度里的字节数减回去，避免进度条虚高
     */
    void downloadOne(ManifestFile f, Path tempDir, HostPort manifestBase, List<String> baseCandidates,
                     List<String> configUrls, AtomicLong done, long totalBytes, SpeedStat speed,
                     AtomicLong uiTimer, Object progressLock, List<String> failures) {
        Path temp = tempDir.resolve(f.path + ".temp");

        try {
            Files.createDirectories(temp.getParent());
        } catch (IOException e) {
            failures.add(f.path + "（无法创建临时目录：" + e.getMessage() + "）");
            return;
        }

        if (window != null) {
            window.setLabelSecondaryText(PathUtility.getFilename(f.path));
        }

        boolean ok = false;
        Exception fileError = null;
        int cdnAttempts = 0;

        // 本次同步的选源结果：测速判定该走服务端时，连 CDN 都不试（省掉一次注定浪费的请求）
        boolean useCdn = config.preferCdn
                && !(speedChoice != null && speedChoice.mode == SourceModeSelector.Mode.SERVER);

        // CDN 优先：清单里带了 http/https 绝对链接时，先直接走 CDN（同样按 download-threads 做 Range 分块并发），
        // 下载完照常校验 SHA-256；任何一步失败（连不上/超时/4xx/5xx/哈希不符）都只记日志，
        // 然后继续走下面的 MSFP 更新源流程，绝不影响整体同步
        if (useCdn) {
            if (isCdnExcluded(f.path, config.cdnExclude)) {
                Log.info("[服务端] " + PathUtility.getFilename(f.path) + "（命中 cdn-exclude）");
            } else {
                List<String> cdnUrls = cdnCandidates(f);

                cdnAttempts = cdnUrls.size();
                ok = downloadFromCdn(f, temp, cdnUrls, done, totalBytes, speed, uiTimer, progressLock);
            }
        } else if (config.preferCdn) {
            Log.info("[服务端] " + PathUtility.getFilename(f.path) + "（本次测速选了服务端，跳过 CDN）");
        } else {
            Log.info("[服务端] " + PathUtility.getFilename(f.path) + "（source-mode: server，跳过 CDN）");
        }

        List<String> sources = pickSources(f, manifestBase, baseCandidates, configUrls);

        if (!ok) {
            for (String raw : sources) {
                AtomicLong fileBytes = new AtomicLong();
                String label = raw;

                try {
                    TcpFileClient.Endpoint endpoint = TcpFileClient.resolve(raw, manifestBase, f.path);

                    label = endpoint.toString();

                    Log.debug("下载 " + f.path + " <- " + label);

                    if (Files.exists(temp)) {
                        Files.delete(temp);
                    }

                    TcpFileClient.download(endpoint.hostPort, endpoint.path, temp, config, (batch, received, total) ->
                            reportProgress(batch, fileBytes, done, speed, uiTimer, progressLock, totalBytes));

                    verifyHash(f, temp);

                    ok = true;
                    break;
                } catch (Exception e) {
                    fileError = e;

                    synchronized (progressLock) {
                        done.addAndGet(-fileBytes.get());
                    }

                    deleteQuietly(temp);
                    Log.warn("来源失败 " + label + " : " + describe(e));
                }
            }
        }

        if (!ok) {
            failures.add(f.path + "（已尝试 " + (sources.size() + cdnAttempts) + " 个来源："
                    + (fileError == null ? "未知错误" : fileError.getMessage()) + "）");
        }
    }

    /**
     * 依次尝试清单里该文件的 http/https 绝对链接，全部失败时返回 false（由调用方回退到更新源）<p>
     * 下载成功后会照常校验 SHA-256，校验不过也算失败
     */
    boolean downloadFromCdn(ManifestFile f, Path temp, List<String> cdnUrls, AtomicLong done, long totalBytes,
                            SpeedStat speed, AtomicLong uiTimer, Object progressLock) {
        String fileName = PathUtility.getFilename(f.path);

        for (String cdnUrl : cdnUrls) {
            AtomicLong fileBytes = new AtomicLong();

            try {
                Log.debug("下载 " + f.path + " <- " + cdnUrl);

                deleteQuietly(temp);

                CdnDownloader.download(cdnUrl, temp, config, cdnDownloadThreads(), (batch, received, total) ->
                        reportProgress(batch, fileBytes, done, speed, uiTimer, progressLock, totalBytes));

                verifyHash(f, temp);

                Log.info("[CDN] " + fileName + " 从 " + CdnDownloader.hostOf(cdnUrl) + " 下载成功");

                return true;
            } catch (Exception e) {
                synchronized (progressLock) {
                    done.addAndGet(-fileBytes.get());
                }

                deleteQuietly(temp);
                Log.warn("[CDN] " + fileName + " 失败（" + describe(e) + "），回退服务端");
            }
        }

        return false;
    }

    /**
     * 本次同步走 CDN 时用多少条分段连接<p>
     * 测速时若因为 CDN 慢而提过线程、并且提完线程后判定 CDN 够快，真实下载也要用那个更高的线程数，
     * 否则就成了「测出来 64 条连接够快、下载却还只开 32 条」，白测一轮
     */
    int cdnDownloadThreads() {
        int threads = Math.max(1, config.downloadThreads);

        if (speedChoice != null && speedChoice.mode == SourceModeSelector.Mode.CDN) {
            threads = Math.max(threads, Math.max(1, speedChoice.cdnThreads));
        }

        return threads;
    }

    /**
     * 挑一个测速样本<p>
     * 1. 优先从本次待下载列表里挑最大的、体积达到 speedtest-min-size-mb 的文件<p>
     * 2. 一个够大的待下载文件都没有时（大文件都已是最新），挑一个本地已有的大文件当影子样本：
     *    只从清单里读它的路径/大小/链接，不读也不改它的内容<p>
     * 挑不出来就返回 null，由上层跳过测速、按原有顺序尝试<p>
     * 被 cdn-exclude 命中、或者清单里根本没有 http(s) 直链的文件不能当样本（没有 CDN 可测）
     */
    SourceModeSelector.Sample pickSpeedtestSample(Manifest manifest, List<ManifestFile> downloads,
                                                  HostPort manifestBase, List<String> baseCandidates) {
        if (config == null || manifest == null) {
            return null;
        }

        // 1. 本次待下载列表里最大的够格文件
        SourceModeSelector.Sample best = null;

        if (downloads != null) {
            for (ManifestFile f : downloads) {
                SourceModeSelector.Sample candidate = buildSpeedtestSample(f, false, manifestBase, baseCandidates);

                if (candidate != null && (best == null || candidate.size > best.size)) {
                    best = candidate;
                }
            }
        }

        if (best != null) {
            Log.debug("测速样本选自本次待下载列表：" + best.path + "（" + best.size + " 字节）");
            return best;
        }

        // 2. 影子样本：本地已经是最新的那个大文件（只读清单信息，不碰本地文件）
        Set<String> needDownload = new HashSet<>();

        if (downloads != null) {
            for (ManifestFile f : downloads) {
                needDownload.add(f.path);
            }
        }

        for (ManifestFile f : manifest.files) {
            if (needDownload.contains(f.path)) {
                continue;
            }

            // 必须是本地真的已经有这个文件，否则「影子」就无从谈起
            if (baseDir == null || !Files.isRegularFile(baseDir.resolve(f.path))) {
                continue;
            }

            SourceModeSelector.Sample candidate = buildSpeedtestSample(f, true, manifestBase, baseCandidates);

            if (candidate != null && (best == null || candidate.size > best.size)) {
                best = candidate;
            }
        }

        if (best != null) {
            Log.debug("测速样本选自本地已有的大文件（影子样本）：" + best.path + "（" + best.size + " 字节）");
        }

        return best;
    }

    /**
     * 把一个清单条目变成测速样本，不合格（太小 / 命中 cdn-exclude / 没有 CDN 直链 / 地址解析失败）时返回 null
     */
    SourceModeSelector.Sample buildSpeedtestSample(ManifestFile f, boolean shadow, HostPort manifestBase,
                                                   List<String> baseCandidates) {
        if (f == null || manifestBase == null) {
            return null;
        }

        if (f.size < Math.max(1L, config.speedtestMinBytes())) {
            return null;
        }

        if (isCdnExcluded(f.path, config.cdnExclude)) {
            return null;
        }

        List<String> cdnUrls = cdnCandidates(f);

        if (cdnUrls.isEmpty()) {
            return null;
        }

        try {
            // 服务端一侧要用「真实下载最终会成功的那一个候选地址」，否则测的就不是下载会走的那条路：
            // 例如清单 urls 里写的是相对路径 files/mods/x.jar，而「更新源 + 清单路径 mods/x.jar」在很多
            // 部署里根本不存在，直接拿第一个候选去测只会测出一个 not found
            List<String> sources = pickSources(f, manifestBase, baseCandidates, config.urls);
            TcpFileClient.Endpoint endpoint = resolveWorkingEndpoint(f, manifestBase, sources);

            if (endpoint == null) {
                Log.debug("样本 " + f.path + " 在所有候选更新源上都取不到，跳过");
                return null;
            }

            SourceModeSelector.Sample sample = new SourceModeSelector.Sample();

            sample.path = f.path;
            sample.name = PathUtility.getFilename(f.path);
            sample.size = f.size;
            sample.cdnUrl = cdnUrls.get(0);
            sample.shadow = shadow;
            sample.server = endpoint;

            return sample;
        } catch (Exception e) {
            Log.debug("样本 " + f.path + " 的地址解析失败，跳过：" + e.getMessage());
            return null;
        }
    }

    /**
     * 在候选来源里找一个「服务端上真的有这个文件」的地址，给测速用<p>
     * 最多试前 4 个候选，每个只发一次 SIZE（不重试），所以代价很小；
     * 一个都取不到时返回 null，上层会跳过测速、回退到原有顺序
     */
    TcpFileClient.Endpoint resolveWorkingEndpoint(ManifestFile f, HostPort manifestBase, List<String> candidates) {
        // 探路用的配置：只关心「能不能连上、有没有这个文件」，超时和重试都压到最小
        AppConfig probe = new AppConfig(new HashMap<>());

        probe.tcpTimeout = (int) Math.min(Math.max(1000, config.tcpTimeout), 5000);
        probe.reties = 1;

        int tried = 0;

        for (String raw : candidates) {
            if (tried >= 4) {
                break;
            }

            tried += 1;

            try {
                TcpFileClient.Endpoint endpoint = TcpFileClient.resolve(raw, manifestBase, f.path);

                TcpFileClient.sizeOnce(endpoint.hostPort, endpoint.path, probe);

                Log.debug("测速将使用服务端地址：" + endpoint);

                return endpoint;
            } catch (Exception e) {
                Log.debug("样本候选地址不可用：" + raw + "（" + describe(e) + "）");
            }
        }

        return null;
    }

    /**
     * 挑出清单里该文件的 CDN 直链（http/https 绝对地址），保持清单里的顺序并去重
     */
    static List<String> cdnCandidates(ManifestFile f) {
        List<String> result = new ArrayList<>();

        if (f == null || f.urls == null) {
            return result;
        }

        for (String url : f.urls) {
            if (url == null) {
                continue;
            }

            String value = url.trim();

            if (CdnDownloader.isHttpUrl(value) && !result.contains(value)) {
                result.add(value);
            }
        }

        return result;
    }

    /**
     * 判断某个文件是否命中 cdn-exclude（强制走更新源，不尝试 CDN）<p>
     * 配置项可以写成文件名通配（Flashback-*.jar、*.jar）或相对路径（mods/xxx.jar、mods/lib/*.jar），
     * 带 / 的按清单完整路径匹配，不带的只按文件名匹配，均不区分大小写
     */
    static boolean isCdnExcluded(String manifestPath, List<String> patterns) {
        if (manifestPath == null || patterns == null || patterns.isEmpty()) {
            return false;
        }

        String path = manifestPath.replace('\\', '/');
        String name = path.substring(path.lastIndexOf('/') + 1);

        for (String raw : patterns) {
            if (raw == null) {
                continue;
            }

            String pattern = raw.trim().replace('\\', '/');

            if (pattern.isEmpty()) {
                continue;
            }

            String subject = pattern.contains("/") ? path : name;

            if (globToPattern(pattern).matcher(subject).matches()) {
                return true;
            }
        }

        return false;
    }

    /**
     * 把 cdn-exclude 里的一项（支持 * 和 ? 通配）编译成正则，编译结果会缓存复用
     */
    static Pattern globToPattern(String glob) {
        return GLOB_CACHE.computeIfAbsent(glob, key -> {
            StringBuilder regex = new StringBuilder();

            for (char c : key.toCharArray()) {
                if (c == '*') {
                    regex.append(".*");
                } else if (c == '?') {
                    regex.append('.');
                } else {
                    regex.append(Pattern.quote(String.valueOf(c)));
                }
            }

            return Pattern.compile(regex.toString(), Pattern.CASE_INSENSITIVE);
        });
    }

    /**
     * 更新进度：把本次批次累加进总进度与文件进度、喂给测速统计，并按需刷新界面<p>
     * 多文件并发（甚至 CDN 分块并发）时会被多个线程同时调用，全部状态都在 progressLock 里更新
     */
    void reportProgress(long batch, AtomicLong fileBytes, AtomicLong done, SpeedStat speed, AtomicLong uiTimer,
                        Object progressLock, long totalBytes) {
        synchronized (progressLock) {
            done.addAndGet(batch);
            fileBytes.addAndGet(batch);
            speed.feed(batch);

            if (window != null) {
                long now = System.currentTimeMillis();

                if (now - uiTimer.get() > 300) {
                    uiTimer.set(now);

                    long d = done.get();
                    window.setProgressBarText(String.format("%s/%s  -  %s/s",
                            BytesUtils.convertBytes(d),
                            BytesUtils.convertBytes(totalBytes),
                            speed.sampleSpeed2()));
                    window.setProgressBarValue(totalBytes > 0 ? (int) (d / (float) totalBytes * 1000) : 0);
                }
            }
        }
    }

    /**
     * 校验下载到临时文件的 SHA-256（清单里没写 sha256 时跳过）
     */
    static void verifyHash(ManifestFile f, Path temp) throws IOException {
        if (!f.sha256.isEmpty()) {
            String actual = HashUtil.sha256(temp);

            if (!actual.equalsIgnoreCase(f.sha256)) {
                throw new IOException("SHA-256 校验失败（期望 " + f.sha256 + "，实际 " + actual + "）");
            }
        }
    }

    /**
     * 取异常的简短描述，用于日志
     */
    static String describe(Exception e) {
        String message = e.getMessage();

        return message == null || message.isEmpty() ? e.getClass().getSimpleName() : message;
    }

    /**
     * 算出一个文件可以尝试的来源列表，按优先级排列<p>
     * 1. 优先用测速选出来的更新源（可能和清单所在源不是同一个），按排名靠前的排前面<p>
     * 2. 再试清单所在源<p>
     * 3. 最后保留清单里自己声明的 urls 作为兜底<p>
     * 相对路径会拼到对应的源上，绝对地址（msfp://host:port 或裸 host:port）不受前面这些影响
     */
    List<String> pickSources(ManifestFile f, HostPort manifestBase, List<String> baseCandidates, List<String> configUrls) {
        List<String> bases = baseCandidates == null || baseCandidates.isEmpty()
                ? (configUrls == null ? new ArrayList<>() : configUrls)
                : baseCandidates;

        List<String> result = new ArrayList<>();

        for (String base : bases) {
            if (base != null && !base.trim().isEmpty() && !result.contains(base)) {
                result.add(base);
            }
        }

        if (manifestBase != null) {
            String text = manifestBase.toString();

            if (!result.contains(text)) {
                result.add(text);
            }
        }

        if (f.urls != null) {
            for (String url : f.urls) {
                if (url == null || url.trim().isEmpty() || result.contains(url)) {
                    continue;
                }

                // prefer-cdn 打开时，清单里的绝对 http(s) 链接由 CDN 阶段负责；
                // 它们不是 MSFP 地址，当成相对路径发给更新源只会白失败一次
                if (config != null && config.preferCdn && CdnDownloader.isHttpUrl(url)) {
                    continue;
                }

                result.add(url);
            }
        }

        // 兜底：一个来源都没有时用「清单所在源 + 清单里的路径」
        if (result.isEmpty()) {
            result.add("");
        }

        return result;
    }

    /**
     * 文件级并发下载池的线程工厂，同样起成守护线程
     */
    static class FileDownloadThreadFactory implements ThreadFactory {
        final AtomicInteger counter = new AtomicInteger();

        @Override
        public Thread newThread(Runnable runnable) {
            Thread thread = new Thread(runnable, "msfp-file-" + counter.incrementAndGet());
            thread.setDaemon(true);

            return thread;
        }
    }

    /**
     * 镜像模式：规划 mods 目录里「清单里没有」的文件<p>
     * 只扫描 mods 目录这一层（与 modId 冲突检测的扫描范围保持一致，不递归子目录），<p>
     * 以清单里所有 mods/ 开头的 path 作为白名单；白名单之外的文件（含 .disabled 结尾的）都要清理。<p>
     * 这里只负责找出要清理的文件，不执行任何删除：真正的清理必须等下载应用完成之后再做<p>
     * 安全兜底：清单里一个 mods/ 条目都没有时绝不清理（只警告），避免服务端清单配错把玩家的 mods 清空
     *
     * @param manifest 本次生效的清单
     * @return 待清理的本地文件列表；未开启镜像模式或触发兜底时为空列表
     */
    List<Path> planMirrorRemovals(Manifest manifest) {
        List<Path> removals = new ArrayList<>();

        mirrorScanSkipped = false;

        if (config == null || !config.mirrorMode || manifest == null || baseDir == null) {
            Log.debug("镜像模式（mirror-mode=false）：清单外的文件不会被清理，只维护清单内文件与 deletes 列表");
            return removals;
        }

        Log.openIndent("镜像模式");

        try {
            // 白名单 = 清单里所有 mods/ 开头的路径（不限于 .jar，清单说什么就留什么）
            Set<String> whitelist = new LinkedHashSet<>();

            for (ManifestFile f : manifest.files) {
                if (f.path != null && isModsPath(f.path)) {
                    whitelist.add(f.path.replace('\\', '/'));
                }
            }

            Log.info("镜像模式已开启（mirror-backup=" + config.mirrorBackup + "）：mods 目录必须与服务端清单完全一致，"
                    + (config.mirrorBackup ? "清单外文件会被移动到 " + MIRROR_BACKUP_DIR + " 备份目录" : "清单外文件会被直接删除"));

            Path modsDir = baseDir.resolve("mods");
            List<Path> localFiles = new ArrayList<>();

            // 只扫描 mods 这一层，不递归子目录
            if (Files.isDirectory(modsDir)) {
                try (DirectoryStream<Path> stream = Files.newDirectoryStream(modsDir)) {
                    for (Path file : stream) {
                        if (Files.isRegularFile(file)) {
                            localFiles.add(file);
                        }
                    }
                }
            }

            // 兜底一：清单里一个 mods/ 条目都没有，说明服务端清单很可能配错了，绝不动玩家的 mods 目录
            if (whitelist.isEmpty()) {
                mirrorScanSkipped = true;

                Log.warn("清单里没有任何 mods/ 条目（白名单 0 个），已跳过镜像清理以免误删玩家模组！");
                Log.warn("本地 mods 目录里的 " + localFiles.size() + " 个文件全部保持不动，请检查服务端分发目录是否配置正确");
                return removals;
            }

            // deletes 里列出的文件由原来的删除逻辑负责，这里不重复统计，免得同一个文件被处理两次
            Set<String> deletePaths = new HashSet<>();

            for (String d : manifest.deletes) {
                if (d != null) {
                    deletePaths.add(d.replace('\\', '/'));
                }
            }

            // 防御：绝不清理更新器自己和日志文件
            Set<Path> protectedPaths = new HashSet<>();
            Path currentJar = Env.getJarPath();

            if (currentJar != null) {
                protectedPaths.add(currentJar.normalize());
            }

            if (logFilePath != null) {
                protectedPaths.add(logFilePath.normalize());
            }

            for (Path file : localFiles) {
                String relative = toManifestPath(file);

                // 白名单内保留，deletes 里的交给原删除逻辑，更新器自己和日志文件也不碰
                if (whitelist.contains(relative) || deletePaths.contains(relative)
                        || protectedPaths.contains(file.normalize())) {
                    continue;
                }

                removals.add(file);
            }

            int kept = localFiles.size() - removals.size();

            // 必须让人一眼能核对：白名单多少个、本地多少个、将清理多少个
            Log.info("白名单 " + whitelist.size() + " 个 / 本地 " + localFiles.size()
                    + " 个 / 将清理 " + removals.size() + " 个（清理后本地保留 " + kept + " 个）");

            for (Path file : removals) {
                if (isDisabledName(file)) {
                    Log.info("  待清理：" + toManifestPath(file) + "（.disabled 文件不在服务端清单里，同样清理）");
                } else {
                    Log.info("  待清理：" + toManifestPath(file));
                }
            }

            // 兜底二：要清掉的比留下的还多、而且留下的不足 5 个，说明这一次几乎会清空 mods 目录。
            // 这里选择「醒目警告但仍然执行」，理由：镜像模式本身就是用户显式开启的高危配置，
            // 服务端整批换 mod 时确实会一次清掉大量旧文件，再加一道开关会让正常更新无法自动完成；
            // 而 mirror-backup 默认开启，被清掉的文件都能从备份目录里找回，风险已经可控
            if (!removals.isEmpty() && removals.size() > kept && kept < 5) {
                Log.warn("============================== 警告 ==============================");
                Log.warn("本次将清理 " + removals.size() + " 个清单外文件，仅保留 " + kept + " 个，mods 目录几乎会被清空！");
                Log.warn("请确认服务端清单是否正确；"
                        + (config.mirrorBackup
                        ? "当前 mirror-backup=true，被清理的文件可在 " + MIRROR_BACKUP_DIR + " 目录里找回"
                        : "当前 mirror-backup=false，删除不可恢复，建议临时改成 true"));
                Log.warn("================================================================");
            }
        } catch (Exception e) {
            // 镜像清理属于额外的强一致手段，任何意外都只记录日志，绝不打断正常同步
            Log.warn("镜像模式扫描失败，本次不执行清理：" + e);
            removals.clear();
        } finally {
            Log.closeIndent();
        }

        return removals;
    }

    /**
     * 执行镜像清理：按 {@link #planMirrorRemovals} 规划好的列表逐个处理<p>
     * mirror-backup=true 时移动到备份目录（保留相对路径结构），false 时直接删除<p>
     * 单个文件失败只记日志，不影响其它文件，也不影响进游戏
     *
     * @param removals 规划好的待清理文件
     * @return 实际处理成功的文件数
     */
    int applyMirrorRemovals(List<Path> removals) {
        if (config == null || !config.mirrorMode || removals == null || removals.isEmpty()) {
            return 0;
        }

        Log.openIndent("镜像模式清理");

        try {
            Path backupRoot = baseDir.resolve(MIRROR_BACKUP_DIR);
            int backedUp = 0;
            int deleted = 0;
            int failed = 0;

            for (Path file : removals) {
                if (!Files.isRegularFile(file)) {
                    // 规划之后已经被别的逻辑（例如 deletes）处理掉了，属于正常情况
                    Log.debug("文件已不存在，跳过镜像清理：" + toManifestPath(file));
                    continue;
                }

                String relative = toManifestPath(file);

                try {
                    if (config.mirrorBackup) {
                        // 备份目录里保留相对路径结构，例如 mods/a.jar -> .modsync-removed/mods/a.jar
                        Path target = resolveBackupTarget(backupRoot, relative);

                        Files.createDirectories(target.getParent());
                        Files.move(file, target);

                        backedUp += 1;

                        Log.info("镜像模式：已移除清单外文件 " + relative + " -> "
                                + baseDir.relativize(target).toString().replace('\\', '/'));
                    } else {
                        PathUtility.delete(file);

                        deleted += 1;

                        Log.info("镜像模式：已删除清单外文件 " + relative);
                    }
                } catch (Exception e) {
                    failed += 1;

                    Log.warn("镜像模式：清理 " + relative + " 失败：" + e.getMessage());
                }
            }

            Log.info("镜像模式清理完成：共处理 " + (backedUp + deleted) + " 个清单外文件（备份 "
                    + backedUp + " 个，直接删除 " + deleted + " 个" + (failed > 0 ? "，失败 " + failed + " 个" : "") + "）");

            return backedUp + deleted;
        } catch (Exception e) {
            Log.warn("镜像模式清理失败：" + e);
            return 0;
        } finally {
            Log.closeIndent();
        }
    }

    /**
     * 算出备份目标路径。备份目录里已经有同名文件时加时间戳后缀（还冲突就再加序号），绝不覆盖已有备份
     *
     * @param backupRoot 备份根目录
     * @param relative   被清理文件相对于游戏目录的路径，例如 mods/a.jar
     */
    static Path resolveBackupTarget(Path backupRoot, String relative) {
        Path target = backupRoot.resolve(relative).normalize();

        if (!Files.exists(target)) {
            return target;
        }

        String name = target.getFileName().toString();
        int dot = name.lastIndexOf('.');
        String base = dot > 0 ? name.substring(0, dot) : name;
        String ext = dot > 0 ? name.substring(dot) : "";
        String stamp = new SimpleDateFormat("yyyyMMdd-HHmmss").format(new Date());

        Path candidate = target.resolveSibling(base + "." + stamp + ext);
        int index = 2;

        while (Files.exists(candidate)) {
            candidate = target.resolveSibling(base + "." + stamp + "-" + index + ext);
            index += 1;
        }

        return candidate;
    }

    /**
     * 判断清单里的路径是不是 mods 目录下的条目（镜像白名单的判定依据）
     */
    static boolean isModsPath(String manifestPath) {
        return manifestPath.replace('\\', '/').toLowerCase(Locale.ROOT).startsWith("mods/");
    }

    /**
     * 判断本地文件名是不是 .disabled 结尾（玩家或冲突检测改名后留下的文件）
     */
    static boolean isDisabledName(Path file) {
        return file.getFileName().toString().toLowerCase(Locale.ROOT).endsWith(".disabled");
    }

    /**
     * 扫描 mods 目录，检测多个 jar 声明了同一个 modId 的情况<p>
     * 清单托管的那一个保留，其余（玩家自加的）重命名为 xxx.jar.disabled，避免 NeoForge 因 Duplicate mod 直接崩溃<p>
     * 如果某个 modId 的 jar 全部都是玩家自加的，则无法判断该留哪个，只打日志警告不做处理<p>
     * 任何失败都只记录日志，绝不打断整个同步流程
     *
     * @param manifest 本次生效的清单
     */
    void detectModConflicts(Manifest manifest) {
        if (config == null || !config.detectModConflicts || manifest == null || baseDir == null) {
            return;
        }

        if (config.mirrorMode) {
            Log.debug("镜像模式已开启：清单外文件已在镜像清理阶段处理完毕，这里的 modId 冲突检测只作兜底");
        } else {
            Log.debug("镜像模式未开启：不清理清单外文件，只把与服务器托管模组同 modId 的玩家模组改名 .disabled");
        }

        try {
            Path modsDir = baseDir.resolve("mods");

            if (!Files.isDirectory(modsDir)) {
                return;
            }

            // 清单里托管的 mods 文件路径，清单里统一使用正斜杠，例如 mods/create.jar
            Set<String> managedPaths = new HashSet<>();

            for (ManifestFile f : manifest.files) {
                if (f.path != null && isModJar(f.path)) {
                    managedPaths.add(f.path.replace('\\', '/'));
                }
            }

            // modId -> 声明了它的 jar 列表
            Map<String, List<Path>> modIdMap = new LinkedHashMap<>();

            // 只扫描 mods 目录这一层，不递归
            try (DirectoryStream<Path> stream = Files.newDirectoryStream(modsDir)) {
                for (Path jar : stream) {
                    if (!Files.isRegularFile(jar)) {
                        continue;
                    }

                    String name = jar.getFileName().toString();

                    // 已经被禁用掉的 xxx.jar.disabled 不会再被扫到
                    if (!name.toLowerCase(Locale.ROOT).endsWith(".jar")) {
                        continue;
                    }

                    for (String id : ModIdReader.readModIds(jar)) {
                        modIdMap.computeIfAbsent(id, k -> new ArrayList<>()).add(jar);
                    }
                }
            }

            List<String> disabled = new ArrayList<>();

            for (Map.Entry<String, List<Path>> entry : modIdMap.entrySet()) {
                List<Path> jars = entry.getValue();

                if (jars.size() < 2) {
                    continue;
                }

                List<Path> managed = new ArrayList<>();
                List<Path> unmanaged = new ArrayList<>();

                for (Path jar : jars) {
                    if (managedPaths.contains(toManifestPath(jar))) {
                        managed.add(jar);
                    } else {
                        unmanaged.add(jar);
                    }
                }

                // 全都是玩家自加的，无法判断该保留哪一个，只能警告
                if (managed.isEmpty()) {
                    Log.warn("检测到 " + jars.size() + " 个模组共用 modId=" + entry.getKey()
                            + "，但它们都不在服务器清单里，无法判断该保留哪一个，已跳过：");

                    for (Path jar : jars) {
                        Log.warn("  " + jar.getFileName());
                    }

                    continue;
                }

                // 清单自己就重复了，那属于服务端的问题，这里一个都不动
                if (unmanaged.isEmpty()) {
                    Log.warn("服务器清单里有 " + managed.size() + " 个文件共用 modId=" + entry.getKey() + "，均保持不动");
                    continue;
                }

                // 托管的所有 jar 都保留，把玩家自加的（会引起 Duplicate mod 崩溃的）禁用掉
                for (Path jar : unmanaged) {
                    String name = jar.getFileName().toString();
                    Path target = jar.resolveSibling(name + ".disabled");

                    try {
                        if (Files.exists(target)) {
                            Files.delete(target);
                        }

                        Files.move(jar, target);

                        disabled.add(name);

                        Log.info("模组 modId 冲突：已自动禁用玩家自加模组 " + name
                                + "（modId=" + entry.getKey() + "，与服务器模组重复）");
                    } catch (Exception e) {
                        Log.warn("禁用冲突模组失败 " + name + ": " + e.getMessage() + "，已跳过该文件");
                    }
                }
            }

            if (!disabled.isEmpty() && window != null) {
                StringBuilder text = new StringBuilder("以下模组因与服务器模组冲突已被自动禁用：\r\n");

                for (String name : disabled) {
                    text.append("  ").append(name).append("\r\n");
                }

                text.append("\r\n如需恢复请删除文件名的 .disabled 后缀");

                JOptionPane.showMessageDialog(null, text.toString(), config.windowTitle, JOptionPane.WARNING_MESSAGE);
            }
        } catch (Exception e) {
            // 冲突检测只是锦上添花，任何意外都不能影响正常进游戏
            Log.warn("模组 modId 冲突检测失败: " + e);
        }
    }

    /**
     * 判断清单里的路径是不是 mods 目录下的 jar
     */
    static boolean isModJar(String manifestPath) {
        String path = manifestPath.replace('\\', '/').toLowerCase(Locale.ROOT);

        return path.startsWith("mods/") && path.endsWith(".jar");
    }

    /**
     * 把本地文件路径转换成清单里那种使用正斜杠的相对路径
     */
    String toManifestPath(Path file) {
        try {
            return baseDir.relativize(file).toString().replace('\\', '/');
        } catch (Exception e) {
            // 兜底：mods 目录下只扫描一层，所以直接用 mods/ 前缀拼文件名
            return "mods/" + file.getFileName();
        }
    }

    void writeVersion(Path versionFile, String version) {
        if (version == null || version.isEmpty()) {
            return;
        }

        try {
            if (versionFile.getParent() != null) {
                Files.createDirectories(versionFile.getParent());
            }
            Files.write(versionFile, version.getBytes(StandardCharsets.UTF_8));
        } catch (IOException e) {
            Log.error("写入版本号失败: " + e.getMessage());
        }
    }

    static void deleteQuietly(Path path) {
        try {
            if (Files.exists(path)) {
                Files.delete(path);
            }
        } catch (IOException ignored) {
        }
    }
}
