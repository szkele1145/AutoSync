package com.github.balloonupdate.mcpatch.client.ui;

import com.github.balloonupdate.mcpatch.client.logging.GuiLogHandler;
import com.github.balloonupdate.mcpatch.client.logging.Log;
import com.github.balloonupdate.mcpatch.client.logging.LogLevel;
import com.github.balloonupdate.mcpatch.client.logging.Message;
import com.github.kasuminova.GUI.SetupSwing;

import javax.swing.*;
import javax.swing.text.Document;
import java.awt.*;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.Callable;
import java.util.concurrent.FutureTask;

/**
 * 「日志区到底滚没滚」诊断探针（进程内，不动鼠标键盘）。
 *
 * 跑法与 GuiLogHarness 一样，走 Log -> GuiLogHandler -> EDT -> McPatchWindow 的真实链路，
 * 区别是这里不打分，只把每一次追加之后的滚动条/视口数值打出来，用来看清失败发生在哪一步：
 *   phase=before   该批日志插入之前
 *   phase=inserted 插入之后、我们的延迟补偿还没跑
 *   phase=settled  延迟补偿跑完之后（用户实际看到的位置）
 */
public class LogScrollProbe {
    static McPatchWindow win;
    static GuiLogHandler handler;

    static final List<String> rows = new ArrayList<>();
    static String phase = "before";

    public static void main(String[] args) throws Exception {
        SetupSwing.init();

        win = new McPatchWindow(660, 480);
        handler = new GuiLogHandler(new GuiLogHandler.Sink() {
            @Override
            public void appendLogMessages(List<Message> messages) {
                phase = "before";
                win.appendLogMessages(messages);

                phase = "inserted";
                sampleNoThrow("inserted");

                SwingUtilities.invokeLater(() -> {
                    phase = "settled";
                    sampleNoThrow("settled");
                    phase = "before";
                });
            }
        }, LogLevel.Debug);

        Log.addHandler(handler);
        win.setTitleText("AutoSync");
        win.setLabelText("诊断探针");
        win.show();
        Thread.sleep(600);

        System.out.println("窗口内容高度=" + edt(() -> win.window.getContentPane().getHeight())
                + " 日志区高度=" + edt(() -> win.logScrollPane.getHeight()));

        caseContinuous(Applies.REAL_LOG, 100, 20);
        caseBatch(400);
        caseBeforeShow();
        caseAfterDividerDrag();
        caseAfterResize();
        caseAfterSelection();

        System.out.println();
        for (String row : rows)
            System.out.println(row);

        System.exit(0);
    }

    enum Applies { REAL_LOG, DIRECT }

    /**
     * 场景一：每 20ms 追一条（真实下载日志的节奏），每次追加前/后/落地后都取样
     */
    static void caseContinuous(Applies mode, int count, int intervalMs) throws Exception {
        header("场景 A：连续追加 " + count + " 条，每 " + intervalMs
                + "ms 一条（" + (mode == Applies.REAL_LOG ? "走 Log/GuiLogHandler" : "直接调 window.appendLogMessages") + "）");

        for (int i = 0; i < count; i++) {
            sample("before#" + i);
            append(mode, "连续追加行 A" + i);
            Thread.sleep(intervalMs);
        }

        Thread.sleep(300);
        sample("after-all");
        row("最后一行可见=" + edt(LogScrollProbe::lastLineVisible)
                + " isLogAtBottom=" + edt(() -> win.isLogAtBottom()));
    }

    /**
     * 场景二：一批 400 条（GuiLogHandler 的单批上限）
     */
    static void caseBatch(int count) throws Exception {
        header("场景 B：一批 " + count + " 条一次性追加");

        sample("before");
        List<Message> batch = new ArrayList<>();

        for (int i = 0; i < count; i++)
            batch.add(message("批量行 B" + i));

        edt(() -> win.appendLogMessages(batch));
        sample("after-edt-return");
        Thread.sleep(400);
        sample("settled");
        row("最后一行可见=" + edt(LogScrollProbe::lastLineVisible)
                + " isLogAtBottom=" + edt(() -> win.isLogAtBottom()));
    }

