package com.github.balloonupdate.mcpatch.client.ui;

import com.github.balloonupdate.mcpatch.client.logging.GuiLogHandler;
import com.github.balloonupdate.mcpatch.client.logging.Log;
import com.github.balloonupdate.mcpatch.client.logging.LogLevel;
import com.github.kasuminova.GUI.SetupSwing;

import javax.swing.*;
import javax.swing.border.Border;
import javax.swing.border.EmptyBorder;
import javax.swing.plaf.UIResource;
import javax.swing.text.AttributeSet;
import javax.swing.text.BadLocationException;
import javax.swing.text.Document;
import javax.swing.text.Element;
import javax.swing.text.StyleConstants;
import javax.swing.text.StyledDocument;
import java.awt.*;
import java.awt.event.InputEvent;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.Callable;
import java.util.concurrent.FutureTask;

/**
 * 「实时日志」窗口验收 harness（进程内）。
 *
 * 与主程序走完全一样的链路：Log -> GuiLogHandler -> SwingUtilities.invokeLater -> McPatchWindow 日志区。
 * 类被放在 com.github.balloonupdate.mcpatch.client.ui 包里，是为了直接读窗口的包内字段做断言，
 * 不需要为了测试在正式代码上开任何后门。
 *
 * 全部是进程内断言：不截图、不动鼠标键盘，跑的时候不打扰正在用这台机器的人。
 *
 * 用法：
 *   javac -encoding UTF-8 -cp build/libs/AutoSync-1.0.0.jar -d tests/gui/classes tests/gui/GuiLogHarness.java
 *   java -cp "build/libs/AutoSync-1.0.0.jar;tests/gui/classes" com.github.balloonupdate.mcpatch.client.ui.GuiLogHarness
 */
public class GuiLogHarness {
    // ------------------------------------------------------------------
    // 「改动前」实测基线：在改动前的 AutoSync-1.0.0.jar 上用同一套探针量出来的数值，
    // 用来证明日志区确实变大了、配色确实从 10 种变成 1 种
    // ------------------------------------------------------------------
    static final int BASELINE_CONTENT_HEIGHT = 442;
    static final int BASELINE_DIVIDER_LOCATION = 147;
    static final int BASELINE_LOG_HEIGHT = 287;
    static final double BASELINE_LOG_RATIO = BASELINE_LOG_HEIGHT * 1.0 / BASELINE_CONTENT_HEIGHT;
    static final int BASELINE_SCROLLBAR_WIDTH = 12;
    static final int BASELINE_FOREGROUND_COUNT = 10;
    static final String BASELINE_LOG_BACKGROUND = "#3C4150";
    static final String BASELINE_WINDOW_BACKGROUND = "#282C34";
    static final String BASELINE_SCROLL_BORDER = "com.formdev.flatlaf.ui.FlatBorder insets=3,3,3,3";

    static McPatchWindow win;
    static int passed = 0;
    static int failed = 0;

    /**
     * 正在派发用例自己合成的输入事件，这期间输入屏蔽要让路（否则合成事件会被吞掉）
     */
    static volatile boolean syntheticInput = false;

    /**
     * 自己写一份 UTF-8 报告文件：PowerShell 管道会把日志里的换行重新拼成 CRLF、还可能按宽度折行，
     * 直接看控制台输出容易被这些假象误导
     */
    static final List<String> report = new ArrayList<>();

    static final java.nio.file.Path reportPath = java.nio.file.Paths.get("tests", "gui", "harness-report.txt");

    public static void main(String[] args) throws Exception {
        SetupSwing.init();
        installInputShield();

        win = new McPatchWindow();
        Log.addHandler(new GuiLogHandler(win, LogLevel.Debug));
        win.setTitleText("AutoSync");
        win.setLabelText("正在连接到更新服务器");
        win.setLabelSecondaryText("demo.jar");
        moveOffScreen(win);
        win.show();

        testWindowOpened();
        testRealtimeAppend();
        testAutoScrollToBottom();
        testManualScrollUpNotYankedBack();
        testManualScrollUpByRealWheel();
        testSingleForegroundColor();
        testLogAreaLayout();
        testFusionIntoWindow();
        testFusionUnderFocus();
        testMultiLineAlignment();
        testLineLimit();
        testBurstPerformance();
        testResizeGrowsLogArea();
        testLightThemePalette();

        System.out.println();
        System.out.println("结果：通过 " + passed + " 项，失败 " + failed + " 项");
        report.add("");
        report.add("结果：通过 " + passed + " 项，失败 " + failed + " 项");

        java.nio.file.Files.write(reportPath, report, java.nio.charset.StandardCharsets.UTF_8);
        System.out.println("报告已写入 " + reportPath.toAbsolutePath());

        System.exit(failed == 0 ? 0 : 1);
    }

    // ------------------------------------------------------------------
    // 用例
    // ------------------------------------------------------------------

    static void testWindowOpened() throws Exception {
        section("1. 窗口能打开、结构正确");

        check(edt(() -> win.window.isShowing()), "窗口已显示");
        check("AutoSync".equals(edt(() -> win.window.getTitle())), "窗口标题是 AutoSync",
                "实际：" + edt(() -> win.window.getTitle()));
        check(edt(() -> win.window.isResizable()), "窗口可缩放");
        check(edt(() -> win.splitPane != null && win.splitPane.getOrientation() == JSplitPane.VERTICAL_SPLIT),
                "上下两块用 JSplitPane 垂直分隔");
        check(edt(() -> Math.abs(win.splitPane.getResizeWeight()) < 1e-9),
                "resizeWeight=0：窗口变高时多出来的高度给日志区");
        check(edt(() -> win.logScrollPane.getViewport().getView() == win.logPane), "日志区是 JTextPane + JScrollPane");
        check(edt(() -> !win.logPane.isEditable()), "日志区不可编辑");
        check(edt(() -> win.splitPane.getDividerLocation() > 0), "分隔条位置已初始化",
                "divider=" + edt(() -> win.splitPane.getDividerLocation()));
        check(edt(() -> win.splitPane.getDividerSize() > 0), "分隔条仍可拖动（dividerSize>0）",
                "dividerSize=" + edt(() -> win.splitPane.getDividerSize()));
        check(McPatchWindow.LOG_MAX_LINES == 2000, "日志上限是 2000 行");
        check(McPatchWindow.HEADER_HEIGHT <= 110 && McPatchWindow.HEADER_HEIGHT >= 96,
                "上半部状态区的目标高度落在 96~110px 区间", McPatchWindow.HEADER_HEIGHT + "px");
    }

