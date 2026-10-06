package com.github.balloonupdate.mcpatch.client.ui;

import com.github.balloonupdate.mcpatch.client.logging.GuiLogHandler;
import com.github.balloonupdate.mcpatch.client.logging.Log;
import com.github.balloonupdate.mcpatch.client.logging.LogLevel;
import com.github.balloonupdate.mcpatch.client.logging.Message;
import com.github.kasuminova.GUI.SetupSwing;

import javax.swing.*;
import javax.swing.border.EmptyBorder;
import javax.swing.text.AttributeSet;
import javax.swing.text.BadLocationException;
import javax.swing.text.DefaultCaret;
import javax.swing.text.DefaultStyledDocument;
import javax.swing.text.Document;
import javax.swing.text.SimpleAttributeSet;
import javax.swing.text.StyleConstants;
import java.awt.*;
import java.awt.event.ActionEvent;
import java.awt.event.InputEvent;
import java.awt.event.KeyEvent;
import java.awt.event.WindowAdapter;
import java.awt.event.WindowEvent;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.function.ToIntFunction;

/**
 * 更新主窗口<p>
 * 窗口被 {@link JSplitPane} 分成上下两块：<p>
 * - 上半部：状态标签 + 进度条，只占刚好放得下的高度（{@link #HEADER_HEIGHT}）<p>
 * - 下半部：实时日志区（{@link JTextPane} + {@link JScrollPane}），日志产生即滚动显示，最多保留
 *   {@link #LOG_MAX_LINES} 行，时间戳/等级标记/正文统一用一种前景色<p>
 * 窗口可以自由缩放，多出来的高度全部给日志区（{@code splitPane.setResizeWeight(0)}）<p>
 * 日志区与窗口背景同色、没有外边距和焦点描边、滚动条只有几像素宽，看起来是「嵌」在窗口里的，
 * 而不是另贴上去的一块面板<p>
 * 日志区的滚动策略：处于跟随状态时，每批日志插入之后都贴到最新一行；用户自己往上翻就停止跟随
 * （见 {@link #shouldFollowLogTail()}），自己翻回底部后自动恢复。拖动分隔条 / 缩放窗口 / 窗口刚显示完
 * 布局这类「视口尺寸变化」不算用户上翻，一律继续跟随
 */
public class McPatchWindow implements GuiLogHandler.Sink {
    /**
     * 日志区最多保留多少行，超出时丢弃最旧的，避免长时间运行内存膨胀
     */
    public static final int LOG_MAX_LINES = 2000;

    /**
     * 判定「滚动条已经在底部」的容差（像素）。滚到底时 value + visibleAmount == maximum，
     * 所以只要还差得不多就认为用户没往上翻
     */
    static final int LOG_BOTTOM_TOLERANCE = 2;

    /**
     * 首次显示时上半部状态区占用的高度（像素）：刚好放得下标题/状态文字/进度条。
     * 之前是 150，压到 100 之后日志区默认就占窗口的大部分高度
     */
    static final int HEADER_HEIGHT = 100;

    /**
     * 上半部状态区的最小高度（像素）。{@link JLabel} 不折行，这里够放下两行状态文字 + 进度条，
     * 拖分隔条也好、窗口变小也好，都不会把状态区截断
     */
    static final int HEADER_MIN_HEIGHT = 96;

    /**
     * 日志区至少保留的高度（像素），窗口很矮时也不会把日志区挤没
     */
    static final int LOG_MIN_HEIGHT = 120;

    /**
     * 日志区竖直滚动条的宽度（像素）：细一点、低调一点，不抢日志的视觉重心
     */
    static final int LOG_SCROLLBAR_WIDTH = 8;

    /**
     * 日志区内边距（像素）：文字不贴边
     */
    static final Insets LOG_PADDING = new Insets(8, 10, 8, 10);

    int width;
    int height;

    JFrame window;
    JLabel label;
    JLabel labelSecondary;
    JProgressBar progressBar;

