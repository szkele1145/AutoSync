package com.github.balloonupdate.mcpatch.client.config;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * 配置文件对象，保存所有从配置文件里读取出来的配置项
 */
public class AppConfig {
    /**
     * 下载来源模式：CDN 直链与更新源（服务端）之间怎么选
     */
    public enum SourceMode {
        /**
         * 每次同步开始时测速一次，按测速结果自动在 CDN 与服务端之间选一个（默认）
         */
        AUTO,

        /**
         * 强制走 CDN 直链（不测速）；CDN 下载失败时仍然照常回退到服务端，不影响整体同步
         */
        CDN,

        /**
         * 强制走服务端更新源，完全跳过 CDN（不测速、不尝试直链）
         */
        SERVER,
    }

    /**
     * 更新服务器地址列表
     */
    public List<String> urls;

    /**
     * 记录客户端版本号文件的路径<p>
     * 客户端的版本号会被存储在这个文件里，并以此为依据判断是否更新到了最新版本
     */
    public String versionFilePath;

    /**
     * 当程序发生错误而更新失败时，是否可以继续进入游戏<p>
     * 如果为true，发生错误时会忽略错误，正常启动游戏，但是可能会因为某些新模组未下载无法进服<p>
     * 如果为false，发生错误时会直接崩溃掉Minecraft进程，停止游戏启动过程<p>
     * 此选项仅当程序以非图形模式启动时有效，因为在图形模式下，会主动弹框并将选择权交给用户
     */
    public boolean allowError;

    /**
     * 在没有更新时，是否显示“资源文件暂无更新!”提示框
     */
    public boolean showNoUpdateMessage;

    /**
     * 在有更新时，是否显示更新日志提示框
     */
    public boolean showHasUpdateMessage;

    /**
     * 自动关闭更新日志的时间，单位为毫秒。设置为0代表不会自动关闭更新日志窗口，需要手点
     */
    public int autoCloseChangelogs;

    /**
     * 安静模式，是否只在下载文件时才显示窗口<p>
     * 如果为true，程序启动后在后台静默检查文件更新，而不显示窗口，若没有更新会直接启动Minecraft，<p>
     *            有更新的话再显示下载进度条窗口，此选项可以尽可能将程序的存在感降低（适合线上环境）<p>
     * 如果为false，每次都正常显示窗口（适合调试环境）<p>
     * 此选项仅当程序以图形模式启动时有效
     */
    public boolean silentMode;

    /**
     * 禁用主题
     */
    public boolean disableTheme;

    /**
     * 窗口标题，可以自定义更新时的窗口标题<p>
     * 只有在桌面环境上时才有效，因为非桌面环境没法弹出窗口
     */
    public String windowTitle;

    /**
     * 更新的起始目录，也就是要把文件都更新到哪个目录下<p>
     * 默认情况下程序会智能搜索，并将所有文件更新到.minecraft父目录下（也是启动主程序所在目录），<p>
     * 这样文件更新的位置就不会随主程序文件的工作目录变化而改变了，每次都会更新在相同目录下。<p>
     * 如果你不喜欢这个智能搜索的机制，可以修改此选项来把文件更新到别的地方（十分建议保持默认不要修改）<p>
     * 1. 当此选项的值是空字符串''时，会智能搜索.minecraft父目录作为更新起始目录（这也是默认值）<p>
     * 2. 当此选项的值是'.'时，会把当前工作目录作为更新起始目录<p>
     * 3. 当此选项的值是'..'时，会把当前工作目录的上级目录作为更新起始目录<p>
     * 4. 当此选项的值是别的时，比如'ab/cd'时，会把当前工作目录下的ab目录里面的cd目录作为更新起始目录
     */
    public String basePath;

    /**
     * 私有协议的超时判定时间，单位毫秒，值越小判定越严格<p>
     * 网络环境较差时可能会频繁出现连接超时，那么此时可以考虑增加此值（建议30s以下）<p>
     */
    public int privateTimeout;

    /**
     * 为http系的协议设置自定义headers
     */
    public Map<String, String> httpHeaders;