    static void testRealtimeAppend() throws Exception {
        section("2. 日志实时追加");

        Log.info("实时日志测试：第一条");
        check(awaitText("实时日志测试：第一条"), "Log.info 之后窗口里出现了这一行");

        Log.info("实时日志测试：第二条");
        check(awaitText("实时日志测试：第二条"), "第二条也实时出现了");

        String renderedLine = firstLineWith(edt(() -> logText()), "实时日志测试：第一条");

        info("取到的行长度=" + renderedLine.length() + "，字符码=" + codes(renderedLine));

        check(renderedLine.matches("「\\d\\d:\\d\\d:\\d\\d\\.\\d\\d\\d \\[ INFO  \\] 实时日志测试：第一条」"),
                "行格式与控制台/文件日志一致（时间 + 等级 + 内容）", renderedLine);
        check(edt(() -> logText().matches("(?s).*\\d\\d:\\d\\d:\\d\\d\\.\\d\\d\\d \\[ INFO  \\] .*")),
                "每行有时间戳 [ HH:mm:ss.SSS ] 与等级标记 [ INFO  ]");
    }

    static void testAutoScrollToBottom() throws Exception {
        section("3. 自动滚到底部");

        for (int i = 0; i < 400; i++)
            Log.info("自动滚底填充行 " + i);

        check(awaitText("自动滚底填充行 399"), "400 行都渲染出来了");
        Thread.sleep(400);

        check(edt(() -> win.isLogAtBottom()), "滚动条停在底部");
        check(edt(GuiLogHarness::lastLineVisible), "最后一行在视口里可见（真的滚到底了）");
        check(edt(() -> logText().contains("自动滚底填充行 0")), "前面的行还在文档里");
    }

    static void testManualScrollUpNotYankedBack() throws Exception {
        section("4. 手动往上翻之后不再被强制拉回");

        // 直接把滚动条拉到最上面，等价于用户手动上翻（判定逻辑只看滚动条位置）
        edt(() -> win.logScrollPane.getVerticalScrollBar().setValue(0));
        Thread.sleep(120);

        int before = edt(() -> win.logScrollPane.getVerticalScrollBar().getValue());
        check(before == 0, "已经翻到最上面", "value=" + before);

        for (int i = 0; i < 60; i++)
            Log.info("上翻期间的新日志 " + i);

        check(awaitText("上翻期间的新日志 59"), "上翻期间的新日志照样实时渲染");
        Thread.sleep(500);

        int after = edt(() -> win.logScrollPane.getVerticalScrollBar().getValue());
        check(after == 0, "滚动条没有被拉到底部", "value=" + after + "（拉回会是 "
                + edt(() -> win.logScrollPane.getVerticalScrollBar().getMaximum()) + " 附近）");
        check(!edt(() -> win.isLogAtBottom()), "内部判定也确实认为用户不在底部");
        check(edt(GuiLogHarness::lastLineVisible) == false, "最后一行没有出现在视口里（内容是往上翻的旧日志）");

        // 再手动翻回底部，之后就应该恢复自动跟随
        edt(() -> {
            JScrollBar bar = win.logScrollPane.getVerticalScrollBar();
            bar.setValue(bar.getMaximum());
        });
        Thread.sleep(120);

        for (int i = 0; i < 30; i++)
            Log.info("翻回底部之后的新日志 " + i);

        check(awaitText("翻回底部之后的新日志 29"), "翻回底部后的日志已渲染");
        Thread.sleep(400);
        check(edt(() -> win.isLogAtBottom()), "用户翻回底部后，自动滚底恢复");
        check(edt(GuiLogHarness::lastLineVisible), "最后一行重新可见");
    }

