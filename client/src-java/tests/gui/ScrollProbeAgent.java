package com.github.balloonupdate.mcpatch.client.ui;

import javax.swing.*;
import javax.swing.text.JTextComponent;
import java.awt.*;
import java.awt.event.InputEvent;
import java.lang.instrument.Instrumentation;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;

/**
 * 端到端验收用的探针 agent：在**真实图形模式同步**的进程里，每隔 100ms 采一次日志区的滚动状态，
 * 写到一个文本文件里，供 {@code tests/verify_gui_live.py} 断言「日志一直自动滚到最新一行」。<p>
 *
 * 全部是进程内采样，不截屏、不动鼠标键盘：<p>
 * - 采样：从 {@code Window.getWindows()} 里认出「内容区里有 JTextComponent + JScrollPane」的那个窗口，
 *   读滚动条的 value/visible/maximum。只依赖 Swing/AWT，不引用被测代码，同步流程怎么改都不会把探针搞挂<p>
 * - 隔离：顺手吞掉落到这个进程的滚轮/按键事件。跑验收时用户可能正在这台机器上玩游戏，
 *   真实滚轮落到日志区会把「视图位置」搅乱（进程内 harness 上实测发生过），
 *   而验收只关心程序自己的行为<p>
 *
 * 两个必须踩过的坑（都实测过）：<p>
 * 1. agent 参数**只能是纯 ASCII 的文件名**。Windows 上 -javaagent 的参数按平台编码解码，
 *    把 {@code C:\Users\一只屑\...} 这种带着中文的完整路径塞进去，到 premain 时已经被损坏成不存在的路径
 *    （现象：写文件一直 NoSuchFileException）。目录由 JVM 自己从 {@code java.io.tmpdir} 取，
 *    那个值不经过命令行解码，中文目录不会出问题<p>
 * 2. 采样**绝不能往 EDT 投任务**（invokeLater / invokeAndWait）。投了任务，AWT 事件队列就一直被吊着：
 *    客户端同步完、窗口都销毁了，JVM 仍然不退出（现象：e2e 里客户端进程 120 秒都不结束）。
 *    这里改成在采样线程上直接读{@link JScrollBar} 的值——{@code DefaultBoundedRangeModel} 的读方法是
 *    synchronized 的，跨线程读是安全的；窗口销毁后采样线程收工，JVM 照常退出<p>
 *
 * 用法（由 verify_gui_live.py 自动构建并挂上，不用手工调）：
 * <pre>
 * java -javaagent:scroll-probe-agent.jar=&lt;纯 ASCII 的输出文件名&gt; -jar AutoSync-1.0.0.jar
 * </pre>
 */
public class ScrollProbeAgent {
    /**
     * 采样间隔（毫秒）。同步本身只跑一两秒，密一点才采得到足够多的样本
     */
    static final long SAMPLE_INTERVAL_MS = 50;

    /**
     * 最多采样多久，防止 agent 把进程拖住（正常同步几秒就结束了）
     */
    static final long MAX_DURATION_MS = 10 * 60 * 1000L;

    /**
     * 一直没等到窗口就放弃的时限：别把无关的进程拖着不退出
     */
    static final long WINDOW_WAIT_MS = 60 * 1000L;

    /**
     * 认定「贴底」的像素容差
     */
    static final int TOLERANCE = 2;

    static final List<String> samples = new ArrayList<>();

    /**
     * 有没有采到过窗口（用来判断「同步结束、窗口已销毁」）
     */
    static volatile boolean sawWindow = false;

    public static void premain(String args, Instrumentation instrumentation) {
        final Path output = probePath(args);

        installInputShield();

        Thread thread = new Thread(() -> loop(output), "autoscroll-probe");

        thread.setDaemon(true);
        thread.start();
    }