    /**
     * http系的协议连接超时判定时间，单位毫秒，值越小判定越严格<p>
     * 网络环境较差时可能会频繁出现连接超时，那么此时可以考虑增加此值（建议30s以下）
     */
    public int httpTimeout;

    /**
     * MSFP（自定义 TCP 文件传输协议）的连接超时与读超时，单位毫秒，默认15000<p>
     * 连接超时指 TCP 建连最多等多久；读超时指两次收到数据之间最多等多久<p>
     * 大文件下载只要还在持续收数据就不会超时，所以这里可以放得比 http-timeout 宽松一些
     */
    public int tcpTimeout;

    /**
     * 出现网络问题时的重试次数，适用于所有协议，最大值不建议超过100<p>
     * 当服务器没有及时响应数据时，会消耗1次重试次数，然后进行重新连接<p>
     * 当所有的重试次数消耗完后，程序才会真正判定为超时，并弹出网络错误对话框<p>
     * 建议 timeout * retries 的总时间控制在20秒以内，避免玩家等的太久
     */
    public int reties;

    /**
     * 忽略对http系列协议的证书验证（如果开启了https）
     */
    public boolean ignoreSSLCertificate;

    /**
     * 多线程分块下载的线程数（也就是单个文件的最大分段数），默认32<p>
     * 当服务器/CDN支持 Range 请求并且文件大小超过了下面的 downloadThreshold 时，<p>
     * 会把文件切成这么多个分段并发下载，用来绕过单条连接被限速的问题<p>
     * 设置为1则等同于关闭多线程（始终使用原来的单线程整文件下载）<p>
     * 注意：峰值连接数 ≈ concurrent-files × download-threads，调大前先确认隧道/家宽扛得住
     */
    public int downloadThreads;

    /**
     * 启用多线程分块下载的最小文件大小，单位字节，默认1MB<p>
     * 小于此值的文件直接单线程下载，因为分段带来的开销不划算
     */
    public int downloadThreshold;

    /**
     * 文件级并发下载数，也就是同时下载多少个文件，默认2<p>
     * 原来是一个文件一个文件地串行下载，公网 RTT 下光是 SIZE 请求和 TCP 建连就要等很久，
     * 打开文件级并发后可以把这个纯等待时间重叠掉<p>
     * 注意：最大同时在用的连接数大约是 concurrent-files × download-threads（小文件只占 1 条连接），
     * 默认就是 4 × 8 = 32 条，已经足够打满普通家宽，继续加大只会打爆服务端和本机，不建议超过 8
     */
    public int concurrentFiles;

    /**
     * 测试模式，开启后每次都会重头更新，会增加流量消耗，仅用来测试更新时网速
     */
    public boolean testMode;

    /**
     * 是否检测并自动禁用与服务器托管模组 modId 冲突的玩家自加模组<p>
     * 开启后，如果 mods 目录里存在与清单托管模组 modId 相同的非托管 jar，<p>
     * 会被重命名为 xxx.jar.disabled，避免 NeoForge 因 Duplicate mod 直接崩溃
     */
    public boolean detectModConflicts;

    /**
     * 镜像模式：是否让客户端 mods 目录与服务端清单「完全一致」<p>
     * false（默认）= 保持原有行为，只维护清单里列出的文件，清单外的文件一概不碰<p>
     * true = 镜像模式，mods 目录里所有不在清单白名单中的文件都会被清理（移到备份目录或直接删除）<p>
     * 注意：清单里一个 mods/ 条目都没有时绝不会执行清理（视为服务端清单配错），只打警告
     */
    public boolean mirrorMode;

    /**
     * 镜像模式下清理清单外文件的方式<p>
     * true（默认）= 移动到备份目录 &lt;baseDir&gt;/.modsync-removed/ 下（保留相对路径结构），可人工找回<p>
     * false = 直接删除，不可恢复
     */
    public boolean mirrorBackup;

    /**
     * 是否在启动时对所有更新源并发测速，并自动选择最优源<p>
     * 关闭后退回原来的行为：按 urls 的顺序依次尝试
     */
    public boolean autoSelectSource;

