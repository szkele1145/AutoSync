package com.github.balloonupdate.mcpatch.client.ui;

import com.github.balloonupdate.mcpatch.client.logging.GuiLogHandler;
import com.github.balloonupdate.mcpatch.client.logging.Log;
import com.github.balloonupdate.mcpatch.client.logging.LogLevel;
import com.github.balloonupdate.mcpatch.client.logging.Message;
import com.github.kasuminova.GUI.SetupSwing;

import javax.swing.*;
import javax.swing.border.Border;
import javax.swing.plaf.UIResource;
import javax.swing.text.BadLocationException;
import javax.swing.text.Document;
import javax.swing.text.Element;
import javax.swing.text.StyleConstants;
import javax.swing.text.StyledDocument;
import java.awt.*;
import java.awt.event.InputEvent;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.Callable;
import java.util.concurrent.FutureTask;

/**
 * 「日志区自动滚到最新一行」验收 harness（进程内断言，不动鼠标键盘）。<p>
 * 与主程序走完全一样的链路：Log -&gt; GuiLogHandler -&gt; SwingUtilities.invokeLater -&gt; McPatchWindow 日志区。<p>
 * 类放在 ui 包里是为了直接读窗口的包内字段/方法做断言，不需要在正式代码上开后门。<p>
 * 用法：
 * <pre>
 * javac -encoding UTF-8 -cp build/libs/AutoSync-1.0.0.jar -d tests/gui/classes tests/gui/LogAutoScrollHarness.java
 * java -cp "build/libs/AutoSync-1.0.0.jar;tests/gui/classes" com.github.balloonupdate.mcpatch.client.ui.LogAutoScrollHarness
 * </pre>
 */
public class LogAutoScrollHarness {
    static int passed = 0;
    static int failed = 0;

    static final List<String> report = new ArrayList<>();
    static final Path reportPath = Paths.get("tests", "gui", "autoscroll-report.txt");

    /**
     * 「贴底」的像素容差，与正式代码里的判定保持一致
     */
    static final int TOLERANCE = McPatchWindow.LOG_BOTTOM_TOLERANCE;

    /**
     * 每次追加后的检查里，允许等待的毫秒数（EDT 忙时延迟补偿可能晚一点跑）
     */
    static final long SETTLE_TIMEOUT_MS = 3000;

    /**
     * 「验收窗口在屏幕外」这条信息只在第一次报告里打一次
     */
    static boolean reportedLocation = false;

    public static void main(String[] args) throws Exception {
        SetupSwing.init();
        installInputShield();

        caseContinuousAppend();
        caseFirstAppendBeforeLayout();
        caseLogsBeforeShow();
        caseAfterDividerDrag();
        caseAfterResize();
        caseFocusAndSelection();
        caseBatchVersusOneByOne();
        caseManualScrollUpNotYankedBack();
        caseManualScrollUpThenLayoutChange();
        caseStyleAndTrimRegression();

        System.out.println();
        System.out.println("结果：通过 " + passed + " 项，失败 " + failed + " 项");
        report.add("");
        report.add("结果：通过 " + passed + " 项，失败 " + failed + " 项");

        Files.write(reportPath, report, StandardCharsets.UTF_8);
        System.out.println("报告已写入 " + reportPath.toAbsolutePath());

        Log.stop();
        System.exit(failed == 0 ? 0 : 1);
    }

    // ------------------------------------------------------------------
    // 用例
    // ------------------------------------------------------------------