    /**
     * 上下两块的分隔条
     */
    JSplitPane splitPane;

    /**
     * 实时日志区
     */
    JTextPane logPane;
    JScrollPane logScrollPane;

    /**
     * 日志当前有多少行（空文档算 1 行），用来做限行数，不必每次去数文档
     */
    int logLineCount = 1;

    /**
     * 我们最后一次自动滚动之后，滚动条真正停住的值。<p>
     * 用来区分「滚动条数值变了」的两种完全不同的原因：<p>
     * - 用户自己往上翻/往下拖：值离开了这个位置<p>
     * - 只是视口尺寸或内容高度变了（拖分隔条、拉窗口、窗口刚显示完布局）：值原封不动<p>
     * 之前的判定只看 {@code value + visible >= maximum}，第二种情况会被误判成「用户上翻了」，
     * 于是自动跟随被永久关掉——后面再有日志也不会滚到最新一行（这就是「日志没有自动滚动」的根因）
     */
    int lastAutoScrollValue = 0;

    /**
     * 正在执行我们自己的贴底滚动，用来挡住由此触发的滚动条事件，避免递归
     */
    boolean autoScrolling = false;

    /**
     * 是否已经按像素定过分隔条位置，只在第一次显示窗口时定一次，
     * 免得用户手动拖过之后又被 silent-mode 的第二次 show 重置回去
     */
    boolean dividerInitialized = false;

    /**
     * 窗口是否已被销毁，销毁之后到达的日志直接丢弃
     */
    volatile boolean disposed = false;

    final SimpleDateFormat logTimeFormat = new SimpleDateFormat("HH:mm:ss.SSS", Locale.ROOT);

    // ---------- 日志样式 ----------
    /**
     * 日志区唯一的文字属性：时间戳、等级标记、正文全部同色，不再按等级/前缀分级配色
     */
    AttributeSet styleLog;

    /**
     * 日志区背景。与窗口内容背景完全一致，这样日志区不会显示成一个颜色不同的方块
     */
    Color logBackground;

    /**
     * 日志区唯一的前景色，跟随主题的浅灰/白
     */
    Color logForeground;

    public OnWindowClosing onWindowClosing;