    static void testManualScrollUpByRealWheel() throws Exception {
        section("5. 真实滚轮事件上翻");

        // 说明：这台机器上 Robot 的输入注入坐标被 DPI 缩放重映射了（想去 (701,504)，指针实际落在 (666,684)），
        // Robot.mouseWheel / 点击都打不到日志区，所以这里改成往 viewport 派发一个真正的 MouseWheelEvent——
        // 它走的是 JScrollPane 的滚轮处理链，和真实滚轮是同一条路径，比直接 setValue 更接近真实手势
        edt(() -> {
            JScrollBar bar = win.logScrollPane.getVerticalScrollBar();
            bar.setValue(bar.getMaximum());
        });
        Thread.sleep(200);

        final int bottomBefore = edt(() -> {
            JScrollBar bar = win.logScrollPane.getVerticalScrollBar();

            return bar.getMaximum() - bar.getVisibleAmount();
        });

        int before = edt(() -> win.logScrollPane.getVerticalScrollBar().getValue());

        check(before == bottomBefore, "滚轮之前视图贴在底部", "value=" + before + " 底部=" + bottomBefore);

        edt(() -> {
            syntheticInput = true;

            try {
                win.logPane.dispatchEvent(new java.awt.event.MouseWheelEvent(
                        win.logScrollPane.getViewport(), java.awt.event.MouseWheelEvent.MOUSE_WHEEL,
                        System.currentTimeMillis(), 0, 40, 40, 0, false,
                        java.awt.event.MouseWheelEvent.WHEEL_UNIT_SCROLL, 3, -8));
            } finally {
                syntheticInput = false;
            }
        });
        Thread.sleep(400);

        int afterWheel = edt(() -> win.logScrollPane.getVerticalScrollBar().getValue());

        check(afterWheel < bottomBefore - 3, "滚轮事件把视图往上翻走了",
                "value " + before + " -> " + afterWheel + "（底部 " + bottomBefore + "）");

        for (int i = 0; i < 40; i++)
            Log.info("滚轮上翻期间的新日志 " + i);

        check(awaitText("滚轮上翻期间的新日志 39"), "上翻期间的新日志照样实时渲染");
        Thread.sleep(500);

        int after = edt(() -> win.logScrollPane.getVerticalScrollBar().getValue());
        int bottomAfter = edt(() -> {
            JScrollBar bar = win.logScrollPane.getVerticalScrollBar();

            return bar.getMaximum() - bar.getVisibleAmount();
        });

        // 说明：这里允许一点点抖动（FlatLaf 的平滑滚动/EDT 调度在机器忙的时候会有几像素到几十像素的
        // 偏差，改动前也偶发过一次），真正要守住的是「没有被拉回底部」：
        // 真被拉回去的话 value 会直接跳到 bottom 附近，差着几千像素
        check(!edt(() -> win.isLogAtBottom()) && bottomAfter - after > 100,
                "滚轮上翻后，新日志没有把视图拉回底部",
                "before=" + afterWheel + " after=" + after + "（底部 " + bottomAfter + "，相距 "
                        + (bottomAfter - after) + "px）");
        check(Math.abs(after - afterWheel) <= 64, "上翻位置基本没动（允许 ±64px 抖动）",
                "value " + afterWheel + " -> " + after);

        // 收尾：回到跟随状态，免得影响后续用例
        edt(() -> {
            JScrollBar bar = win.logScrollPane.getVerticalScrollBar();
            bar.setValue(bar.getMaximum());
        });
        Thread.sleep(200);
    }

    /**
     * 要求 1：日志不要颜色。遍历 StyledDocument 的所有 Element（也就是每一个字符），
     * 确认前景色只有一种：时间戳、等级标记、[CDN]/[测速]/[服务端]/[镜像模式] 前缀、正文全部同色
     */
    static void testSingleForegroundColor() throws Exception {
        section("6. 日志只有一种前景色（无分级配色）");

        Log.debug("配色样例：debug 行");
        Log.info("配色样例：info 行");
        Log.warn("配色样例：warn 行");
        Log.error("配色样例：error 行");
        Log.info("[CDN] 配色样例：cdn 行");
        Log.warn("[CDN] 配色样例：cdn 警告行");
        Log.info("[测速] 配色样例：speedtest 行");
        Log.info("[服务端] 配色样例：server 行");
        Log.info("镜像模式：配色样例：mirror 行");
        Log.info("[镜像模式] 配色样例：mirror 方括号行");

        check(awaitText("配色样例：mirror 方括号行"), "配色样例日志已渲染");
        Thread.sleep(200);

        List<Color> colors = edt(GuiLogHarness::documentForegrounds);
        Color expected = (Color) edt(() -> win.styleLog.getAttribute(StyleConstants.Foreground));

        info("改动前同一批样例里出现过 " + BASELINE_FOREGROUND_COUNT + " 种前景色"
                + "（#5C6370 时间戳 / #7F8C98 DEBUG / #D8DEE9 INFO / #E5C07B WARN / #E06C75 ERROR"
                + " / #56B6C2 [CDN] / #C678DD [测速] / #98C379 [服务端] / #61AFEF [镜像模式] / #979FAD 结尾换行）");
        info("现在文档里所有 Element 的前景色 = " + hexList(colors));

        check(colors.size() == 1, "整个文档的前景色只有一种",
                "实际 " + colors.size() + " 种：" + hexList(colors));
        check(expected != null && colors.size() == 1 && expected.equals(colors.get(0)),
                "字符属性里的前景色与统一前景色一致", "期望 " + hex(expected) + "，实际 " + hexList(colors));
        check(edt(() -> win.logPane.getForeground()).equals(expected),
                "组件前景色也是同一种（DefaultStyledDocument 结尾的换行叶子靠它解析颜色）",
                hex(edt(() -> win.logPane.getForeground())));

        // 逐点抽查：时间戳 / 等级标记 / 关键前缀 / 正文
        String[][] spots = {
                {"配色样例：debug 行", "[ DEBUG ]", "DEBUG 等级标记"},
                {"配色样例：info 行", "[ INFO  ]", "INFO 等级标记"},
                {"配色样例：warn 行", "[ WARN  ]", "WARN 等级标记"},
                {"配色样例：error 行", "[ ERROR ]", "ERROR 等级标记"},
                {"[CDN] 配色样例：cdn 行", "[CDN]", "[CDN] 前缀"},
                {"[CDN] 配色样例：cdn 行", "配色样例", "[CDN] 行正文"},
                {"[CDN] 配色样例：cdn 警告行", "[ WARN  ]", "[CDN] 的 WARN 等级标记"},
                {"[测速] 配色样例：speedtest 行", "[测速]", "[测速] 前缀"},
                {"[服务端] 配色样例：server 行", "[服务端]", "[服务端] 前缀"},
                {"镜像模式：配色样例：mirror 行", "镜像模式", "镜像模式 前缀（无方括号）"},
                {"[镜像模式] 配色样例：mirror 方括号行", "[镜像模式]", "[镜像模式] 前缀"},
        };

        for (String[] spot : spots)
            checkSameColor(spot[0], spot[1], expected, spot[2] + " 与统一前景色相同");

        checkSameColor("配色样例：info 行", "配色样例", expected, "正文与前缀同色");
        checkLineStartColor("配色样例：info 行", expected, "时间戳与正文同色");
        checkLineStartColor("[CDN] 配色样例：cdn 行", expected, "[CDN] 行的时间戳同色");
    }

