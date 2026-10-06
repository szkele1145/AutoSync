package com.github.balloonupdate.mcpatch.client.exceptions;

/**
 * 代表业务异常，出现这个异常时，下载会自动进行重试
 */
public class McpatchBusinessException extends Exception {
    public McpatchBusinessException(String message) {
        super(message);
    }

    public McpatchBusinessException(String message, Exception e) {
        // e 允许为 null（例如所有下载来源都失败、但没有任何一个异常能代表「最后一个原因」时），
        // 不能直接 e.getClass()，否则会抛出一个莫名其妙的 NullPointerException 把真实错误盖掉
        super(message + "，原因：" + describe(e), e);
    }

    public McpatchBusinessException(Exception e) {
        super("好像出现了错误\n" + describe(e), e);
    }

    /**
     * 把异常压成「类名: 消息」，null 时给一句人能看懂的话
     */
    static String describe(Exception e) {
        if (e == null) {
            return "（没有可用的异常信息）";
        }

        String name = e.getClass().getSimpleName();
        String message = e.getMessage();

        return message == null || message.isEmpty() ? name : name + ": " + message;
    }

    @Override
    public String toString() {
        StringBuilder sb = new StringBuilder();

        Throwable cause = getCause();

        sb.append(getMessage());
        sb.append("\n");

        if (cause != null) {
            sb.append(stackTraceToString(cause));
        } else {
            sb.append(stackTraceToString(this));
        }

        return sb.toString();
    }

    /**
     * 获取错误的调用堆栈并做成字符串返回
     */
    static String stackTraceToString(Throwable e) {
        StringBuilder sb = new StringBuilder();

        StackTraceElement[] frames = e.getStackTrace();

        for (int i = 0; i < frames.length; i++) {
//            sb.append("    ");
//            sb.append(i);
//            sb.append(": ");
            sb.append(frames[i].toString());
            sb.append("\n");
        }

        return sb.toString();
    }
}