    /**
     * 场景三：window.show() 之前就产生的日志（弹出时要能回溯启动日志）
     */
    static void caseBeforeShow() throws Exception {
        header("场景 C：show() 之前的日志，弹出后应该能看到并停在底部");

        McPatchWindow hidden = new McPatchWindow(660, 480);
        Log.addHandler(new GuiLogHandler(hidden, LogLevel.Debug));

        // 注意：这里用的是全局 Log，前面的 handler 也会收到，但 hidden 窗口自己也会渲染一份
        for (int i = 0; i < 80; i++)
            Log.info("启动日志 C" + i);

        Thread.sleep(400);
        row("show() 前：hidden 文档长度=" + edt(() -> hidden.logPane.getDocument().getLength())
                + " 文档行数=" + edt(() -> hidden.logLineCount));

        edt(() -> hidden.show());
        Thread.sleep(600);

        row("show() 后：value=" + edt(() -> hidden.logScrollPane.getVerticalScrollBar().getValue())
                + " visible=" + edt(() -> hidden.logScrollPane.getVerticalScrollBar().getVisibleAmount())
                + " max=" + edt(() -> hidden.logScrollPane.getVerticalScrollBar().getMaximum())
                + " 最后一行可见=" + edt(() -> lastLineVisibleOf(hidden))
                + " isLogAtBottom=" + edt(() -> hidden.isLogAtBottom()));

        Log.info("启动日志 C80（show 之后的第一条）");
        Thread.sleep(400);

        row("show() 之后再来一条：value=" + edt(() -> hidden.logScrollPane.getVerticalScrollBar().getValue())
                + " max=" + edt(() -> hidden.logScrollPane.getVerticalScrollBar().getMaximum())
                + " 最后一行可见=" + edt(() -> lastLineVisibleOf(hidden)));

        edt(() -> hidden.destroy());
    }

    /**
     * 场景四：用户拖动分隔条之后（视口高度变了）
     */
    static void caseAfterDividerDrag() throws Exception {
        header("场景 D：拖动分隔条之后追加日志");

        // 先把视图放到底部，保证「用户没上翻」
        edt(() -> {
            JScrollBar bar = win.logScrollPane.getVerticalScrollBar();
            bar.setValue(bar.getMaximum());
        });
        Thread.sleep(200);

        edt(() -> win.splitPane.setDividerLocation(240));
        Thread.sleep(400);
        row("拖动后：日志区高度=" + edt(() -> win.logScrollPane.getHeight())
                + " value=" + edt(() -> win.logScrollPane.getVerticalScrollBar().getValue())
                + " max=" + edt(() -> win.logScrollPane.getVerticalScrollBar().getMaximum()));

        for (int i = 0; i < 20; i++) {
            append(Applies.REAL_LOG, "分隔条之后 D" + i);
            Thread.sleep(20);
        }

        Thread.sleep(400);
        sample("settled");
        row("分隔条之后：最后一行可见=" + edt(LogScrollProbe::lastLineVisible)
                + " isLogAtBottom=" + edt(() -> win.isLogAtBottom()));
    }

    /**
     * 场景五：窗口 resize 之后
     */
    static void caseAfterResize() throws Exception {
        header("场景 E：窗口 resize 之后追加日志");

        edt(() -> {
            JScrollBar bar = win.logScrollPane.getVerticalScrollBar();
            bar.setValue(bar.getMaximum());
        });
        Thread.sleep(200);

        edt(() -> win.window.setSize(win.window.getWidth() - 180, win.window.getHeight() + 160));
        Thread.sleep(500);
        row("resize 后：日志区高度=" + edt(() -> win.logScrollPane.getHeight())
                + " value=" + edt(() -> win.logScrollPane.getVerticalScrollBar().getValue())
                + " max=" + edt(() -> win.logScrollPane.getVerticalScrollBar().getMaximum()));

        for (int i = 0; i < 20; i++) {
            append(Applies.REAL_LOG, "resize 之后 E" + i);
            Thread.sleep(20);
        }

        Thread.sleep(400);
        sample("settled");
        row("resize 之后：最后一行可见=" + edt(LogScrollProbe::lastLineVisible)
                + " isLogAtBottom=" + edt(() -> win.isLogAtBottom()));
    }