    public McPatchWindow(int width, int height) {
        this.width = width;
        this.height = height;

        window = new JFrame();

        label = new JLabel("空标签空标签空标签空标签空标签空标签空签空标签空标签空签空标签空标签空标签空标签空标签");
        label.setHorizontalAlignment(JLabel.CENTER);
        label.setAlignmentX(Component.CENTER_ALIGNMENT);

        labelSecondary = new JLabel("空标签空标签空标签空标签空标空标签空标签空标空标签空标签空标签空标签空标签空标签空标签");
        labelSecondary.setHorizontalAlignment(JLabel.CENTER);
        labelSecondary.setAlignmentX(Component.CENTER_ALIGNMENT);

        progressBar = new JProgressBar(0, 1000);
        progressBar.setStringPainted(true);
        progressBar.setPreferredSize(new Dimension(320, 26));

        // 日志区：用 JTextPane + StyledDocument，所有文字统一一种前景色
        logPane = new JTextPane(new DefaultStyledDocument());
        logPane.setEditable(false);
        logPane.setFont(new Font(Font.MONOSPACED, Font.PLAIN, 12));
        logPane.setOpaque(true);
        // 内边距由 border 提供；JTextComponent 的 margin 会再叠一层，这里清零免得内外边距翻倍
        logPane.setMargin(new Insets(0, 0, 0, 0));
        logPane.setBorder(new EmptyBorder(LOG_PADDING));
        logPane.setCaret(new DefaultCaret() {
            /**
             * 彻底关掉「插入符自动滚动视图」这一行为。<p>
             * Swing 的 DefaultCaret 会把插入符跟到文档末尾，并且——注意这里——它是在
             * {@code repaintNewCaret()} 里、**稍后的另一个 EDT 任务**中才调用 adjustVisibility 去
             * {@code scrollRectToVisible} 的。所以「只在追加日志期间屏蔽」根本拦不住：等那个延迟任务跑的时候，
             * 追加早就结束了，视图照样会被拽到底部（实测就是这么被拽下去的）。<p>
             * 要真正做到「用户往上翻了就不拉回」，只能让插入符永远不滚动视图，日志区的滚动完全交给
             * {@link #shouldFollowLogTail()} / {@link #scrollLogToBottom()} 决定；键盘滚动另见 {@link #installLogScrollKeys()}
             */
            @Override
            protected void adjustVisibility(Rectangle nloc) {
                // 故意不调用 super：插入符只移动，不滚动视图
            }
        });

        logScrollPane = new JScrollPane(logPane);
        logScrollPane.setVerticalScrollBarPolicy(ScrollPaneConstants.VERTICAL_SCROLLBAR_AS_NEEDED);
        // JTextPane 会按视口宽度自动折行，所以不需要横向滚动条
        logScrollPane.setHorizontalScrollBarPolicy(ScrollPaneConstants.HORIZONTAL_SCROLLBAR_NEVER);
        // 「嵌进去」的关键：去掉 JScrollPane 默认的 FlatLaf 边框（ScrollPane.border = FlatBorder）。
        // 那个边框在日志区拿到焦点时会沿四周画一圈蓝色描边，看着就是另贴上去的一块面板
        logScrollPane.setBorder(null);
        // 滚动条细、低调：宽度既在 UIManager（SetupSwing 里的 ScrollBar.width）里统一收窄，
        // 也在这里显式钉住，免得换 LAF 之后又变粗
        JScrollBar logScrollBar = logScrollPane.getVerticalScrollBar();
        logScrollBar.setUnitIncrement(16);
        logScrollBar.setPreferredSize(new Dimension(LOG_SCROLLBAR_WIDTH, 0));
        logScrollPane.setMinimumSize(new Dimension(0, LOG_MIN_HEIGHT));

        // 滚动条状态一变就检查一次：内容变高、视口变高/变矮（拖分隔条、拉窗口、窗口刚显示完布局）、
        // 用户自己拖动，都会走到这里。只有「确实还在跟随、只是被尺寸变化甩下了一点」时才补一次贴底；
        // 用户自己往上翻的场景 {@link #shouldFollowLogTail()} 返回 false，绝不会把他拉回底部
        logScrollBar.getModel().addChangeListener(e -> followLogTailIfDroppedBehind());

        installLogScrollKeys();
        initLogStyles();

        splitPane = new JSplitPane(JSplitPane.VERTICAL_SPLIT, buildHeaderPanel(), logScrollPane);
        splitPane.setBorder(null);
        splitPane.setDividerSize(8);
        splitPane.setContinuousLayout(true);
        splitPane.setOneTouchExpandable(false);
        // 窗口被拉大时多出来的高度全部给下面的日志区
        splitPane.setResizeWeight(0.0);
        splitPane.setDividerLocation(HEADER_HEIGHT);

        window.getContentPane().setLayout(new BorderLayout());
        window.getContentPane().add(splitPane, BorderLayout.CENTER);

        window.setUndecorated(false);
        window.setVisible(false);
        window.setSize(width, height);
        window.setMinimumSize(new Dimension(420, 260));
        window.setDefaultCloseOperation(JFrame.DO_NOTHING_ON_CLOSE);
        window.setLocationRelativeTo(null);
        window.setResizable(true);
//        window.isAlwaysOnTop = true;

        McPatchWindow that = this;

        window.addWindowListener(new WindowAdapter() {
            @Override
            public void windowClosing(WindowEvent e) {
                if (onWindowClosing != null)
                    onWindowClosing.run(that);
                else
                    destroy();
            }
        });
    }

    public McPatchWindow() {
        this(660, 480);
    }