    /**
     * 要求：窗口 show() 之后连续追加日志（每 20ms 一条，共 100 条），
     * 每一次追加后都检查「滚动条在底部 + 最后一行真的在视口里」
     */
    static void caseContinuousAppend() throws Exception {
        section("1. show() 之后连续追加 100 条（每 20ms 一条），逐条断言");

        McPatchWindow win = openWindow(660, 480, true);

        int worstGap = 0;
        int notBottom = 0;
        int notVisible = 0;
        int firstBadIndex = -1;
        String firstBadDetail = "";

        for (int i = 0; i < 100; i++) {
            Thread.sleep(20);
            Log.info("连续追加 R" + i);
            awaitText(win, "连续追加 R" + i);

            Snapshot s = snapshot(win);

            if (s.gap > worstGap)
                worstGap = s.gap;

            if (s.gap > TOLERANCE)
                notBottom += 1;

            if (!s.lastLineVisible)
                notVisible += 1;

            if ((s.gap > TOLERANCE || !s.lastLineVisible) && firstBadIndex < 0) {
                firstBadIndex = i;
                firstBadDetail = s.describe();
            }
        }

        check(notBottom == 0, "100 次追加后，每一次滚动条都在底部（差值 <= " + TOLERANCE + "px）",
                notBottom == 0 ? "最大差值 " + worstGap + "px" : "有 " + notBottom + " 次没贴底，第一次是第 "
                        + firstBadIndex + " 条：" + firstBadDetail);
        check(notVisible == 0, "100 次追加后，每一次最后一行都真的在视口内可见",
                notVisible == 0 ? "全部可见" : "有 " + notVisible + " 次不可见，第一次是第 " + firstBadIndex + " 条："
                        + firstBadDetail);
        check(edt(() -> bar(win).getValue() + bar(win).getVisibleAmount() == bar(win).getMaximum()),
                "最后停下来时 value + visible 恰好等于 maximum",
                edt(() -> bar(win).getValue() + " + " + bar(win).getVisibleAmount() + " == " + bar(win).getMaximum()));
        check(followState(win), "内部状态仍然是「跟随最新一行」");

        closeWindow(win);
    }

    /**
     * 边界：第一次追加。窗口还没显示、视口没布局完，此时 maximum 可能还是 0
     */
    static void caseFirstAppendBeforeLayout() throws Exception {
        section("2. 第一次追加（视口尚未布局完成，maximum 还是 0）");

        McPatchWindow win = new McPatchWindow(660, 480);
        Log.stop();
        Log.addHandler(new GuiLogHandler(win, LogLevel.Debug));
        moveOffScreen(win);

        Log.info("第一条日志：窗口还没显示");
        awaitText(win, "第一条日志：窗口还没显示");

        int max = edt(() -> bar(win).getMaximum());
        int viewportHeight = edt(() -> win.logScrollPane.getViewport().getHeight());
        int visible = edt(() -> bar(win).getVisibleAmount());

        info("追加第一条时：maximum=" + max + "，visible=" + visible + "，视口高=" + viewportHeight
                + "（视口没布局完时 visible/视口高 就是 0）");
        check(followState(win), "视口还没布局完的时候，判定依然认为处于跟随状态",
                "maximum=" + max + " visible=" + visible);

        // 从非 EDT 线程 show()：主程序 Main.java 就是这么调的，必须也能贴底
        win.show();
        awaitSettled(win);

        Snapshot after = snapshot(win);
        check(after.gap <= TOLERANCE, "窗口显示之后视图落在底部", after.describe());
        check(after.lastLineVisible, "第一条日志在视口里可见", after.describe());

        // 显示完成之后再追加几条，仍然要跟着走
        for (int i = 0; i < 5; i++) {
            Log.info("第一条之后 F" + i);
            awaitText(win, "第一条之后 F" + i);
        }

        awaitSettled(win);
        Snapshot later = snapshot(win);
        check(later.gap <= TOLERANCE && later.lastLineVisible, "窗口显示后再追加 5 条依然贴底且末行可见",
                later.describe());

        closeWindow(win);
    }