    /**
     * 要求 2：日志区更大。初始分隔条位置要压到 96~110px，日志区默认占窗口的大部分高度，
     * 并且上半部状态区里的标题/状态文字/进度条都不能被截断
     */
    static void testLogAreaLayout() throws Exception {
        section("7. 日志区默认占窗口的大部分高度");

        int content = edt(() -> win.window.getContentPane().getHeight());
        int divider = edt(() -> win.splitPane.getDividerLocation());
        int header = edt(() -> win.splitPane.getTopComponent().getHeight());
        int log = edt(() -> win.logScrollPane.getHeight());
        double ratio = log * 1.0 / content;
        double dividerRatio = divider * 1.0 / content;

        info(String.format("改动前：内容高度 %d，分隔条 %d（占 %.4f），日志区 %d（占 %.4f）",
                BASELINE_CONTENT_HEIGHT, BASELINE_DIVIDER_LOCATION,
                BASELINE_DIVIDER_LOCATION * 1.0 / BASELINE_CONTENT_HEIGHT, BASELINE_LOG_HEIGHT, BASELINE_LOG_RATIO));
        info(String.format("现在　：内容高度 %d，分隔条 %d（占 %.4f），日志区 %d（占 %.4f）",
                content, divider, dividerRatio, log, ratio));
        info(String.format("日志区占比提升 %.4f -> %.4f（+%.2f 个百分点）",
                BASELINE_LOG_RATIO, ratio, (ratio - BASELINE_LOG_RATIO) * 100));

        check(divider <= 110, "初始分隔条位置压到 110px 以内", divider + "px（改动前 " + BASELINE_DIVIDER_LOCATION + "px）");
        check(divider >= 96, "上半部状态区仍保留最小高度（>=96px）", divider + "px");
        check(header <= 110, "上半部状态区实际高度也在 110px 以内", header + "px");
        check(ratio > BASELINE_LOG_RATIO, "日志区占比比改动前更大",
                String.format("%.4f > %.4f", ratio, BASELINE_LOG_RATIO));
        check(ratio >= 0.70, "日志区默认占窗口大部分高度（>=70%）", String.format("%.2f%%", ratio * 100));

        check(edt(() -> McPatchWindow.initialDividerLocation(442)) == McPatchWindow.HEADER_HEIGHT,
                "442px 内容高度下初始分隔条就是 HEADER_HEIGHT",
                edt(() -> McPatchWindow.initialDividerLocation(442)) + "px");
        check(edt(() -> McPatchWindow.initialDividerLocation(2000)) == McPatchWindow.HEADER_HEIGHT,
                "窗口再高，状态区也不会跟着长高（多出来的都给日志区）",
                edt(() -> McPatchWindow.initialDividerLocation(2000)) + "px");
        check(edt(() -> McPatchWindow.initialDividerLocation(200)) == McPatchWindow.HEADER_MIN_HEIGHT,
                "窗口很矮时退回 HEADER_MIN_HEIGHT，状态区不被挤没",
                edt(() -> McPatchWindow.initialDividerLocation(200)) + "px");

        JPanel headerPanel = edt(() -> (JPanel) win.splitPane.getTopComponent());
        checkFullyInside(headerPanel, win.label, "标题文字");
        checkFullyInside(headerPanel, win.labelSecondary, "状态文字");
        checkFullyInside(headerPanel, win.progressBar, "进度条");
    }