    /**
     * 上半部：两个状态标签 + 进度条
     */
    JPanel buildHeaderPanel() {
        JPanel texts = new JPanel();
        texts.setLayout(new BoxLayout(texts, BoxLayout.Y_AXIS));
        texts.add(Box.createVerticalGlue());
        texts.add(label);
        texts.add(Box.createVerticalStrut(4));
        texts.add(labelSecondary);
        texts.add(Box.createVerticalGlue());

        JPanel barWrapper = new JPanel(new GridBagLayout());
        barWrapper.add(progressBar);

        JPanel header = new JPanel(new BorderLayout(0, 8));
        header.setBorder(new EmptyBorder(12, 14, 10, 14));
        header.add(texts, BorderLayout.CENTER);
        header.add(barWrapper, BorderLayout.SOUTH);
        header.setMinimumSize(new Dimension(0, HEADER_MIN_HEIGHT));

        return header;
    }

    /**
     * 准备日志区的背景与文字样式。<p>
     * 只有一种前景色：时间戳、等级标记、正文全部同色；背景直接取窗口内容背景，
     * 让日志区与窗口融为一体（不再是一个颜色不同的方块）
     */
    void initLogStyles() {
        // 背景要与窗口内容背景一致。注意这里必须是「非 UIResource」的普通 Color，
        // FlatLaf 才不会在焦点/可用性变化时把文字组件的背景颜色改回去
        Color windowBackground = window.getContentPane().getBackground();

        if (windowBackground == null)
            windowBackground = UIManager.getColor("Panel.background");

        if (windowBackground == null)
            windowBackground = logPane.getBackground();

        // 必须是「非 UIResource」的普通 Color：FlatLaf 只接管 UIResource 的背景，
        // 用主题给的那个 ColorUIResource 直接塞进去，某些主题下会在可用性/可编辑性变化时
        // 把日志区背景改回 TextPane 的默认色（#3C4150 那种），融合感就没了
        logBackground = new Color(windowBackground.getRGB(), true);
        logForeground = pickLogForeground(logBackground);

        logPane.setBackground(logBackground);
        // 组件前景色也要设：DefaultStyledDocument 结尾那个段落换行叶子没有自己的属性，
        // 它的颜色是从组件前景色解析出来的，不设的话日志区最后会混进另一种颜色
        logPane.setForeground(logForeground);
        logScrollPane.setBackground(logBackground);
        logScrollPane.getViewport().setBackground(logBackground);

        styleLog = textStyle(logForeground);
    }

    /**
     * 挑日志区唯一的前景色：优先跟随主题的文字前景色，对比度不够时才退回通用色板
     * （深色背景用浅灰、浅色背景用深灰）。这样 disable-theme 回落到默认 LAF 时也不会白底白字
     */
    static Color pickLogForeground(Color background) {
        Color themed = UIManager.getColor("TextPane.foreground");

        if (themed == null)
            themed = UIManager.getColor("Label.foreground");

        if (background != null && themed != null && contrastRatio(themed, background) >= 4.5)
            // 剥掉 UIResource，避免主题更新时又被换掉
            return new Color(themed.getRGB(), true);

        return luminance(background) < 0.5 ? new Color(0xD8DEE9) : new Color(0x24292E);
    }

    /**
     * 语法糖：造一个「等宽 + 指定颜色」的字符属性
     */
    static AttributeSet textStyle(Color color) {
        SimpleAttributeSet attrs = new SimpleAttributeSet();

        StyleConstants.setForeground(attrs, color);
        StyleConstants.setFontFamily(attrs, Font.MONOSPACED);
        StyleConstants.setFontSize(attrs, 12);

        return attrs;
    }

    /**
     * 求颜色的相对明度，用来判断当前是深色主题还是浅色主题
     */
    static double luminance(Color color) {
        if (color == null)
            return 0;

        return (0.299 * color.getRed() + 0.587 * color.getGreen() + 0.114 * color.getBlue()) / 255.0;
    }