    /**
     * 边界：窗口 show() 之前就产生的日志（静默模式启动，弹出时要能回溯启动日志，并且停在最新一行）
     */
    static void caseLogsBeforeShow() throws Exception {
        section("3. show() 之前的启动日志（弹出后既能看到启动日志，也停在最新一行）");

        McPatchWindow win = new McPatchWindow(660, 480);
        Log.stop();
        Log.addHandler(new GuiLogHandler(win, LogLevel.Debug));
        moveOffScreen(win);

        for (int i = 0; i < 80; i++)
            Log.info("启动日志 C" + i);

        awaitText(win, "启动日志 C79");
        awaitSettled(win);

        int maxBefore = edt(() -> bar(win).getMaximum());
        info("show() 之前：maximum=" + maxBefore + "，视口高=" + edt(() -> win.logScrollPane.getViewport().getHeight()));

        win.show();
        awaitSettled(win);

        Snapshot s = snapshot(win);
        check(s.gap <= TOLERANCE, "show() 之后视图直接落在底部（不再停在第一行）", s.describe());
        check(s.lastLineVisible, "show() 之后最后一行（启动日志 C79）在视口里可见", s.describe());
        check(edt(() -> bar(win).getValue()) > 0, "内容超出一屏时确实滚下去了（value > 0）",
                "value=" + edt(() -> bar(win).getValue()));
        check(edt(() -> docText(win).contains("启动日志 C0")), "最早那条启动日志还在文档里，可以往上翻回溯");

        Log.info("show 之后的第一条 C80");
        awaitText(win, "show 之后的第一条 C80");
        awaitSettled(win);
        Snapshot after = snapshot(win);
        check(after.gap <= TOLERANCE && after.lastLineVisible, "show() 之后的新日志照样贴底跟随", after.describe());

        closeWindow(win);
    }

    /**
     * 边界：JSplitPane 分隔条被拖动之后（视口高度变了）
     */
    static void caseAfterDividerDrag() throws Exception {
        section("4. 拖动 JSplitPane 分隔条之后");

        McPatchWindow win = openWindow(660, 480, true);
        fill(win, 60, "拖动前填充");
        awaitSettled(win);

        int beforeHeight = edt(() -> win.logScrollPane.getHeight());

        // 往「日志区变矮」的方向拖：这正是会把自动跟随搞死的方向
        edt(() -> win.splitPane.setDividerLocation(260));
        awaitSettled(win);

        int afterHeight = edt(() -> win.logScrollPane.getHeight());

        Snapshot dragged = snapshot(win);
        check(afterHeight < beforeHeight, "分隔条确实把日志区拖矮了",
                beforeHeight + "px -> " + afterHeight + "px");
        check(dragged.gap <= TOLERANCE, "拖动之后视图立刻仍然贴在底部（不需要等下一条日志）", dragged.describe());
        check(dragged.lastLineVisible, "拖动之后最后一行仍然可见", dragged.describe());

        int bad = appendAndAssertEach(win, 20, "分隔条之后 D");
        check(bad == 0, "拖动分隔条之后再追加 20 条，每一条都贴底且末行可见",
                bad == 0 ? "20/20 通过" : bad + " 次失败");

        // 反向：把日志区拖高，也不能掉队
        edt(() -> win.splitPane.setDividerLocation(120));
        awaitSettled(win);

        Snapshot taller = snapshot(win);
        check(taller.gap <= TOLERANCE && taller.lastLineVisible, "把日志区拖高之后同样贴底且末行可见",
                taller.describe());

        closeWindow(win);
    }

    /**
     * 边界：窗口 resize（变大 / 变小两个方向）
     */
    static void caseAfterResize() throws Exception {
        section("5. 窗口 resize 之后");

        McPatchWindow win = openWindow(660, 480, true);
        fill(win, 60, "resize 前填充");
        awaitSettled(win);

        // 变矮：日志区跟着变矮，最容易把跟随搞死
        edt(() -> win.window.setSize(760, 420));
        awaitSettled(win);

        Snapshot smaller = snapshot(win);
        info("变小之后：日志区高=" + edt(() -> win.logScrollPane.getHeight()));
        check(smaller.gap <= TOLERANCE && smaller.lastLineVisible, "窗口变矮之后仍然贴底、末行可见",
                smaller.describe());

        int bad = appendAndAssertEach(win, 20, "resize 之后 E");
        check(bad == 0, "resize 之后再追加 20 条，每一条都贴底且末行可见",
                bad == 0 ? "20/20 通过" : bad + " 次失败");

        // 变大：value 会被夹到新的底部，也不能掉队
        edt(() -> win.window.setSize(900, 760));
        awaitSettled(win);

        Snapshot bigger = snapshot(win);
        info("变大之后：日志区高=" + edt(() -> win.logScrollPane.getHeight()));
        check(bigger.gap <= TOLERANCE && bigger.lastLineVisible, "窗口变大之后仍然贴底、末行可见",
                bigger.describe());

        int bad2 = appendAndAssertEach(win, 10, "放大之后 E2");
        check(bad2 == 0, "放大之后再追加 10 条依然跟随", bad2 == 0 ? "10/10 通过" : bad2 + " 次失败");

        closeWindow(win);
    }