    /**
     * 要求 3：嵌进去的融合感——去掉滚动条外侧边框与焦点描边、背景与窗口一致、滚动条细、留内边距
     */
    static void testFusionIntoWindow() throws Exception {
        section("8. 嵌进窗口：无边框、细滚动条、背景融合、内边距");

        // --- 滚动条外侧边框 ---
        Border scrollBorder = edt(() -> win.logScrollPane.getBorder());
        info("改动前 logScrollPane.getBorder() = " + BASELINE_SCROLL_BORDER + "（FlatLaf 的 FlatBorder）");
        check(scrollBorder == null || isEmptyBorder(scrollBorder), "JScrollPane 的 border 为空",
                describeBorder(scrollBorder));
        check(!(scrollBorder instanceof com.formdev.flatlaf.ui.FlatBorder),
                "不再是 FlatLaf 的 FlatBorder（焦点蓝色描边的来源）", describeBorder(scrollBorder));

        // --- 内边距 ---
        Border paneBorder = edt(() -> win.logPane.getBorder());
        Insets borderInsets = edt(() -> win.logPane.getBorder() == null ? null
                : win.logPane.getBorder().getBorderInsets(win.logPane));
        Insets paneInsets = edt(() -> win.logPane.getInsets());
        Insets margin = edt(() -> win.logPane.getMargin());

        check(paneBorder instanceof EmptyBorder, "JTextPane 的 border 是 EmptyBorder", describeBorder(paneBorder));
        check(borderInsets != null && borderInsets.top > 0 && borderInsets.left > 0
                        && borderInsets.bottom > 0 && borderInsets.right > 0,
                "内边距四边都 > 0（文字不贴边）", String.valueOf(borderInsets));
        check(borderInsets != null && borderInsets.left >= 8 && borderInsets.top >= 8,
                "内边距在 8~10px 量级", String.valueOf(borderInsets));
        check(paneInsets.top > 0 && paneInsets.left > 0 && paneInsets.bottom > 0 && paneInsets.right > 0,
                "JTextPane.getInsets() 确实反映出内边距（排版真的让开了）", String.valueOf(paneInsets));
        check(margin != null && margin.top == 0 && margin.left == 0 && margin.bottom == 0 && margin.right == 0,
                "margin 已清零，内外边距不会叠成两层", String.valueOf(margin));

        // --- 滚动条宽度 ---
        int barWidth = edt(() -> win.logScrollPane.getVerticalScrollBar().getPreferredSize().width);
        int uiBarWidth = UIManager.getInt("ScrollBar.width");

        info("改动前竖直滚动条首选宽度 = " + BASELINE_SCROLLBAR_WIDTH + "px（UIManager ScrollBar.width）");
        check(barWidth <= 10, "竖直滚动条首选宽度 <= 10px",
                barWidth + "px（改动前 " + BASELINE_SCROLLBAR_WIDTH + "px）");
        check(uiBarWidth <= 10, "UIManager 的 ScrollBar.width 也收窄了", uiBarWidth + "px");
        check(edt(() -> win.logScrollPane.getVerticalScrollBar().getUnitIncrement()) > 0,
                "滚动条 unitIncrement 保持为正（滚轮/键盘可用）",
                String.valueOf(edt(() -> win.logScrollPane.getVerticalScrollBar().getUnitIncrement())));

        // --- 背景融合 ---
        Color paneBg = edt(() -> win.logPane.getBackground());
        Color contentBg = edt(() -> win.window.getContentPane().getBackground());
        Color headerBg = edt(() -> win.splitPane.getTopComponent().getBackground());
        Color viewportBg = edt(() -> win.logScrollPane.getViewport().getBackground());
        Color foreground = (Color) edt(() -> win.styleLog.getAttribute(StyleConstants.Foreground));
        double ratio = McPatchWindow.contrastRatio(foreground, paneBg);

        info("改动前：日志区背景 " + BASELINE_LOG_BACKGROUND + "，窗口/上半部背景 " + BASELINE_WINDOW_BACKGROUND
                + "（明显两块）");
        info("现在　：日志区 " + hex(paneBg) + "，窗口内容 " + hex(contentBg)
                + "，上半部 " + hex(headerBg) + "，视口 " + hex(viewportBg));

        check(colorDistance(paneBg, contentBg) <= 6, "日志区背景与窗口内容背景一致/极接近",
                hex(paneBg) + " vs " + hex(contentBg) + "（最大通道差 " + colorDistance(paneBg, contentBg) + "）");
        check(colorDistance(paneBg, headerBg) <= 6, "与上半部状态区背景也是同一块底色",
                hex(paneBg) + " vs " + hex(headerBg));
        check(colorDistance(viewportBg, contentBg) <= 6, "视口背景同样融合",
                hex(viewportBg) + " vs " + hex(contentBg));
        check(!(paneBg instanceof UIResource), "背景是我们自己设的普通 Color，FlatLaf 不会再改回去",
                paneBg.getClass().getName());
        check(ratio >= 4.5, "统一前景色在融合后的背景上对比度 >= 4.5:1",
                String.format("%s on %s = %.2f:1", hex(foreground), hex(paneBg), ratio));
    }

    /**
     * 焦点描边：FlatLaf 的 ScrollPane.border（FlatBorder）会在日志区拿到焦点时画一圈蓝色描边。
     * 这里真的把焦点给日志区，再确认边框没有任何变化
     */
    static void testFusionUnderFocus() throws Exception {
        section("9. 日志区拿到焦点也不描边");

        info("FlatLaf 的 Component.focusWidth = " + UIManager.getInt("Component.focusWidth")
                + "（>0 时 FlatBorder 会画焦点描边，这正是改动前那圈蓝色的来源）");

        Border paneBefore = edt(() -> win.logPane.getBorder());
        Border scrollBefore = edt(() -> win.logScrollPane.getBorder());

        // 屏幕外 + 不可获得焦点是为了隔离真实输入；这一节专门验证焦点路径，
        // 临时把窗口搬回屏幕内并放开焦点（输入屏蔽照旧生效，用户真实滚轮依然进不来）
        edt(() -> {
            win.window.setFocusableWindowState(true);
            win.window.setLocationRelativeTo(null);
            win.window.toFront();
            win.logPane.requestFocusInWindow();
        });
        Thread.sleep(400);

        Border paneAfter = edt(() -> win.logPane.getBorder());
        Border scrollAfter = edt(() -> win.logScrollPane.getBorder());

        check(edt(() -> win.logPane.hasFocus()), "日志区确实拿到了焦点（真的触发了焦点路径）");
        check(scrollAfter == null, "拿到焦点后 JScrollPane 仍然没有边框（无蓝色描边）",
                describeBorder(scrollAfter));
        check(scrollBefore == null && scrollAfter == null, "焦点前后 JScrollPane 边框都是空",
                describeBorder(scrollBefore) + " -> " + describeBorder(scrollAfter));
        check(paneAfter instanceof EmptyBorder && paneAfter == paneBefore,
                "焦点前后 JTextPane 的 EmptyBorder 是同一个实例（没有被换成描边）",
                describeBorder(paneBefore) + " -> " + describeBorder(paneAfter));
        check(edt(() -> win.logScrollPane.getClientProperty("JComponent.outline")) == null
                        && edt(() -> win.logPane.getClientProperty("JComponent.outline")) == null,
                "FlatLaf 客户端属性 JComponent.outline 为空（不会触发错误/警告描边）");
        check(edt(() -> win.logScrollPane.getClientProperty("FlatLaf.styleClass")) == null
                        && edt(() -> win.logPane.getClientProperty("FlatLaf.styleClass")) == null,
                "没有额外的 FlatLaf styleClass 会给日志区加样式");

        // 收尾：搬回屏幕外，重新隔离真实输入
        edt(() -> {
            win.window.setFocusableWindowState(false);
            win.window.setLocation(-4000, -4000);
        });
    }