    /**
     * WCAG 相对对比度（1 ~ 21），用来判断前景色在当前背景上是否看得清
     */
    static double contrastRatio(Color foreground, Color background) {
        if (foreground == null || background == null)
            return 1;

        double a = relativeLuminance(foreground);
        double b = relativeLuminance(background);

        return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
    }

    /**
     * 单通道的线性化，用于 {@link #contrastRatio(Color, Color)}
     */
    static double relativeLuminance(Color color) {
        return 0.2126 * linearChannel(color.getRed())
                + 0.7152 * linearChannel(color.getGreen())
                + 0.0722 * linearChannel(color.getBlue());
    }

    static double linearChannel(int value) {
        double v = value / 255.0;

        return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    }

    // 标题栏文字
    public void setTitleText(String value) {
        window.setTitle(value);
    }

    // 显示窗口
    public void show() {
        window.setVisible(true);

        // 第一次显示时窗口才有真实高度，这时候按像素定比例最准
        if (!dividerInitialized) {
            dividerInitialized = true;
            splitPane.setDividerLocation(initialDividerLocation(window.getContentPane().getHeight()));
        }

        // 窗口这时才第一次有真实的视口高度。隐藏期间（静默模式）攒下的启动日志要在这里滚到最新一行：
        // 否则视图会停在第一行，而且旧判定还会把它当成「用户上翻」，之后所有新日志都不再自动滚动。
        // show() 是由主线程调的，所以这里必须保证在 EDT 上执行
        if (SwingUtilities.isEventDispatchThread())
            scrollLogToBottomIfFollowing();
        else
            SwingUtilities.invokeLater(this::scrollLogToBottomIfFollowing);
    }

    /**
     * 初始分隔条位置（像素）：上半部状态区只留 {@link #HEADER_HEIGHT} 这么高，
     * 其余高度全部给日志区；窗口特别矮时按 {@link #HEADER_MIN_HEIGHT} 与
     * {@link #LOG_MIN_HEIGHT} 兜底，两端都不会被挤没
     */
    static int initialDividerLocation(int available) {
        return Math.min(HEADER_HEIGHT, Math.max(HEADER_MIN_HEIGHT, available - LOG_MIN_HEIGHT));
    }

    // 隐藏窗口
    public void hide() {
        window.setVisible(false);
    }

    // 销毁窗口
    public void destroy() {
        disposed = true;
        window.dispose();
    }

    // 进度条上的文字
    public void setProgressBarText(String value) {
        progressBar.setString(value);
        progressBar.setToolTipText(value);
    }

    // 进度条的值

    public void setProgressBarValue(int value) {
        progressBar.setValue(value);
    }

    // 标签上的文字
    public void setLabelText(String value) {
        label.setText(value);
        label.setToolTipText(value);
    }

    // 副签上的文字
    public void setLabelSecondaryText(String value) {
        labelSecondary.setToolTipText(value);
        labelSecondary.setText(value);
    }

    // ------------------------------------------------------------------
    // 实时日志区
    // ------------------------------------------------------------------

    /**
     * 追加一批日志。约定由 {@link GuiLogHandler} 在 EDT 上调用，
     * 这里再兜一层：万一被别的线程直接调用，就转到 EDT 上执行
     */
    @Override
    public void appendLogMessages(List<Message> messages) {
        if (!SwingUtilities.isEventDispatchThread()) {
            List<Message> copy = new ArrayList<>(messages);
            SwingUtilities.invokeLater(() -> appendLogMessages(copy));
            return;
        }

        if (disposed || messages == null || messages.isEmpty())
            return;

        Document document = logPane.getDocument();

        // 关键点：必须在插入之前判断用户是不是还在跟随最新日志。
        // 插入之后滚动条的 maximum 会变大，那时候再判断就永远是「不在底部」了
        boolean stickToBottom = shouldFollowLogTail();

        try {
            for (Message message : messages)
                appendOneLog(document, message);

            if (logLineCount > LOG_MAX_LINES)
                trimLogLines(document);
        } catch (BadLocationException e) {
            // 日志区渲染失败绝不能影响同步，丢掉这一批就算了
            return;
        }

        // 用户手动往上翻了就别再把他拉回底部
        if (stickToBottom)
            scrollLogToBottom();
    }