    /**
     * 边界：日志区获得焦点 / 有文本选择时
     */
    static void caseFocusAndSelection() throws Exception {
        section("6. 日志区获得焦点、有文本选择时");

        McPatchWindow win = openWindow(660, 480, true);
        fill(win, 60, "焦点前填充");
        awaitSettled(win);

        edt(() -> {
            // 这一节专门走「焦点 + 选择」这条路径：临时把窗口恢复成可获得焦点，走完再收回去
            win.window.setFocusableWindowState(true);
            win.logPane.requestFocusInWindow();
            // 选中开头一段文字：真的建立 selection，而不是只移动插入符
            win.logPane.setCaretPosition(0);
            win.logPane.moveCaretPosition(60);
        });
        awaitSettled(win);

        info("hasFocus=" + edt(() -> win.logPane.hasFocus())
                + "，selectionStart=" + edt(() -> win.logPane.getSelectionStart())
                + "~" + edt(() -> win.logPane.getSelectionEnd()));

        Snapshot selected = snapshot(win);
        check(edt(() -> win.logPane.getSelectionStart() == 0 && win.logPane.getSelectionEnd() == 60),
                "确实建立了文本选择", edt(() -> win.logPane.getSelectionStart() + "~" + win.logPane.getSelectionEnd()));
        check(selected.gap <= TOLERANCE && selected.lastLineVisible, "有选择、有焦点时视图依然贴在底部",
                selected.describe());

        int bad = appendAndAssertEach(win, 20, "选择期间 F");
        check(bad == 0, "有选择、有焦点时追加 20 条，每一条都贴底且末行可见",
                bad == 0 ? "20/20 通过" : bad + " 次失败");

        closeWindow(win);
    }

    /**
     * 要求：单批多条（400 条）vs 逐条
     */
    static void caseBatchVersusOneByOne() throws Exception {
        section("7. 单批 400 条 vs 逐条");

        // --- 单批 400 条，而且窗口刚打开就灌 ---
        McPatchWindow batch = openWindow(660, 480, true);
        List<Message> messages = new ArrayList<>();

        for (int i = 0; i < 400; i++)
            messages.add(message("批量 400 行 B" + i));

        edt(() -> batch.appendLogMessages(new ArrayList<>(messages)));
        awaitText(batch, "批量 400 行 B399");
        awaitSettled(batch);

        Snapshot batchSnapshot = snapshot(batch);
        check(batchSnapshot.gap <= TOLERANCE, "窗口刚打开就灌一批 400 条，视图落在底部", batchSnapshot.describe());
        check(batchSnapshot.lastLineVisible, "一批 400 条之后最后一行可见", batchSnapshot.describe());
        check(edt(() -> batch.logPane.getDocument().getLength()) > 0, "400 条确实渲染进文档了");
        closeWindow(batch);

        // --- 逐条 400 条 ---
        McPatchWindow single = openWindow(660, 480, true);
        int bad = appendAndAssertEach(single, 400, "逐条 400 R");
        check(bad == 0, "逐条追加 400 条，每一条都贴底且末行可见",
                bad == 0 ? "400/400 通过" : bad + " 次失败");
        check(followState(single), "逐条追加之后仍然处于跟随状态");
        closeWindow(single);
    }