    /**
     * 解析输出路径：只接受纯 ASCII 的文件名（相对路径一律落到 {@code java.io.tmpdir}）
     */
    static Path probePath(String args) {
        String value = args == null || args.isBlank() ? "autosync-scroll-probe.txt" : args.trim();
        Path path = Paths.get(value);

        if (path.isAbsolute())
            return path;

        return Paths.get(System.getProperty("java.io.tmpdir", ".")).resolve(value);
    }

    /**
     * 吞掉落到本进程的滚轮/按键事件（只影响被验收的这个 JVM，动不到用户别的程序）
     */
    static void installInputShield() {
        try {
            Toolkit.getDefaultToolkit().addAWTEventListener(event -> {
                if (event instanceof InputEvent)
                    ((InputEvent) event).consume();
            }, AWTEvent.MOUSE_WHEEL_EVENT_MASK | AWTEvent.KEY_EVENT_MASK);
        } catch (Throwable e) {
            System.err.println("[autosync-probe] 安装输入屏蔽失败（不影响验收）：" + e);
        }
    }

    static void loop(Path output) {
        long begin = System.currentTimeMillis();
        long deadline = begin + MAX_DURATION_MS;

        while (System.currentTimeMillis() < deadline) {
            boolean sampled = sampleOnce();

            if (sampled)
                sawWindow = true;

            flush(output);

            // 窗口没了 = 同步结束，收工走人（采样线程是 daemon，不会拖住 JVM）
            if (!sampled && sawWindow)
                break;

            if (!sampled && System.currentTimeMillis() - begin > WINDOW_WAIT_MS)
                break;

            try {
                Thread.sleep(SAMPLE_INTERVAL_MS);
            } catch (InterruptedException e) {
                return;
            }
        }

        flush(output);
    }

    static void flush(Path output) {
        String text;

        synchronized (samples) {
            text = String.join("\n", samples) + "\n";
        }

        try {
            Files.write(output, text.getBytes(StandardCharsets.UTF_8));
        } catch (Throwable e) {
            System.err.println("[autosync-probe] 写采样文件失败 " + output + " -> " + e);
        }
    }

    /**
     * 采一次样（在采样线程上直接读，不碰 EDT）。找到日志区返回 true
     */
    static boolean sampleOnce() {
        for (Window window : Window.getWindows()) {
            if (!window.isShowing() || !(window instanceof Frame))
                continue;

            JScrollPane logScrollPane = findLogScrollPane(window);

            if (logScrollPane == null)
                continue;

            JScrollBar bar = logScrollPane.getVerticalScrollBar();

            // DefaultBoundedRangeModel 的读方法是 synchronized 的，跨线程读安全；
            // 三个值不是一次原子快照，极端情况下会差几像素，所以下面的判定留了容差
            int value = bar.getValue();
            int visible = bar.getVisibleAmount();
            int maximum = bar.getMaximum();
            int documentLength = documentLength(logScrollPane);
            int gap = maximum - visible - value;
            boolean atBottom = gap <= TOLERANCE;

            String line = String.format(
                    "doc=%d value=%d visible=%d max=%d gap=%d at_bottom=%s",
                    documentLength, value, visible, maximum, gap, atBottom);

            synchronized (samples) {
                samples.add(line);
            }

            return true;
        }

        return false;
    }

    /**
     * 找日志区：内容区里那个「视图是 JTextComponent 的 JScrollPane」
     */
    static JScrollPane findLogScrollPane(Container container) {
        for (Component component : container.getComponents()) {
            if (component instanceof JScrollPane) {
                JScrollPane scrollPane = (JScrollPane) component;

                if (scrollPane.getViewport().getView() instanceof JTextComponent)
                    return scrollPane;
            }

            if (component instanceof Container) {
                JScrollPane found = findLogScrollPane((Container) component);

                if (found != null)
                    return found;
            }
        }

        return null;
    }

    static int documentLength(JScrollPane scrollPane) {
        Component view = scrollPane.getViewport().getView();

        if (view instanceof JTextComponent)
            return ((JTextComponent) view).getDocument().getLength();

        return -1;
    }
}