    /**
     * 手动锁定的更新源，填写后跳过自动测速，直接把它作为首选更新源<p>
     * 留空表示不使用手动锁定
     */
    public String manualSource;

    /**
     * 综合评分中「吞吐」的权重，默认 0.8（默认偏向吞吐，因为玩家痛点是下载慢）
     */
    public double sourceWeightSpeed;

    /**
     * 综合评分中「延迟」的权重，默认 0.2
     */
    public double sourceWeightLatency;

    /**
     * 测速结果的缓存时间，单位秒，默认 3600<p>
     * 缓存未过期时直接复用上次的测速结果，避免每次启动都重新测速
     */
    public int sourceCacheSeconds;

    /**
     * 单个更新源测速的超时时间，单位毫秒，默认 5000<p>
     * 某个源超时不会拖累其它源的测速，只会被判定为不可用或者吞吐很低
     */
    public int sourceProbeTimeout;

    /**
     * 测速文件名，相对于更新源根目录，默认 speedtest.bin（建议 256KB ~ 1MB）<p>
     * 该文件不存在（404）时，该源降级为只测 RTT
     */
    public String sourceSpeedtestFile;

    /**
     * 是否启用多源分块聚合下载<p>
     * 开启后，一个大文件的分块会按轮转方式分配给多个更新源，用来叠加多条线路的带宽<p>
     * 前提是所有源上的文件内容一致（仍然保留 SHA-256 校验兜底）
     */
    public boolean multiSourceDownload;

    /**
     * 下载来源模式，由 source-mode 配置项决定（默认 AUTO，每次同步测速后自动选）<p>
     * 兼容旧的 prefer-cdn 写法：只写了 prefer-cdn 时，true 等价于 CDN、false 等价于 SERVER；
     * 两个都写时以 source-mode 为准
     */
    public SourceMode sourceMode;

    /**
     * source-mode 在配置文件里的原始写法，只用于日志（未配置时为空串）
     */
    public String sourceModeSource;

    /**
     * 测速样本的最小体积，单位 MB，默认 10<p>
     * 只有体积不小于它的文件才会被挑来当 CDN/服务端测速样本，太小的文件测不出真实带宽
     */
    public double speedtestMinSizeMb;

    /**
     * 每个来源的测速时长，单位毫秒，默认 2000<p>
     * CDN 与服务端是并发测的，各自在这个时间窗口内能传多少字节就换算出多少 MB/s
     */
    public int speedtestDurationMs;

    /**
     * CDN 速度达到服务端的这个比例就算「够快」，直接选 CDN，默认 0.5<p>
     * 选 CDN 的判据是 cdn_speed &gt;= server_speed * cdn-prefer-ratio：
     * 比例调小更偏向 CDN（省服务端带宽），调到 1 以上表示「CDN 必须比服务端更快才用它」
     */
    public double cdnPreferRatio;

    /**
     * CDN 测速太慢时提线程重测的上限，默认 64<p>
     * 第一次测速按 download-threads 条连接并发，若判定 CDN 太慢就会把连接数翻倍重测一次，
     * 但不会超过这个上限；比 download-threads 还小就等于关掉重测
     */
    public int cdnMaxThreads;

    /**
     * 是否优先用清单里的 CDN 链接（http/https 绝对地址）下载文件<p>
     * 这是 source-mode 的派生结果，保留下来是为了让下载逻辑少一层判断：<p>
     * source-mode = server 时为 false（完全忽略清单里的绝对链接，全部走更新源）；<p>
     * source-mode = cdn / auto 时为 true（auto 下具体走不走 CDN 还要看本次测速结果）
     */
    public boolean preferCdn;

    /**
     * 强制走更新源、不尝试 CDN 的文件名单，默认空<p>
     * 每一项可以是文件名通配（例如 Flashback-*.jar、*.jar）或相对路径（例如 mods/xxx.jar），
     * 也支持通配的相对路径（例如 mods/lib/*.jar）；匹配时不区分大小写<p>
     * 典型用途：自托管、CDN 上本来就没有的文件，省掉一次注定失败的 CDN 请求
     */
    public List<String> cdnExclude;