    /**
     * 回归：用户手动上翻时，新日志不得把视图拉回底部；用户翻回底部后自动跟随要恢复
     */
    static void caseManualScrollUpNotYankedBack() throws Exception {
        section("8. 回归：手动上翻不被拉回，翻回底部恢复跟随");

        McPatchWindow win = openWindow(660, 480, true);
        fill(win, 120, "上翻前填充");
        awaitSettled(win);

        // 直接 setValue(0) == 用户一路翻到最上面（真实滚轮路径另见 GuiLogHarness 第 5 节）
        edt(() -> bar(win).setValue(0));
        awaitSettled(win);

        check(!edt(() -> win.isLogAtBottom()), "已经翻到最上面（内部判定也认为不在底部）");
        check(!followState(win), "内部跟随状态已经关掉");

        int before = edt(() -> bar(win).getValue());
        int bad = 0;
        String firstBad = "";

        for (int i = 0; i < 60; i++) {
            Log.info("上翻期间的新日志 U" + i);
            awaitText(win, "上翻期间的新日志 U" + i);
            awaitSettled(win);

            int value = edt(() -> bar(win).getValue());

            if (Math.abs(value - before) > TOLERANCE) {
                bad += 1;

                if (firstBad.isEmpty())
                    firstBad = "第 " + i + " 条之后 value " + before + " -> " + value;
            }
        }

        check(bad == 0, "上翻期间来了 60 条新日志，视图一直停在原处，没有被拉回底部",
                bad == 0 ? "value 始终保持 " + before : firstBad);
        check(!edt(() -> win.isLogAtBottom()), "上翻期间也一直没有被拉到底部");
        check(!lastLineVisible(win), "上翻期间最后一行不在视口里（看的是旧日志）");

        // 用户自己翻回底部 -> 跟随恢复
        edt(() -> {
            JScrollBar bar = bar(win);
            bar.setValue(bar.getMaximum());
        });
        awaitSettled(win);

        check(followState(win), "用户翻回底部后，跟随状态恢复");

        int badAfter = appendAndAssertEach(win, 30, "翻回底部之后 U");
        check(badAfter == 0, "翻回底部后追加 30 条，每一条都重新贴底且末行可见",
                badAfter == 0 ? "30/30 通过" : badAfter + " 次失败");

        closeWindow(win);
    }

    /**
     * 回归：上翻状态下拖分隔条 / 缩放窗口，同样不得把视图拉到底
     */
    static void caseManualScrollUpThenLayoutChange() throws Exception {
        section("9. 回归：上翻状态下拖动分隔条 / 缩放窗口不被拉回");

        McPatchWindow win = openWindow(660, 480, true);
        fill(win, 120, "上翻前填充 L");
        awaitSettled(win);

        edt(() -> bar(win).setValue(600));
        awaitSettled(win);

        int before = edt(() -> bar(win).getValue());
        check(!edt(() -> win.isLogAtBottom()), "已经手动翻到中间", "value=" + before);

        edt(() -> win.splitPane.setDividerLocation(300));
        awaitSettled(win);

        int afterDivider = edt(() -> bar(win).getValue());
        check(Math.abs(afterDivider - before) <= TOLERANCE, "拖分隔条没有把视图拉到底部",
                "value " + before + " -> " + afterDivider + "（底部是 "
                        + edt(() -> bar(win).getMaximum() - bar(win).getVisibleAmount()) + "）");

        edt(() -> win.window.setSize(900, 700));
        awaitSettled(win);

        int afterResize = edt(() -> bar(win).getValue());
        check(Math.abs(afterResize - before) <= TOLERANCE, "缩放窗口没有把视图拉到底部",
                "value " + before + " -> " + afterResize + "（底部是 "
                        + edt(() -> bar(win).getMaximum() - bar(win).getVisibleAmount()) + "）");

        for (int i = 0; i < 20; i++) {
            Log.info("上翻期间布局变化 L" + i);
            awaitText(win, "上翻期间布局变化 L" + i);
        }

        awaitSettled(win);

        int finalValue = edt(() -> bar(win).getValue());
        check(Math.abs(finalValue - before) <= TOLERANCE, "上翻 + 布局变化之后，新日志仍然没有把视图拉回底部",
                "value=" + finalValue + "（底部是 "
                        + edt(() -> bar(win).getMaximum() - bar(win).getVisibleAmount()) + "）");

        closeWindow(win);
    }

