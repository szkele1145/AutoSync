package com.github.balloonupdate.mcpatch.client;

import com.github.balloonupdate.mcpatch.client.config.AppConfig;
import com.github.balloonupdate.mcpatch.client.exceptions.McpatchBusinessException;
import com.github.balloonupdate.mcpatch.client.logging.ConsoleHandler;
import com.github.balloonupdate.mcpatch.client.logging.FileHandler;
import com.github.balloonupdate.mcpatch.client.logging.GuiLogHandler;
import com.github.balloonupdate.mcpatch.client.logging.Log;
import com.github.balloonupdate.mcpatch.client.logging.LogLevel;
import com.github.balloonupdate.mcpatch.client.ui.McPatchWindow;
import com.github.balloonupdate.mcpatch.client.utils.BytesUtils;
import com.github.balloonupdate.mcpatch.client.utils.DialogUtility;
import com.github.balloonupdate.mcpatch.client.utils.Env;
import com.github.kasuminova.GUI.SetupSwing;
import org.yaml.snakeyaml.Yaml;
import org.yaml.snakeyaml.parser.ParserException;

import java.awt.*;
import java.io.*;
import java.lang.instrument.Instrumentation;
import java.nio.channels.ClosedByInterruptException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.Map;
import java.util.jar.JarFile;
import java.util.zip.ZipEntry;

public class Main {
    /**
     * 程序的启动方式
     */
    public enum StartMethod {
        /**
         * 作为独立进程启动
         */
        Standalone,

        /**
         * 作为 java agent lib 启动
         */
        JavaAgent,

        /**
         * 由 modloader 启动
         */
        ModLoader,
    }

    public static void main(String[] args) throws Throwable  {
        boolean graphicsMode = Desktop.isDesktopSupported();

        if (args.length > 0 && args[0].equals("windowless"))
            graphicsMode = false;

        AppMain(graphicsMode, StartMethod.Standalone, true, false);
    }

    public static void premain(String agentArgs, Instrumentation ins) throws Throwable  {
        boolean graphicsMode = Desktop.isDesktopSupported();

        if (agentArgs != null && agentArgs.equals("windowless"))
            graphicsMode = false;

        try {
            AppMain(graphicsMode, StartMethod.JavaAgent, true, false);
        } catch (Throwable e) {
            // 能走到这里说明：
            // 1. 图形模式下用户在弹框里选择了停止启动
            // 2. 非图形模式下 allow-error 为 false（同步失败就不该让 Minecraft 起来）
            // 这里必须把异常继续抛出去，javaagent 的 premain 抛异常会让 JVM 启动失败、退出码非 0，
            // 启动器就能据此停住；allow-error 为 true 的情况在 AppMain 内就已经被吞掉并打日志了，不会走到这里
            Log.error("同步失败，已阻止启动 Minecraft：" + e.getMessage());
            throw e;
        }
    }

    public static boolean modloader(boolean enableLogFile, boolean disableTheme) throws Throwable {
        boolean graphicsMode = Desktop.isDesktopSupported();

        return AppMain(graphicsMode, StartMethod.ModLoader, enableLogFile, disableTheme);
    }