    /**
     * CDN（http/https 清单链接）下载的连接超时与读超时，单位毫秒，默认15000<p>
     * 与 tcp-timeout 一样：只要还在持续收到数据就不会超时，避免 CDN 卡住整个同步
     */
    public int cdnTimeout;


    public AppConfig(Map<String, Object> map) {
        List<String> urls = getList(map, "urls", null, new ArrayList<>());
        String versionFilePath = getString(map, "version-file-path", null, "version-label.txt");
        boolean allowError = getBoolean(map, "allow-error", null, false);
        boolean showNoUpdateMessage = getBoolean(map, "show-no-update-message", "show-finish-message", true);
        boolean showHasUpdateMessage = getBoolean(map, "show-has-update-message", "show-finish-message", true);
        int autoCloseChangelogs = getInt(map, "auto-close-changelogs", null, 0);
        boolean silentMode = getBoolean(map, "silent-mode", null, false);
        boolean disableTheme = getBoolean(map, "disable-theme", null, false);
        String windowTitle = getString(map, "window-title", "changelogs_window_title", "AutoSync");
        String basePath = getString(map, "base-path", null, "");
        int privateTimeout = getInt(map, "private-timeout", null, 7000);
        Map<String, String> httpHeaders = getMap(map, "http-headers", null, new HashMap<>());
        int httpTimeout = getInt(map, "http-timeout", null, 7000);
        int tcpTimeout = getInt(map, "tcp-timeout", null, 15000);
        int reties = getInt(map, "retries", "http-retries", 3);
        boolean ignoreSSLCertificate = getBoolean(map, "ignore-ssl-cert", "http-ignore-certificate", false);
        boolean testMode = getBoolean(map, "test-mode", null, false);
        int downloadThreads = getInt(map, "download-threads", null, 32);
        int downloadThreshold = getInt(map, "download-threshold", null, 1048576);
        int concurrentFiles = getInt(map, "concurrent-files", null, 2);

        // 兜底：文件级并发度至少 1，上限 16。
        // 上限是因为最大同时在用的连接数 ≈ concurrent-files × download-threads（默认 2 × 32 = 64），
        // 配得太大（比如 16 × 32 = 512）会瞬间开几百条连接，把服务端和隧道都打爆
        if (concurrentFiles < 1) {
            concurrentFiles = 1;
        } else if (concurrentFiles > 16) {
            concurrentFiles = 16;
        }
        boolean detectModConflicts = getBoolean(map, "detect-mod-conflicts", null, true);
        boolean mirrorMode = getBoolean(map, "mirror-mode", null, false);
        boolean mirrorBackup = getBoolean(map, "mirror-backup", null, true);
        boolean autoSelectSource = getBoolean(map, "auto-select-source", null, true);
        String manualSource = getString(map, "manual-source", null, "");
        double sourceWeightSpeed = getDouble(map, "source-weight-speed", null, 0.8);
        double sourceWeightLatency = getDouble(map, "source-weight-latency", null, 0.2);
        int sourceCacheSeconds = getInt(map, "source-cache-seconds", null, 3600);
        int sourceProbeTimeout = getInt(map, "source-probe-timeout", null, 5000);
        String sourceSpeedtestFile = getString(map, "source-speedtest-file", null, "speedtest.bin");
        boolean multiSourceDownload = getBoolean(map, "multi-source-download", null, true);
        SourceMode sourceMode = readSourceMode(map);
        double speedtestMinSizeMb = getDouble(map, "speedtest-min-size-mb", null, 10);
        int speedtestDurationMs = getInt(map, "speedtest-duration-ms", null, 2000);
        double cdnPreferRatio = getDouble(map, "cdn-prefer-ratio", null, 0.5);
        int cdnMaxThreads = getInt(map, "cdn-max-threads", null, 64);
        List<String> cdnExclude = getList(map, "cdn-exclude", null, new ArrayList<>());
        int cdnTimeout = getInt(map, "cdn-timeout", null, 15000);

        // 兼容层：prefer-cdn 已经被 source-mode 取代，这里只保留它派生出来的布尔值
        boolean preferCdn = sourceMode != SourceMode.SERVER;

        // 测速用到的数值都夹到合理范围，配置写错时不至于把测速拖死或者把连接数打爆
        if (speedtestMinSizeMb < 0) {
            speedtestMinSizeMb = 0;
        }

        if (speedtestDurationMs < 200) {
            speedtestDurationMs = 200;
        } else if (speedtestDurationMs > 60000) {
            speedtestDurationMs = 60000;
        }

        if (cdnPreferRatio < 0) {
            cdnPreferRatio = 0;
        } else if (cdnPreferRatio > 100) {
            cdnPreferRatio = 100;
        }

        if (cdnMaxThreads < 1) {
            cdnMaxThreads = 1;
        } else if (cdnMaxThreads > 256) {
            cdnMaxThreads = 256;
        }

//        if (urls.contains("webda"))
//

        this.urls = urls;
        this.versionFilePath = versionFilePath;
        this.allowError = allowError;
        this.showNoUpdateMessage = showNoUpdateMessage;
        this.showHasUpdateMessage = showHasUpdateMessage;
        this.autoCloseChangelogs = autoCloseChangelogs;
        this.silentMode = silentMode;
        this.windowTitle = windowTitle;
        this.disableTheme = disableTheme;
        this.basePath = basePath;
        this.privateTimeout = privateTimeout;
        this.httpHeaders = httpHeaders;
        this.httpTimeout = httpTimeout;
        this.tcpTimeout = tcpTimeout;
        this.reties = reties;
        this.ignoreSSLCertificate = ignoreSSLCertificate;
        this.testMode = testMode;
        this.downloadThreads = downloadThreads;
        this.downloadThreshold = downloadThreshold;
        this.concurrentFiles = concurrentFiles;
        this.detectModConflicts = detectModConflicts;
        this.mirrorMode = mirrorMode;
        this.mirrorBackup = mirrorBackup;
        this.autoSelectSource = autoSelectSource;
        this.manualSource = manualSource;
        this.sourceWeightSpeed = sourceWeightSpeed;
        this.sourceWeightLatency = sourceWeightLatency;
        this.sourceCacheSeconds = sourceCacheSeconds;
        this.sourceProbeTimeout = sourceProbeTimeout;
        this.sourceSpeedtestFile = sourceSpeedtestFile;
        this.multiSourceDownload = multiSourceDownload;
        this.sourceMode = sourceMode;
        this.sourceModeSource = describeSourceMode(map, sourceMode);
        this.speedtestMinSizeMb = speedtestMinSizeMb;
        this.speedtestDurationMs = speedtestDurationMs;
        this.cdnPreferRatio = cdnPreferRatio;
        this.cdnMaxThreads = cdnMaxThreads;
        this.preferCdn = preferCdn;
        this.cdnExclude = cdnExclude;
        this.cdnTimeout = cdnTimeout;
    }

