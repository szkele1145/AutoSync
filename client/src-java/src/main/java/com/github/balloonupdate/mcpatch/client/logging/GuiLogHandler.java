package com.github.balloonupdate.mcpatch.client.logging;

import javax.swing.*;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * 图形窗口日志记录器：把日志实时推送到窗口下半部的日志区<p>
 * 与 {@link ConsoleHandler}、{@link FileHandler} 一样是 {@link LogHandler} 的一个实现，
 * 区别在于渲染工作必须发生在 Swing 的事件派发线程（EDT）上：<p>
 * 1. {@link #onMessage} 由同步线程调用，只做「拷贝 + 入队 + 排一个 EDT 任务」，绝不阻塞下载线程<p>
 * 2. 连续到来的一大批日志会被合并成一次 EDT 任务（{@link #flush}），避免 EDT 被海量 invokeLater 淹没<p>
 * 3. 队列本身有上限，EDT 万一卡住时丢弃最旧的日志，保证内存有上界
 */
public class GuiLogHandler implements LogHandler {
    /**
     * 日志区的接收方（通常是窗口）。实现方约定在 EDT 上被调用
     */
    public interface Sink {
        /**
         * 追加一批日志。实现方需要保证在 Swing 事件派发线程上执行
         */
        void appendLogMessages(List<Message> messages);
    }

    /**
     * 待渲染队列的上限，超过就丢弃最旧的日志（EDT 正常时根本到不了这个量）
     */
    static final int MAX_PENDING = 4000;

    /**
     * 单次 EDT 任务最多渲染多少条，避免一批太大会让界面「停顿」一下
     */
    static final int MAX_BATCH = 400;

    final Sink sink;

    final LogLevel level;

    final ConcurrentLinkedQueue<Message> pending = new ConcurrentLinkedQueue<>();

    final AtomicInteger pendingCount = new AtomicInteger();

    /**
     * 是否已经排了一个 EDT 渲染任务，保证同时最多只有一个
     */
    final AtomicBoolean flushScheduled = new AtomicBoolean(false);

    volatile boolean stopped = false;

    public GuiLogHandler(Sink sink, LogLevel level) {
        this.sink = sink;
        this.level = level;
    }

    @Override
    public LogLevel getFilterLevel() {
        return level;
    }

    @Override
    public void onStart() {
        stopped = false;
    }

    @Override
    public void onStop() {
        stopped = true;

        pending.clear();
        pendingCount.set(0);
    }

    @Override
    public void onMessage(Message message) {
        if (stopped || message == null)
            return;

        // 必须在这里就把日志拷一份。Message.indents 指向的是 Log 里那个会被 openIndent/closeIndent
        // 继续改的同一个 ArrayList，而真正的渲染是延后到 EDT 上做的；
        // 直接引用会导致日志前缀变成「渲染那一刻才打开的 indent」，日志内容与缩进对不上
        Message copy = new Message();

        copy.time = message.time;
        copy.level = message.level;
        copy.content = message.content;
        copy.indents = message.indents == null ? new ArrayList<>() : new ArrayList<>(message.indents);
        copy.appIdentifier = message.appIdentifier;

        // EDT 跟不上时丢弃最旧的，宁可少显示几行也不能让内存一直涨
        if (pendingCount.get() >= MAX_PENDING && pending.poll() != null)
            pendingCount.decrementAndGet();

        pending.add(copy);
        pendingCount.incrementAndGet();

        scheduleFlush();
    }

    /**
     * 排一个 EDT 渲染任务（已经在队列里了就不再重复排）
     */
    void scheduleFlush() {
        if (stopped)
            return;

        if (flushScheduled.compareAndSet(false, true))
            SwingUtilities.invokeLater(this::flush);
    }

    /**
     * 在 EDT 上把当前攒下的日志一次性交给窗口
     */
    void flush() {
        flushScheduled.set(false);

        if (stopped) {
            pending.clear();
            pendingCount.set(0);
            return;
        }

        List<Message> batch = new ArrayList<>();
        Message message;

        while (batch.size() < MAX_BATCH && (message = pending.poll()) != null)
            batch.add(message);

        if (batch.isEmpty())
            return;

        pendingCount.addAndGet(-batch.size());

        try {
            sink.appendLogMessages(batch);
        } catch (Throwable e) {
            // 界面日志渲染出任何问题都不能反过来把同步流程搞崩，这里直接吞掉
            System.err.println("渲染界面日志失败：" + e);
        }

        // 还没渲染完的下一轮继续，保持实时性
        if (!pending.isEmpty())
            scheduleFlush();
    }

    /**
     * 当前还有多少条日志没渲染（给测试用）
     */
    public int pendingLogCount() {
        return pendingCount.get();
    }
}