    /**
     * McPatchClient主逻辑
     * @param graphicsMode 是否以图形模式启动（桌面环境通常以图形模式启动，安卓环境通常不以图形模式启动）
     * @param startMethod 程序的启动方式。
     * @param enableLogFile 是否写入日志文件
     * @param disableTheme 是否强制禁用主题
     * @return 有木有文件更新
     */
    static boolean AppMain(boolean graphicsMode, StartMethod startMethod, boolean enableLogFile, boolean disableTheme) throws Throwable {
        // 记录有无更新
        boolean hasUpdate = false;

        McPatchWindow window = null;

        // catch 块里需要用它来判断 allow-error，所以提到 try 外面声明
        AppConfig config = null;

        try {
            // 初始化控制台日志系统
            InitConsoleLogging(graphicsMode, enableLogFile);

            // 准备各种目录
            Path progDir = getProgramDirectory();
            Path workDir = getWorkDirectory(progDir);
            config = new AppConfig(readConfig(progDir.resolve("mcpatch.yml")));
            Path baseDir = getUpdateDirectory(workDir, config);

            // 初始化文件日志系统
            String logFileName = graphicsMode ? "mcpatch.log" : "mcpatch.log.txt";
            Path logFilePath = progDir.resolve(logFileName);

            if (enableLogFile)
                 InitFileLogging(logFilePath);

            // 非独立进程启动时，使用标签标明日志所属模块
            if (startMethod == StartMethod.ModLoader || startMethod == StartMethod.JavaAgent)
                Log.setAppIdentifier(true);

            // 应用主题
            if (graphicsMode && !disableTheme && !config.disableTheme)
                SetupSwing.init();

            // 初始化UI
            window = graphicsMode ? new McPatchWindow() : null;

            // 初始化窗口
            if (window != null) {
                // 把日志实时推给窗口下半部的日志区。
                // windowless 模式下 window 是 null，永远不会走到这里，控制台与文件日志照旧
                Log.addHandler(new GuiLogHandler(window, LogLevel.Debug));

                window.setTitleText(config.windowTitle);
                window.setLabelText("正在连接到更新服务器");
                window.setLabelSecondaryText("");

                // 弹出窗口
                if (!config.silentMode)
                    window.show();
            }

            // 打印调试信息。放在窗口之后，这样「图形模式: true」这些启动信息在界面里也能看到
            PrintEnvironmentInfo(graphicsMode, startMethod, baseDir, workDir);

//            // 点击窗口的叉时停止更新任务
//            if (window != null) {
//                window.onWindowClosing = w -> {
//                    if (workThread.isAlive())
//                        workThread.interrupt();
//                };
//            }

            SyncEngine engine = new SyncEngine();
            engine.window = window;
            engine.config = config;
            engine.baseDir = baseDir;
            engine.progDir = progDir;
            engine.logFilePath = logFilePath;
            engine.graphicsMode = graphicsMode;
            engine.startMethod = startMethod;

            try {
                // 启动同步任务
                hasUpdate = engine.run();
            } catch (McpatchBusinessException e) {
                handleSyncFailure(e, graphicsMode, startMethod, config);
            }
        } finally {
            if (window != null)
                window.destroy();

            if (startMethod != Main.StartMethod.Standalone)
                Log.info("continue to start Minecraft!");

            // if (startMethod == StartMethod.Standalone)
            //     Runtime.getRuntime().exit(0);
        }

        return hasUpdate;
    }