    /**
     * 回归：2000 行裁剪、单色、无边框、8px 滚动条等样式不变
     */
    static void caseStyleAndTrimRegression() throws Exception {
        section("10. 回归：限行裁剪与既有样式不变");

        McPatchWindow win = openWindow(660, 480, true);

        for (int i = 0; i < 2600; i++)
            Log.info("限行回归 T" + i);

        awaitText(win, "限行回归 T2599");
        awaitSettled(win);

        int tracked = edt(() -> win.logLineCount);
        int actual = edt(() -> countLines(win));
        String text = edt(() -> docText(win));

        check(tracked == 2000 && actual == 2000, "裁剪之后仍然是 2000 行",
                "logLineCount=" + tracked + "，文档实际=" + actual);
        check(text.contains("限行回归 T2599") && !text.contains("限行回归 T0 "), "保留最新的、丢掉最旧的");
        check(edt(() -> win.isLogAtBottom()), "裁剪 + 追加之后仍然贴在底部");
        check(lastLineVisible(win), "裁剪之后最后一行依然可见");

        List<Color> colors = edt(() -> documentForegrounds(win));
        Color styleColor = (Color) edt(() -> win.styleLog.getAttribute(StyleConstants.Foreground));
        check(colors.size() == 1 && colors.get(0).equals(styleColor), "前景色依然只有一种",
                hexList(colors) + " vs " + hex(styleColor));
        check(edt(() -> win.logPane.getForeground()).equals(styleColor), "组件前景色与字符属性一致");

        Border scrollBorder = edt(() -> win.logScrollPane.getBorder());
        check(scrollBorder == null, "JScrollPane 仍然没有边框（没有焦点描边）",
                scrollBorder == null ? "null" : scrollBorder.getClass().getName());
        check(!(edt(() -> win.logPane.getBackground()) instanceof UIResource),
                "日志区背景仍然是我们自己设的普通 Color");

        int barWidth = edt(() -> bar(win).getPreferredSize().width);
        check(barWidth == McPatchWindow.LOG_SCROLLBAR_WIDTH, "竖直滚动条仍然是 " + McPatchWindow.LOG_SCROLLBAR_WIDTH + "px",
                barWidth + "px");
        check(McPatchWindow.LOG_MAX_LINES == 2000, "日志上限仍然是 2000 行");
        check(!edt(() -> win.logPane.isEditable()), "日志区仍然不可编辑");
        check(edt(() -> win.logScrollPane.getVerticalScrollBarPolicy())
                        == ScrollPaneConstants.VERTICAL_SCROLLBAR_AS_NEEDED,
                "竖直滚动条策略没变（AS_NEEDED）");

        closeWindow(win);
    }

    // ------------------------------------------------------------------
    // 断言与取样
    // ------------------------------------------------------------------

    /**
     * 一次快照：滚动条数值 + 最后一行是否在视口里，全部在 EDT 上同一时刻取
     */
    static class Snapshot {
        int value;
        int visible;
        int maximum;
        int gap;
        boolean lastLineVisible;
        int lastLineY;
        int viewportHeight;
        int viewHeight;

        String describe() {
            return "value=" + value + " visible=" + visible + " max=" + maximum + " 距底=" + gap
                    + " 末行可见=" + lastLineVisible + " 末行y=" + lastLineY + " 视口高=" + viewportHeight
                    + " 视图高=" + viewHeight;
        }
    }

    static Snapshot snapshot(McPatchWindow win) throws Exception {
        return edt(() -> {
            JScrollBar bar = bar(win);
            Snapshot s = new Snapshot();

            s.value = bar.getValue();
            s.visible = bar.getVisibleAmount();
            s.maximum = bar.getMaximum();
            s.gap = s.maximum - s.visible - s.value;
            s.lastLineVisible = lastLineVisible(win);
            s.lastLineY = lastLineY(win);
            s.viewportHeight = win.logScrollPane.getViewport().getHeight();
            s.viewHeight = win.logScrollPane.getViewport().getViewSize().height;

            return s;
        });
    }