    /**
     * 测速样本的最小体积（字节），由 speedtest-min-size-mb 换算而来
     */
    public long speedtestMinBytes() {
        return Math.max(0L, (long) (speedtestMinSizeMb * 1024.0 * 1024.0));
    }

    /**
     * 读取下载来源模式<p>
     * 优先级：source-mode（auto / cdn / server，不区分大小写）&gt; 旧的 prefer-cdn（true → cdn，false → server）
     * &gt; 默认 auto<p>
     * 两个都写了的时候以 source-mode 为准，prefer-cdn 被完全忽略
     */
    static SourceMode readSourceMode(Map<String, Object> map) {
        Object value = map.get("source-mode");

        if (value != null) {
            if (!(value instanceof String)) {
                throw new RuntimeException("配置文件中找到 source-mode 配置项了，但是配置项的类型不匹配。"
                        + "预期是 auto / cdn / server 这样的文本，实际是 " + value.getClass().getSimpleName());
            }

            String text = ((String) value).trim().toLowerCase(Locale.ROOT);

            switch (text) {
                case "auto":
                    return SourceMode.AUTO;
                case "cdn":
                    return SourceMode.CDN;
                case "server":
                    return SourceMode.SERVER;
                default:
                    throw new RuntimeException("source-mode 只能是 auto、cdn、server 三者之一，实际写的是：" + value);
            }
        }

        // 兼容旧的 prefer-cdn：true → 强制 CDN，false → 强制服务端
        Object legacy = map.get("prefer-cdn");

        if (legacy != null) {
            if (!(legacy instanceof Boolean)) {
                throw new RuntimeException("配置文件中找到 prefer-cdn 配置项了，但是配置项的类型不匹配。"
                        + "预期 true / false，实际是 " + legacy.getClass().getSimpleName());
            }

            return ((Boolean) legacy) ? SourceMode.CDN : SourceMode.SERVER;
        }

        return SourceMode.AUTO;
    }