    /**
     * 处理同步失败：决定是「继续启动 Minecraft」还是「抛异常阻止启动」<p>
     * 三种启动方式的语义：<p>
     * 1. 图形模式：弹框把选择权交给用户（保持原行为不变）<p>
     * 2. 非图形模式 + allow-error=true：只打日志，继续启动 Minecraft（保持原行为不变）<p>
     * 3. 非图形模式 + allow-error=false：必须抛异常。在 javaagent 的 premain 里抛异常会让 JVM 启动失败、
     *    退出码非 0，启动器才能据此判定同步失败并停下，而不是带着残缺的 mods 目录硬进游戏<p>
     *
     * @throws McpatchBusinessException 需要阻止 Minecraft 启动时原样抛出
     */
    static void handleSyncFailure(McpatchBusinessException e, boolean graphicsMode, StartMethod startMethod,
                                  AppConfig config) throws McpatchBusinessException {
        boolean a = e.getCause() instanceof InterruptedException;
        boolean b = e.getCause() instanceof ClosedByInterruptException;

        if (a || b) {
            Log.info("更新过程被用户打断！");
            return;
        }

        // 打印异常日志
        try {
            Log.openIndent("Crash");
            Log.error(e.toString());
            Log.closeIndent();
        } catch (Exception ex) {
            System.out.println("------------------------");
            System.out.println(ex);
        }

        if (graphicsMode) {
            boolean sp = startMethod == StartMethod.Standalone;

            String errMsg = e.getMessage() != null ? e.getMessage() : "<No Exception Message>";
            String errMessage = BytesUtils.stringBreak(errMsg, 80, "\n");
            String title = "发生错误 " + Env.getVersion();
            String content = errMessage + "\n";
            content += !sp ? "点击\"是\"显示错误详情并停止启动Minecraft，" : "点击\"是\"显示错误详情并退出，";
            content += !sp ? "点击\"否\"继续启动Minecraft" : "点击\"否\"直接退出程序";

            boolean choice = DialogUtility.confirm(title, content);

            if (!sp) {
                if (choice) {
                    DialogUtility.error("错误详情 " + Env.getVersion(), e.toString());

                    throw e;
                }
            } else {
                if (choice)
                    DialogUtility.error("错误详情 " + Env.getVersion(), e.toString());

                throw e;
            }

            return;
        }

        // 非图形模式（-javaagent:xxx=windowless）下没有弹框问用户的机会，只能看 allow-error
        if (config != null && !config.allowError) {
            Log.error("同步失败且 allow-error 为 false，已阻止启动 Minecraft（进程将以非 0 状态退出）");
            throw e;
        }

        Log.warn("同步失败，但 allow-error 为 true，将继续启动 Minecraft");
    }

    /**
     * 获取Jar文件所在的目录
     */
    static Path getProgramDirectory()
    {
        if (Env.isDevelopment()) {
            String devWorkDir = System.getenv("MCPATCH_DEV_WORK_DIR");
            String devProgDir = System.getenv("MCPATCH_DEV_PROG_DIR");

            // 优先用环境变量里的
            if (devWorkDir != null && devProgDir != null) {
                return Paths.get(devProgDir);
            }

            // 然后用test文件夹
            Path userDir = Paths.get(System.getProperty("user.dir"));
            return userDir.resolve("test");
        }

        return Env.getJarPath().getParent();
    }

    /**
     * 获取进程的工作目录
     */
    static Path getWorkDirectory(Path progDir) {
        Path userDir = Paths.get(System.getProperty("user.dir"));

        if (Env.isDevelopment()) {
            String devWorkDir = System.getenv("MCPATCH_DEV_WORK_DIR");
            String devProgDir = System.getenv("MCPATCH_DEV_PROG_DIR");

            Path workDir;

            // 优先用环境变量里的
            if (devWorkDir != null && devProgDir != null) {
                workDir = Paths.get(devWorkDir);
            } else {
                // 同程序文件夹
                workDir = progDir;
            }

            try {
                Files.createDirectories(workDir);
                return workDir;
            } catch (IOException e) {
                throw new RuntimeException(e);
            }
        }

        return userDir;
    }

    /**
     * 获取需要更新的起始目录
     * @param workDir 工作目录
     * @param config 配置信息
     * @return 更新起始目录
     * @throws McpatchBusinessException 当智能搜索搜不到.minecraft目录时
     */
    static Path getUpdateDirectory(Path workDir, AppConfig config) throws McpatchBusinessException {
        // 开发环境下直接返回工作目录
        if (Env.isDevelopment())
            return workDir;

        // 如果填写了base-path，就使用
        if (!config.basePath.equals("")) {
            return Env.getJarPath().getParent().resolve(config.basePath);
        }

        // 如果没有填写，就智能搜索
        Path result = searchDotMinecraft(workDir);

        // 必须找到才可以
        if (result == null) {
            String text = "找不到.minecraft目录。" +
                    "请将软件放到.minecraft目录的同级或者.minecraft目录下（最大7层深度）然后再次尝试运行。" +
                    "Windows系统下请不要使用右键的“打开方式”选择Java运行，而是要将Java设置成默认打开方式然后双击打开";
            throw new McpatchBusinessException(text);
        }

        return result;
    }