    /**
     * 逐条追加 n 条（每 20ms 一条），每次追加后都断言贴底 + 末行可见；
     * 返回失败的次数
     */
    static int appendAndAssertEach(McPatchWindow win, int count, String prefix) throws Exception {
        int bad = 0;
        String firstBad = "";

        for (int i = 0; i < count; i++) {
            Thread.sleep(20);
            Log.info(prefix + i);
            awaitText(win, prefix + i);

            Snapshot s = snapshot(win);

            if (s.gap > TOLERANCE || !s.lastLineVisible) {
                bad += 1;

                if (firstBad.isEmpty())
                    firstBad = "第 " + i + " 条：" + s.describe();
            }
        }

        if (bad > 0)
            info(prefix + "：失败 " + bad + "/" + count + "，第一次是 " + firstBad);

        return bad;
    }

    /**
     * 灌 n 条日志（不逐条断言，用来铺垫内容）
     */
    static void fill(McPatchWindow win, int count, String prefix) throws Exception {
        for (int i = 0; i < count; i++)
            Log.info(prefix + i);

        awaitText(win, prefix + (count - 1));
    }

    /**
     * 等这条日志真的进了文档。<p>
     * 注意这里用 invokeAndWait 读文档：它会排在 GuiLogHandler 的渲染任务、以及渲染任务里排下的
     * 「延迟补偿滚动」之后，所以「等到文本出现」就等于「延迟补偿也已经跑完」，不需要 sleep 猜时间
     */
    static void awaitText(McPatchWindow win, String needle) throws Exception {
        long deadline = System.currentTimeMillis() + SETTLE_TIMEOUT_MS;

        while (System.currentTimeMillis() < deadline) {
            if (edt(() -> docText(win).contains(needle)))
                return;

            Thread.sleep(5);
        }

        throw new IllegalStateException("等不到日志进入文档：" + needle);
    }

    /**
     * 等到滚动条不再变化（布局/补偿都跑完）
     */
    static void awaitSettled(McPatchWindow win) throws Exception {
        long deadline = System.currentTimeMillis() + SETTLE_TIMEOUT_MS;
        int lastValue = Integer.MIN_VALUE;
        int lastMax = Integer.MIN_VALUE;
        int stable = 0;

        while (System.currentTimeMillis() < deadline) {
            int[] state = edt(() -> new int[]{bar(win).getValue(), bar(win).getMaximum(), bar(win).getVisibleAmount()});

            if (state[0] == lastValue && state[1] == lastMax) {
                stable += 1;

                if (stable >= 3)
                    return;
            } else {
                stable = 0;
                lastValue = state[0];
                lastMax = state[1];
            }

            Thread.sleep(15);
        }
    }

    // ------------------------------------------------------------------
    // 窗口与工具
    // ------------------------------------------------------------------

    static McPatchWindow openWindow(int width, int height, boolean viaLog) throws Exception {
        McPatchWindow win = new McPatchWindow(width, height);
        Log.stop();

        if (viaLog)
            Log.addHandler(new GuiLogHandler(win, LogLevel.Debug));

        win.setTitleText("AutoSync");
        win.setLabelText("自动滚动验收");
        moveOffScreen(win);
        win.show();
        awaitSettled(win);

        return win;
    }

    /**
     * 把验收窗口挪到屏幕外。<p>
     * 跑用例时用户可能正在这台机器上玩游戏，窗口默认在屏幕正中，鼠标滚轮很容易落到日志区上——
     * 实测被真实滚轮搅乱过（视图从 0 被滚到 48px，正好是 3 格滚轮）。验收只关心程序自己的行为，
     * 真实输入必须隔离掉
     */
    static void moveOffScreen(McPatchWindow win) throws Exception {
        edt(() -> {
            win.window.setLocation(-4000, -4000);
            win.window.setFocusableWindowState(false);
        });

        if (!reportedLocation) {
            reportedLocation = true;
            info("验收窗口已挪到屏幕外并设为不可获得焦点：" + edt(() -> win.window.getLocation()));
        }
    }