    static void testMultiLineAlignment() throws Exception {
        section("10. 多行日志对齐与前缀");

        Log.info("多行样例第一行\n多行样例第二行");
        check(awaitText("多行样例第二行"), "多行内容已渲染");

        String text = edt(() -> logText());
        String[] lines = text.split("\n");

        int firstColumn = -1;
        int secondColumn = -1;
        String secondLine = null;

        for (String line : lines) {
            if (line.contains("多行样例第一行"))
                firstColumn = line.indexOf("多行样例第一行");

            if (line.contains("多行样例第二行")) {
                secondColumn = line.indexOf("多行样例第二行");
                secondLine = line;
            }
        }

        check(firstColumn > 0 && secondColumn == firstColumn, "多行日志的第二行用等宽空白缩进，与第一行正文左对齐",
                "第一行正文列=" + firstColumn + "，第二行列=" + secondColumn);
        check(secondLine != null && !secondLine.contains("[ INFO  ]"), "第二行不再重复打印等级前缀",
                secondLine == null ? "（没找到）" : quote(secondLine.substring(0, Math.min(40, secondLine.length()))));

        // 带 indent 的日志（Log.openIndent 在 javaagent 模式下用得多）
        Log.openIndent("更新源选择");
        Log.info("缩进样例行");
        Log.closeIndent();

        check(awaitText("缩进样例行"), "带 indent 的日志已渲染");
        check(edt(() -> logText().contains("更新源选择 缩进样例行")), "indent 前缀原样显示",
                firstLineWith(edt(() -> logText()), "缩进样例行"));
    }

    static void testLineLimit() throws Exception {
        section("11. 限行数 2000");

        for (int i = 0; i < 5000; i++)
            Log.info("限行测试 " + i + " " + "x".repeat(60));

        check(awaitText("限行测试 4999"), "5000 行全部投递完毕");
        Thread.sleep(600);

        int tracked = edt(() -> win.logLineCount);
        int actual = edt(() -> countLines(win.logPane.getDocument()));
        String text = edt(() -> logText());

        check(tracked == 2000, "内部行数与上限一致", "logLineCount=" + tracked);
        check(actual == 2000, "文档里实际也是 2000 行", "actual=" + actual);
        check(text.contains("限行测试 4999"), "最新的日志还在");
        check(!text.contains("限行测试 0 "), "最旧的日志已经被丢掉");
        check(edt(() -> win.isLogAtBottom()), "丢弃旧行并继续追加之后，仍然贴在底部");
        check(edt(GuiLogHarness::lastLineVisible), "最后一行依然可见");

        List<Color> colors = edt(GuiLogHarness::documentForegrounds);
        check(colors.size() == 1, "限行裁剪之后前景色依然只有一种", hexList(colors));
    }

    static void testBurstPerformance() throws Exception {
        section("12. 突发日志不卡 UI");

        long maxPing;
        long deadline = System.currentTimeMillis() + 8000;

        // 测量 EDT 的响应延迟：一边猛灌日志，一边每 25ms 往 EDT 上排一个空任务，看它多久能跑完
        final long[] worst = {0};
        final boolean[] stop = {false};

        Thread sampler = new Thread(() -> {
            while (!stop[0] && System.currentTimeMillis() < deadline) {
                long begin = System.nanoTime();

                try {
                    SwingUtilities.invokeAndWait(() -> { });
                } catch (Exception e) {
                    return;
                }

                long cost = (System.nanoTime() - begin) / 1_000_000;

                if (cost > worst[0])
                    worst[0] = cost;

                try {
                    Thread.sleep(25);
                } catch (InterruptedException e) {
                    return;
                }
            }
        }, "edt-ping");
        sampler.setDaemon(true);
        sampler.start();

        long begin = System.currentTimeMillis();
        Thread producer = new Thread(() -> {
            for (int i = 0; i < 8000; i++)
                Log.info("性能测试 " + i + " " + "y".repeat(60));
        }, "log-producer");
        producer.start();
        producer.join();

        check(awaitText("性能测试 7999"), "8000 条日志全部渲染完成");
        stop[0] = true;
        sampler.join(2000);

        long elapsed = System.currentTimeMillis() - begin;
        maxPing = worst[0];

        info("8000 条日志渲染耗时 " + elapsed + "ms，EDT 最大响应延迟 " + maxPing + "ms");
        check(elapsed < 15000, "8000 条日志渲染没有慢到不可接受", elapsed + "ms");
        check(maxPing < 800, "灌日志期间 EDT 没有被长时间占住（不卡顿）", maxPing + "ms");
        check(edt(() -> win.logLineCount) == 2000, "限行数在突发场景下依然成立");
    }

    static void testResizeGrowsLogArea() throws Exception {
        section("13. 窗口缩放，日志区跟着变大");

        int beforeLog = edt(() -> win.logScrollPane.getHeight());
        int beforeHeader = edt(() -> win.splitPane.getTopComponent().getHeight());

        edt(() -> win.window.setSize(win.window.getWidth() + 200, win.window.getHeight() + 200));
        Thread.sleep(400);

        int afterLog = edt(() -> win.logScrollPane.getHeight());
        int afterHeader = edt(() -> win.splitPane.getTopComponent().getHeight());

        info("日志区高度 " + beforeLog + " -> " + afterLog + "，上半部高度 " + beforeHeader + " -> " + afterHeader);
        check(afterLog > beforeLog + 120, "日志区高度跟着窗口一起变大");
        check(Math.abs(afterHeader - beforeHeader) < 40, "上半部信息区高度基本不变（多出来的高度都给了日志区）");
        check(edt(() -> win.isLogAtBottom()), "缩放之后仍然贴在底部");
    }