    /**
     * 拼一句「模式是从哪来的」描述，只用于日志，方便排查到底是 source-mode 生效还是 prefer-cdn 别名生效
     */
    static String describeSourceMode(Map<String, Object> map, SourceMode mode) {
        if (map.get("source-mode") != null) {
            return "source-mode";
        }

        if (map.get("prefer-cdn") != null) {
            return "prefer-cdn（旧写法，等价于 source-mode: " + mode.name().toLowerCase(Locale.ROOT) + "）";
        }

        return "默认值";
    }

    @SuppressWarnings("unchecked")
    static <T> T getOption(Map<String, Object> map, String key, String alterKey, T defaultValue, Class<T> clazz) {
        Object value = map.get(key);

        if (value == null) {
            value = map.get(alterKey);
        }

        if (value == null) {
            return defaultValue;
        }

        if (!clazz.isInstance(value)) {
            throw new RuntimeException("配置文件中找到 " + key + " 配置项了，但是配置项的类型不匹配。预期 " + clazz.getSimpleName() + " ，实际是 " + value.getClass().getSimpleName());
        }

        return clazz.isInstance(value) ? (T) value : null;
    }

    static String getString(Map<String, Object> map, String key, String formerKey, String defaultValue) {
        return getOption(map, key, formerKey, defaultValue, String.class);
    }

    static boolean getBoolean(Map<String, Object> map, String key, String formerKey, boolean defaultValue) {
        return getOption(map, key, formerKey, defaultValue, Boolean.class);
    }

    static int getInt(Map<String, Object> map, String key, String formerKey, int defaultValue) {
        return getOption(map, key, formerKey, defaultValue, Integer.class);
    }

    /**
     * 读取浮点型配置项，兼容 yaml 解析出来的 Integer 和 Double
     */
    static double getDouble(Map<String, Object> map, String key, String formerKey, double defaultValue) {
        Object value = map.get(key);

        if (value == null && formerKey != null) {
            value = map.get(formerKey);
        }

        if (value == null) {
            return defaultValue;
        }

        if (value instanceof Number) {
            return ((Number) value).doubleValue();
        }

        if (value instanceof String) {
            try {
                return Double.parseDouble(((String) value).trim());
            } catch (NumberFormatException e) {
                throw new RuntimeException("配置文件中找到 " + key + " 配置项了，但是内容不是合法的数字：" + value);
            }
        }

        throw new RuntimeException("配置文件中找到 " + key + " 配置项了，但是配置项的类型不匹配。预期数字，实际是 " + value.getClass().getSimpleName());
    }

    static List<String> getList(Map<String, Object> map, String key, String formerKey, List<String> defaultValue) {
        return getOption(map, key, formerKey, defaultValue, List.class);
    }

    static Map<String, String> getMap(Map<String, Object> map, String key, String formerKey, Map<String, String> defaultValue) {
        Map<String, String> result = getOption(map, key, formerKey, defaultValue, Map.class);

        return result != null ? result : new HashMap<>();
    }
}