    /**
     * 吞掉落到本进程的滚轮/按键事件（只影响这个测试 JVM，动不到用户别的程序）
     */
    static void installInputShield() {
        Toolkit.getDefaultToolkit().addAWTEventListener(event -> {
            if (event instanceof InputEvent)
                ((InputEvent) event).consume();
        }, AWTEvent.MOUSE_WHEEL_EVENT_MASK | AWTEvent.KEY_EVENT_MASK);
    }

    static void closeWindow(McPatchWindow win) throws Exception {
        Log.stop();
        awaitSettled(win);
        edt(win::destroy);
        Thread.sleep(80);
    }

    static JScrollBar bar(McPatchWindow win) {
        return win.logScrollPane.getVerticalScrollBar();
    }

    /**
     * 读「是否处于自动跟随状态」。<p>
     * 用反射读，是为了让这份用例在**修复前**的 jar 上也能跑：那时候还没有 shouldFollowLogTail，
     * 就退化成当时唯一可用（也正是有问题）的判定 isLogAtBottom()。
     * 这样「同一份用例在修复前失败、修复后全绿」才是有说服力的复现证据
     */
    static boolean followState(McPatchWindow win) throws Exception {
        java.lang.reflect.Method method;

        try {
            method = McPatchWindow.class.getDeclaredMethod("shouldFollowLogTail");
            method.setAccessible(true);
        } catch (NoSuchMethodException e) {
            return edt(() -> win.isLogAtBottom());
        }

        final java.lang.reflect.Method target = method;

        return edt(() -> {
            try {
                return (Boolean) target.invoke(win);
            } catch (Exception e) {
                throw new RuntimeException(e);
            }
        });
    }

    static Message message(String content) {
        Message message = new Message();

        message.time = System.currentTimeMillis();
        message.level = LogLevel.Info;
        message.content = content;
        message.indents = new ArrayList<>();

        return message;
    }

    static boolean lastLineVisible(McPatchWindow win) {
        return lastLineY(win) >= 0 && lastLineY(win) < win.logScrollPane.getViewport().getHeight();
    }

    static int lastLineY(McPatchWindow win) {
        try {
            Document document = win.logPane.getDocument();
            Rectangle rect = win.logPane.modelToView(Math.max(0, document.getLength() - 1));

            if (rect == null)
                return Integer.MIN_VALUE;

            Point point = SwingUtilities.convertPoint(win.logPane, rect.getLocation(),
                    win.logScrollPane.getViewport());

            return point.y;
        } catch (Exception e) {
            return Integer.MIN_VALUE;
        }
    }

    static String docText(McPatchWindow win) throws Exception {
        Document document = win.logPane.getDocument();

        return document.getText(0, document.getLength());
    }

    static int countLines(McPatchWindow win) throws Exception {
        String text = docText(win);
        int lines = 1;

        for (int i = 0; i < text.length(); i++) {
            if (text.charAt(i) == '\n')
                lines += 1;
        }

        return lines;
    }

    static List<Color> documentForegrounds(McPatchWindow win) throws Exception {
        StyledDocument document = win.logPane.getStyledDocument();
        List<Color> found = new ArrayList<>();

        collectForegrounds(document.getDefaultRootElement(), document, found);

        return found;
    }

    static void collectForegrounds(Element element, StyledDocument document, List<Color> out) {
        if (element.isLeaf()) {
            try {
                String text = document.getText(element.getStartOffset(),
                        element.getEndOffset() - element.getStartOffset());

                if (!text.isEmpty()) {
                    Color color = StyleConstants.getForeground(element.getAttributes());

                    if (!out.contains(color))
                        out.add(color);
                }
            } catch (BadLocationException ignored) { }
        }

        for (int i = 0; i < element.getElementCount(); i++)
            collectForegrounds(element.getElement(i), document, out);
    }

    // ------------------------------------------------------------------
    // 报告
    // ------------------------------------------------------------------

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