    static void testLightThemePalette() throws Exception {
        section("14. 浅色主题下也能看清（disable-theme 场景）");

        try {
            UIManager.setLookAndFeel(new javax.swing.plaf.metal.MetalLookAndFeel());
        } catch (Exception e) {
            info("无法切到 Metal LAF，跳过：" + e);
            return;
        }

        McPatchWindow light = new McPatchWindow(400, 300);
        moveOffScreen(light);
        light.show();
        Thread.sleep(200);

        Color background = edt(() -> light.logPane.getBackground());
        Color foreground = edt(() -> light.logPane.getForeground());
        Color contentBackground = edt(() -> light.window.getContentPane().getBackground());
        Color styleColor = (Color) edt(() -> light.styleLog.getAttribute(StyleConstants.Foreground));

        info("浅色主题下日志区背景 " + hex(background) + "，前景色 " + hex(foreground)
                + "，窗口内容背景 " + hex(contentBackground));
        check(McPatchWindow.luminance(background) > 0.5, "日志区背景确实是浅色", hex(background));
        check(McPatchWindow.luminance(foreground) < 0.5, "统一前景色自动换成深色，不会白底白字", hex(foreground));
        check(McPatchWindow.contrastRatio(foreground, background) >= 4.5,
                "浅色主题下对比度仍然 >= 4.5:1",
                String.format("%.2f:1", McPatchWindow.contrastRatio(foreground, background)));
        check(foreground.equals(styleColor), "组件前景色与字符属性仍然同色", hex(styleColor));
        check(colorDistance(background, contentBackground) <= 6, "浅色主题下日志区背景同样与窗口融合",
                hex(background) + " vs " + hex(contentBackground));

        light.destroy();
    }

    // ------------------------------------------------------------------
    // 工具
    // ------------------------------------------------------------------

    /**
     * 把验收窗口挪到屏幕外。<p>
     * 跑用例时用户可能正在这台机器上玩游戏，窗口默认在屏幕正中，鼠标滚轮很容易落到日志区上，
     * 把「视图位置」这类断言直接搅乱（实测被真实滚轮搅乱过一次）。验收与真实输入无关，必须隔离
     */
    static void moveOffScreen(McPatchWindow target) throws Exception {
        edt(() -> {
            target.window.setLocation(-4000, -4000);
            target.window.setFocusableWindowState(false);
        });
    }

    /**
     * 吞掉落到本进程的滚轮/按键事件（只影响这个测试 JVM，动不到用户别的程序）<p>
     * 用例自己合成的滚轮事件（第 5 节）会临时把 {@link #syntheticInput} 打开，不受影响
     */
    static void installInputShield() {
        Toolkit.getDefaultToolkit().addAWTEventListener(event -> {
            if (!syntheticInput && event instanceof InputEvent)
                ((InputEvent) event).consume();
        }, AWTEvent.MOUSE_WHEEL_EVENT_MASK | AWTEvent.KEY_EVENT_MASK);
    }

    static void section(String title) {
        System.out.println();
        System.out.println("== " + title);
        report.add("");
        report.add("== " + title);
    }

    static void check(boolean ok, String label) {
        check(ok, label, "");
    }

    static void check(boolean ok, String label, String detail) {
        String line = "  [" + (ok ? "PASS" : "FAIL") + "] " + label + (detail.isEmpty() ? "" : "  ->  " + detail);

        System.out.println(line);
        report.add(line);

        if (ok)
            passed += 1;
        else
            failed += 1;
    }

    static void info(String message) {
        System.out.println("  [info] " + message);
        report.add("  [info] " + message);
    }

    // ---------- 文档颜色 ----------

    /**
     * 遍历 StyledDocument 的所有 Element，收集所有「有文字的叶子」的前景色。
     * 叶子覆盖文档里的每一个字符，所以这个集合就是「文档里出现过的全部前景色」
     */
    static List<Color> documentForegrounds() throws Exception {
        StyledDocument doc = win.logPane.getStyledDocument();
        List<Color> found = new ArrayList<>();

        collectForegrounds(doc.getDefaultRootElement(), doc, found);

        return found;
    }

    static void collectForegrounds(Element element, StyledDocument doc, List<Color> out) {
        if (element.isLeaf()) {
            try {
                String text = doc.getText(element.getStartOffset(), element.getEndOffset() - element.getStartOffset());

                if (!text.isEmpty()) {
                    Color color = StyleConstants.getForeground(element.getAttributes());

                    if (!out.contains(color))
                        out.add(color);
                }
            } catch (BadLocationException ignored) { }
        }

        for (int i = 0; i < element.getElementCount(); i++)
            collectForegrounds(element.getElement(i), doc, out);
    }

    static void checkSameColor(String needle, String at, Color expected, String label) throws Exception {
        Color got = foregroundInLine(needle, at);

        check(expected != null && expected.equals(got), label,
                "期望 " + hex(expected) + " 实际 " + hex(got) + "（" + needle + " 里的 " + at + "）");
    }

    /**
     * 取「needle 所在行里 at 第一次出现的位置」的字符颜色
     */
    static Color foregroundInLine(String needle, String at) throws Exception {
        return edt(() -> {
            StyledDocument doc = win.logPane.getStyledDocument();
            String text = doc.getText(0, doc.getLength());
            int index = text.lastIndexOf(needle);

            if (index < 0)
                return null;

            int lineStart = text.lastIndexOf('\n', index) + 1;
            int lineEnd = text.indexOf('\n', index);

            if (lineEnd < 0)
                lineEnd = text.length();

            int offset = text.indexOf(at, lineStart);

            if (offset < 0 || offset >= lineEnd)
                offset = index;

            return StyleConstants.getForeground(doc.getCharacterElement(offset).getAttributes());
        });
    }