    /**
     * 渲染一条日志：时间 + 等级 + 缩进 + 内容。<p>
     * 全部使用同一种前景色（{@link #styleLog}），不再按等级或 [CDN]/[测速]/[服务端]/[镜像模式]
     * 这类前缀分级配色
     */
    void appendOneLog(Document document, Message message) throws BadLocationException {
        LogLevel level = message.level == null ? LogLevel.Info : message.level;

        String content = message.content == null ? "" : message.content;
        // 统一换行符，避免 Windows 风格换行把日志区撑出多余空行
        content = content.replace("\r\n", "\n").replace('\r', '\n');

        String time = logTimeFormat.format(new Date(message.time));

        String levelText = String.format(Locale.ROOT, "[ %-5s ] ", level.name().toUpperCase(Locale.ROOT));

        String indentText = (message.indents == null || message.indents.isEmpty())
                ? "" : String.join(" ", message.indents) + " ";

        String firstPrefix = time + " " + levelText + indentText;

        // 多行日志的后续行只留等宽空白，让内容左对齐
        String continuationPrefix = " ".repeat(firstPrefix.length());

        String[] lines = content.split("\n", -1);

        for (int i = 0; i < lines.length; i++) {
            insert(document, i == 0 ? firstPrefix : continuationPrefix, styleLog);
            insert(document, lines[i], styleLog);
            insert(document, "\n", styleLog);

            logLineCount += 1;
        }
    }

    static void insert(Document document, String text, AttributeSet attrs) throws BadLocationException {
        if (text.isEmpty())
            return;

        document.insertString(document.getLength(), text, attrs);
    }

    /**
     * 把上下方向键 / PageUp / PageDown / Home / End 绑到日志区的滚动条上<p>
     * 插入符已经被改成「只移动、不滚动视图」，键盘就得更明确地滚视口，
     * 否则按 PageUp 只会移动一个看不见的插入符，界面纹丝不动
     */
    void installLogScrollKeys() {
        InputMap keys = logPane.getInputMap(JComponent.WHEN_FOCUSED);
        ActionMap actions = logPane.getActionMap();

        bindLogScrollKey(keys, actions, KeyStroke.getKeyStroke(KeyEvent.VK_UP, 0), "log-scroll-unit-up",
                bar -> bar.getValue() - bar.getUnitIncrement());
        bindLogScrollKey(keys, actions, KeyStroke.getKeyStroke(KeyEvent.VK_DOWN, 0), "log-scroll-unit-down",
                bar -> bar.getValue() + bar.getUnitIncrement());
        bindLogScrollKey(keys, actions, KeyStroke.getKeyStroke(KeyEvent.VK_PAGE_UP, 0), "log-scroll-page-up",
                bar -> bar.getValue() - logPageIncrement(bar));
        bindLogScrollKey(keys, actions, KeyStroke.getKeyStroke(KeyEvent.VK_PAGE_DOWN, 0), "log-scroll-page-down",
                bar -> bar.getValue() + logPageIncrement(bar));
        bindLogScrollKey(keys, actions, KeyStroke.getKeyStroke(KeyEvent.VK_HOME, 0), "log-scroll-home",
                bar -> 0);
        bindLogScrollKey(keys, actions, KeyStroke.getKeyStroke(KeyEvent.VK_END, 0), "log-scroll-end",
                bar -> bar.getMaximum());
        bindLogScrollKey(keys, actions, KeyStroke.getKeyStroke(KeyEvent.VK_HOME, InputEvent.CTRL_DOWN_MASK),
                "log-scroll-ctrl-home", bar -> 0);
        bindLogScrollKey(keys, actions, KeyStroke.getKeyStroke(KeyEvent.VK_END, InputEvent.CTRL_DOWN_MASK),
                "log-scroll-ctrl-end", bar -> bar.getMaximum());
    }