    /**
     * 向上搜索，直到有一个父目录包含 .minecraft 目录
     */
    static Path searchDotMinecraft(Path basedir) {
        try {
            File d = basedir.toFile();

            for (int i = 0; i < 7; i++) {
                for (File f : d.listFiles()) {
                    if (f.getName().equals(".minecraft")) {
                        return d.toPath();
                    }
                }

                d = d.getParentFile();
            }
        } catch (NullPointerException e) {
            return null;
        }

        return null;
    }

    // 从外部/内部读取配置文件并将内容返回
    static Map<String, Object> readConfig(Path external) throws McpatchBusinessException {
        try {
//            System.out.println("aaa " + external.toFile().getAbsolutePath());

            Map<String, Object> result;

            Yaml yaml = new Yaml();

            // 如果外部配置文件存在，优先使用
            if (Files.exists(external)) {
                result = yaml.load(new String(Files.readAllBytes(external)));
            }

            // 如果内部配置文件存在，则读取内部的
            else {
                // 开发时必须要有外部配置文件
                if (Env.isDevelopment()) {
                    throw new McpatchBusinessException("找不到配置文件: mcpatch.yml，开发时必须要有配置文件 " + external);
                }

                // 读取内部配置文件
                try (JarFile jar = new JarFile(Env.getJarPath().toFile())) {
                    ZipEntry entry = jar.getJarEntry("mcpatch.yml");

                    try (InputStream stream = jar.getInputStream(entry)) {
                        result = yaml.load(stream);
                    }
                }
            }

//            System.out.println(result);

            return result;
//
//            if (content.startsWith(":")) {
//                try {
//                    content = new String(Base64.getDecoder().decode(content.substring(1)));
//                } catch (IllegalArgumentException e) {
//                    throw new InvalidConfigFileException();
//                }
//            }
        } catch (ParserException | IOException e) {
            throw new McpatchBusinessException(e);
        }
    }

    /**
     * 初始化控制台日志系统
     */
    static void InitConsoleLogging(boolean graphicsMode, boolean enableLogFile) {
        LogLevel level;

        if (Env.isDevelopment()) {
            // 图形模式或者说禁用了日志文件，这时console就应该显示更详细的日志
            if (graphicsMode || !enableLogFile) {
                level = LogLevel.Debug;
            } else {
                level = LogLevel.Info;
            }
        } else {
            // 打包后也要显示详细一点的日志
            level = LogLevel.Debug;
        }

        Log.addHandler(new ConsoleHandler(level));
    }

    /**
     * 初始化文件日志系统
     */
    static void InitFileLogging(Path logFilePath) {
        Log.addHandler(new FileHandler(LogLevel.All, logFilePath));
    }

    /**
     * 收集并打印环境信息
     */
    static void PrintEnvironmentInfo(boolean graphicsMode, StartMethod startMethod, Path baseDir, Path workDir) {
        String jvmVersion = System.getProperty("java.version");
        String jvmVendor = System.getProperty("java.vendor");
        String osName = System.getProperty("os.name");
        String osArch = System.getProperty("os.arch");
        String osVersion = System.getProperty("os.version");

        Log.info("已用内存: " + BytesUtils.convertBytes(Runtime.getRuntime().totalMemory() - Runtime.getRuntime().freeMemory()));
        Log.info("图形模式: " + graphicsMode);
        Log.info("启动方式: " + startMethod);
        Log.info("基本目录: " + baseDir);
        Log.info("工作目录: " + workDir);
        Log.info("二进制文件目录: " + (Env.isDevelopment() ? "Dev" : Env.getJarPath()));
        Log.info("软件版本: " + Env.getVersion() + " (" + Env.getGitCommit() + ")");
        Log.info("虚拟机版本: " + jvmVendor + " (" + jvmVersion + ")");
        Log.info("操作系统: " + osName + ", " + osVersion + ", " + osArch);
    }
}