    /**
     * 场景六：日志区拿到焦点 + 有文本选择
     */
    static void caseAfterSelection() throws Exception {
        header("场景 F：日志区有焦点、有文本选择时追加日志");

        edt(() -> {
            JScrollBar bar = win.logScrollPane.getVerticalScrollBar();
            bar.setValue(bar.getMaximum());
        });
        Thread.sleep(200);

        edt(() -> {
            win.logPane.requestFocusInWindow();
            win.logPane.setCaretPosition(0);
            win.logPane.moveCaretPosition(40);
        });
        Thread.sleep(300);
        row("选择后：hasFocus=" + edt(() -> win.logPane.hasFocus())
                + " selectionStart=" + edt(() -> win.logPane.getSelectionStart())
                + " value=" + edt(() -> win.logScrollPane.getVerticalScrollBar().getValue())
                + " max=" + edt(() -> win.logScrollPane.getVerticalScrollBar().getMaximum()));

        for (int i = 0; i < 20; i++) {
            append(Applies.REAL_LOG, "选择期间 F" + i);
            Thread.sleep(20);
        }

        Thread.sleep(400);
        sample("settled");
        row("选择期间：最后一行可见=" + edt(LogScrollProbe::lastLineVisible)
                + " isLogAtBottom=" + edt(() -> win.isLogAtBottom()));
    }

    // ------------------------------------------------------------------

    static void append(Applies mode, String text) throws Exception {
        if (mode == Applies.REAL_LOG) {
            Log.info(text);
            return;
        }

        List<Message> one = new ArrayList<>();
        one.add(message(text));
        edt(() -> win.appendLogMessages(one));
    }

    static Message message(String content) {
        Message m = new Message();
        m.time = System.currentTimeMillis();
        m.level = LogLevel.Info;
        m.content = content;
        m.indents = new ArrayList<>();
        return m;
    }

    static void header(String title) {
        rows.add("");
        rows.add("== " + title);
    }

    static void row(String text) {
        rows.add("   " + text);
    }

    /** 在 EDT 上取一次样，追加到结果里 */
    static void sample(String tag) throws Exception {
        if (!SwingUtilities.isEventDispatchThread()) {
            edt(() -> {
                sampleOnEdt(tag);
                return null;
            });
            return;
        }

        sampleOnEdt(tag);
    }

    /** 同上，但不往外抛（EDT 回调里用） */
    static void sampleNoThrow(String tag) {
        try {
            sample(tag);
        } catch (Exception e) {
            rows.add("   [sample 失败] " + tag + " -> " + e);
        }
    }

    static void sampleOnEdt(String tag) {
        JScrollBar bar = win.logScrollPane.getVerticalScrollBar();
        JViewport viewport = win.logScrollPane.getViewport();

        rows.add(String.format("   [%s/%s] doc=%d value=%d visible=%d max=%d 距底=%d 末行可见=%s 末行y=%s 视口高=%d 视图高=%d",
                phase, tag,
                win.logPane.getDocument().getLength(),
                bar.getValue(), bar.getVisibleAmount(), bar.getMaximum(),
                bar.getMaximum() - bar.getVisibleAmount() - bar.getValue(),
                lastLineVisible(), lastLineY(), viewport.getHeight(), viewport.getViewSize().height));
    }

    static String lastLineY() {
        try {
            Document doc = win.logPane.getDocument();
            Rectangle rect = win.logPane.modelToView(Math.max(0, doc.getLength() - 1));

            if (rect == null)
                return "null";

            Point p = SwingUtilities.convertPoint(win.logPane, rect.getLocation(), win.logScrollPane.getViewport());

            return String.valueOf(p.y);
        } catch (Exception e) {
            return "err:" + e;
        }
    }

    static boolean lastLineVisible() {
        return lastLineVisibleOf(win);
    }

    static boolean lastLineVisibleOf(McPatchWindow target) {
        try {
            Document doc = target.logPane.getDocument();
            Rectangle rect = target.logPane.modelToView(Math.max(0, doc.getLength() - 1));

            if (rect == null)
                return false;

            Point p = SwingUtilities.convertPoint(target.logPane, rect.getLocation(), target.logScrollPane.getViewport());

            return p.y >= 0 && p.y < target.logScrollPane.getViewport().getHeight();
        } catch (Exception e) {
            return false;
        }
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