    /**
     * 翻滚一页的像素数（比一屏略少一点，方便看到衔接处）
     */
    static int logPageIncrement(JScrollBar bar) {
        return Math.max(bar.getUnitIncrement(), bar.getVisibleAmount() - bar.getUnitIncrement());
    }

    /**
     * 把一个按键绑成「把滚动条设成某个值」，setValue 自己会夹在合法范围里
     */
    void bindLogScrollKey(InputMap keys, ActionMap actions, KeyStroke stroke, String name, ToIntFunction<JScrollBar> target) {
        keys.put(stroke, name);
        actions.put(name, new AbstractAction() {
            @Override
            public void actionPerformed(ActionEvent e) {
                JScrollBar bar = logScrollPane.getVerticalScrollBar();

                bar.setValue(target.applyAsInt(bar));
            }
        });
    }

    /**
     * 当前滚动条是不是停在底部（内容没占满时也算在底部）
     */
    boolean isLogAtBottom() {
        return barIsAtBottom(logScrollPane.getVerticalScrollBar());
    }

    /**
     * 判定滚动条是否在底部（纯函数，方便测试与在别的线程上读）
     */
    static boolean barIsAtBottom(JScrollBar bar) {
        return bar.getValue() + bar.getVisibleAmount() >= bar.getMaximum() - LOG_BOTTOM_TOLERANCE;
    }

    /**
     * 是否应该继续贴住最新一行（自动跟随）<p>
     * 两种情况算「应该跟随」：<p>
     * 1. 滚动条就在底部——用户本来没翻，或者自己翻回来了<p>
     * 2. 滚动条还停在我们上次自动滚动放的位置——这中间的落差只可能是视口/内容尺寸变化
     *    （拖分隔条、拉窗口、窗口刚显示完布局、首次布局未完成）造成的，不是用户上翻<p>
     * 只有「不在底部、且值也离开了我们放的位置」才认定用户主动上翻，此后不再自动滚动
     */
    boolean shouldFollowLogTail() {
        JScrollBar bar = logScrollPane.getVerticalScrollBar();

        return barIsAtBottom(bar) || bar.getValue() == lastAutoScrollValue;
    }

    /**
     * 处于跟随时把视图贴到底部，不在 EDT 上就转到 EDT 上做
     */
    void scrollLogToBottomIfFollowing() {
        if (disposed || !shouldFollowLogTail())
            return;

        scrollLogToBottom();
    }

    /**
     * 把日志区滚到最底部<p>
     * 刚插入的内容还没走完布局，滚动条的 maximum 有可能还是旧值，所以这里补一次延迟滚动；
     * 但补之前会重新确认一次「还在跟随」——用户若在这期间往上滚了（值离开了我们放的位置、
     * 也不在底部），就绝不把他拉回来
     */
    void scrollLogToBottom() {
        applyScrollToBottom();

        SwingUtilities.invokeLater(() -> {
            if (!disposed && shouldFollowLogTail())
                applyScrollToBottom();
        });
    }

    /**
     * 真正把滚动条放到底部，并记下我们放的位置（{@link #lastAutoScrollValue}）<p>
     * {@code setValue(getMaximum())} 会被模型夹到 {@code maximum - visibleAmount}，所以放完要读回来
     */
    void applyScrollToBottom() {
        if (autoScrolling)
            return;

        autoScrolling = true;

        try {
            JScrollBar bar = logScrollPane.getVerticalScrollBar();

            bar.setValue(bar.getMaximum());
            lastAutoScrollValue = bar.getValue();
        } finally {
            autoScrolling = false;
        }
    }