    /**
     * 取「needle 所在行行首」的字符颜色（用来验证时间戳那一列）
     */
    static Color lineStartColor(String needle) throws Exception {
        return edt(() -> {
            StyledDocument doc = win.logPane.getStyledDocument();
            String text = doc.getText(0, doc.getLength());
            int index = text.lastIndexOf(needle);

            if (index < 0)
                return null;

            int lineStart = text.lastIndexOf('\n', index) + 1;

            return StyleConstants.getForeground(doc.getCharacterElement(lineStart).getAttributes());
        });
    }

    static void checkLineStartColor(String needle, Color expected, String label) throws Exception {
        Color got = lineStartColor(needle);

        check(expected != null && expected.equals(got), label,
                "期望 " + hex(expected) + " 实际 " + hex(got));
    }

    // ---------- 布局 ----------

    /**
     * 组件在容器里必须完整可见：宽高都 > 0，四边都不越界（也就是没有被截断）
     */
    static void checkFullyInside(Container container, Component child, String label) throws Exception {
        Rectangle bounds = edt(() -> SwingUtilities.convertRectangle(
                child.getParent(), child.getBounds(), container));

        boolean ok = bounds.width > 0 && bounds.height > 0
                && bounds.x >= 0 && bounds.y >= 0
                && bounds.x + bounds.width <= container.getWidth()
                && bounds.y + bounds.height <= container.getHeight();

        check(ok, label + "在状态区里完整可见（没有被截断）",
                bounds + "，状态区 " + container.getWidth() + "x" + container.getHeight());
    }

    static boolean isEmptyBorder(Border border) {
        if (border == null)
            return true;

        if (border instanceof EmptyBorder) {
            Insets insets = border.getBorderInsets(new JLabel());

            return insets.top == 0 && insets.left == 0 && insets.bottom == 0 && insets.right == 0;
        }

        return false;
    }

    static String describeBorder(Border border) {
        if (border == null)
            return "null";

        String insets = "";

        try {
            insets = " insets=" + border.getBorderInsets(new JLabel());
        } catch (Throwable ignored) { }

        return border.getClass().getName() + insets;
    }

    /**
     * 两个颜色之间最大的单通道差值，用来判断「一致或极接近」
     */
    static int colorDistance(Color a, Color b) {
        if (a == null || b == null)
            return 255;

        return Math.max(Math.abs(a.getRed() - b.getRed()),
                Math.max(Math.abs(a.getGreen() - b.getGreen()), Math.abs(a.getBlue() - b.getBlue())));
    }

    static boolean lastLineVisible() {
        try {
            Document doc = win.logPane.getDocument();
            int offset = Math.max(0, doc.getLength() - 1);
            Rectangle rect = win.logPane.modelToView(offset);

            if (rect == null)
                return false;

            Point point = SwingUtilities.convertPoint(win.logPane, rect.getLocation(), win.logScrollPane.getViewport());

            return point.y >= 0 && point.y < win.logScrollPane.getViewport().getHeight();
        } catch (Exception e) {
            return false;
        }
    }

    static int countLines(Document document) throws Exception {
        String text = document.getText(0, document.getLength());
        int lines = 1;

        for (int i = 0; i < text.length(); i++) {
            if (text.charAt(i) == '\n')
                lines += 1;
        }

        return lines;
    }

    /**
     * 读日志区的文本。<p>
     * 注意不能用 {@code logPane.getText()}：JTextPane 会把它换成平台换行（Windows 下变成 CRLF），
     * 而文档里其实只有 LF，直接拿来做断言会被那个多出来的 \r 坑到（已经踩过一次）
     */
    static String logText() throws Exception {
        return edt(() -> {
            Document document = win.logPane.getDocument();

            return document.getText(0, document.getLength());
        });
    }

    static boolean awaitText(String needle) throws Exception {
        long deadline = System.currentTimeMillis() + 10000;

        while (System.currentTimeMillis() < deadline) {
            if (edt(() -> logText().contains(needle)))
                return true;

            Thread.sleep(20);
        }

        return false;
    }

    static String firstLineWith(String text, String needle) {
        for (String line : text.split("\n")) {
            if (line.contains(needle))
                return quote(line);
        }

        return "（没找到）";
    }

    static String quote(String value) {
        return "「" + value + "」";
    }

    /**
     * 把字符串的每个字符码点打出来，专门用来排查「看不见的换行/回车」这类问题
     */
    static String codes(String value) {
        StringBuilder builder = new StringBuilder();

        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);

            if (c < 32 || c == 127)
                builder.append("<").append((int) c).append(">");
            else
                builder.append(c);
        }

        return builder.toString();
    }

    static String hex(Color color) {
        return color == null ? "null" : String.format("#%08X", color.getRGB());
    }

    static String hexList(List<Color> colors) {
        StringBuilder builder = new StringBuilder("[");

        for (int i = 0; i < colors.size(); i++) {
            if (i > 0)
                builder.append(", ");

            builder.append(hex(colors.get(i)));
        }

        return builder.append("]").toString();
    }

    static <T> T edt(Callable<T> callable) throws Exception {
        if (SwingUtilities.isEventDispatchThread())
            return callable.call();

        FutureTask<T> task = new FutureTask<>(callable);
        SwingUtilities.invokeAndWait(task);

        return task.get();
    }

    static void edt(Runnable runnable) throws Exception {
        edt(() -> {
            runnable.run();
            return null;
        });
    }
}