    /**
     * 滚动条状态变化时的补救：内容变高、视口尺寸变化把我们甩下时，补一次贴底<p>
     * 用户自己滚动、或已经不在跟随时，这里什么都不做
     */
    void followLogTailIfDroppedBehind() {
        if (disposed || autoScrolling)
            return;

        // 滚动条只应该在 EDT 上动，但窗口 show() 是由主线程调的（见 Main.java），
        // 显示过程中的布局可能从主线程把事件推过来，这里统一转回 EDT
        if (!SwingUtilities.isEventDispatchThread()) {
            SwingUtilities.invokeLater(this::followLogTailIfDroppedBehind);
            return;
        }

        JScrollBar bar = logScrollPane.getVerticalScrollBar();

        if (!barIsAtBottom(bar) && shouldFollowLogTail())
            applyScrollToBottom();
    }

    /**
     * 丢掉最旧的若干行，让日志区回到 {@link #LOG_MAX_LINES} 行以内<p>
     * 一次删掉一整段（而不是一行一行删），删多少行就找第几个换行符
     */
    void trimLogLines(Document document) throws BadLocationException {
        while (logLineCount > LOG_MAX_LINES) {
            int removeLines = logLineCount - LOG_MAX_LINES;
            int offset = nthNewlineOffset(document, removeLines);

            if (offset < 0) {
                // 理论上不该发生，兜底直接把整个文档清掉
                document.remove(0, document.getLength());
                logLineCount = 1;
                return;
            }

            document.remove(0, offset + 1);
            logLineCount -= removeLines;
        }
    }

    /**
     * 找第 n 个（从 1 开始）换行符在文档里的位置，找不到返回 -1
     */
    static int nthNewlineOffset(Document document, int n) throws BadLocationException {
        int chunk = 8192;
        int searched = 0;
        int found = 0;
        int length = document.getLength();

        while (searched < length) {
            int size = Math.min(chunk, length - searched);
            String text = document.getText(searched, size);

            for (int i = 0; i < text.length(); i++) {
                if (text.charAt(i) == '\n') {
                    found += 1;

                    if (found == n)
                        return searched + i;
                }
            }

            searched += size;
        }

        return -1;
    }

    @FunctionalInterface
    public interface OnWindowClosing {
        void run(McPatchWindow window);
    }

    // 开发时调试用
    public static void main(String[] args) throws Exception {
        SetupSwing.init();

        McPatchWindow window = new McPatchWindow();

        Log.addHandler(new GuiLogHandler(window, LogLevel.Debug));

        window.setTitleText("AutoSync");
        window.setLabelText("正在连接到更新服务器");
        window.setLabelSecondaryText("demo.jar");
        window.setProgressBarValue(420);
        window.setProgressBarText("12.00 MB/28.00 MB  -  3.20 MB/s");
        window.show();

        new Thread(() -> {
            Log.info("已用内存: 128.00 MB");
            Log.info("图形模式: true");
            Log.openIndent("更新源选择");
            Log.info("更新源尝试顺序：msfp://127.0.0.1:8123");
            Log.closeIndent();
            Log.info("[测速] 样本 big.jar（24.00 MB，本次待下载）");
            Log.info("[测速] CDN 3.21 MB/s  |  服务端 1.42 MB/s  ->  选 CDN（达服务端 226%）");
            Log.info("[CDN] big.jar 从 127.0.0.1 下载成功");
            Log.warn("[CDN] mid.jar 失败（连接超时），回退服务端");
            Log.info("[服务端] small.jar（命中 cdn-exclude）");
            Log.info("镜像模式：已移除清单外文件 mods/old.jar -> .modsync-removed/mods/old.jar");
            Log.error("有 1 个文件下载失败，本次更新中止（不会应用任何文件）：");
            Log.debug("下载 mods/big.jar <- msfp://127.0.0.1:8123");
        }, "log-demo").start();
    }
}
