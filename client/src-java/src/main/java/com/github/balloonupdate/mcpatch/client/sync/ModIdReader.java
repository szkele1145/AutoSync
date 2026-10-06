package com.github.balloonupdate.mcpatch.client.sync;

import com.github.balloonupdate.mcpatch.client.logging.Log;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.Set;
import java.util.regex.Pattern;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;

/**
 * 模组 modId 读取器<p>
 * 从 jar 里解析 META-INF/neoforge.mods.toml（1.20.5+ / 1.21.x）或 META-INF/mods.toml（旧版 Forge），<p>
 * 取出所有 [[mods]] 段里的 modId，用于检测重复模组冲突<p>
 *
 * 这里只做极简的逐行扫描，不引入任何 TOML 依赖；解析失败一律返回空集合，绝不抛异常中断同步
 */
public class ModIdReader {
    /**
     * 新版（1.20.5+ / 1.21.x）的模组描述文件名
     */
    public static final String NEOFORGE_TOML = "META-INF/neoforge.mods.toml";

    /**
     * 旧版 Forge 的模组描述文件名
     */
    public static final String FORGE_TOML = "META-INF/mods.toml";

    /**
     * 匹配 [[mods]] 段头，允许两侧空白与行尾注释
     */
    private static final Pattern MODS_HEADER = Pattern.compile("^\\[\\[\\s*mods\\s*\\]\\].*$");

    /**
     * 匹配 modId = 值 这种写法（TOML 的键大小写敏感，这里放宽为不区分大小写以增强兼容性）
     */
    private static final Pattern MOD_ID_LINE = Pattern.compile("^modid\\s*=\\s*(.*)$", Pattern.CASE_INSENSITIVE);

    /**
     * 读取一个 jar 里声明的所有 modId<p>
     * 读取不到（不是有效 jar、没有 toml、格式异常）时返回空集合，只记录 debug 日志
     *
     * @param jarPath jar 文件路径
     * @return modId 集合，可能为空但不会是 null
     */
    public static Set<String> readModIds(Path jarPath) {
        if (jarPath == null || !Files.isRegularFile(jarPath)) {
            return Collections.emptySet();
        }

        try (ZipFile zip = new ZipFile(jarPath.toFile(), StandardCharsets.UTF_8)) {
            // 新版名字优先，找不到再回退到旧版名字
            ZipEntry entry = zip.getEntry(NEOFORGE_TOML);

            if (entry == null) {
                entry = zip.getEntry(FORGE_TOML);
            }

            if (entry == null) {
                Log.debug("模组 " + jarPath.getFileName() + " 里没有找到 " + NEOFORGE_TOML + " 或 " + FORGE_TOML + "，跳过 modId 解析");
                return Collections.emptySet();
            }

            try (InputStream stream = zip.getInputStream(entry)) {
                return parseModIds(stream);
            }
        } catch (Exception e) {
            Log.debug("解析模组 " + jarPath.getFileName() + " 的 modId 失败: " + e);
            return Collections.emptySet();
        }
    }

    /**
     * 从 toml 文本流里解析出所有 [[mods]] 段中的 modId
     */
    static Set<String> parseModIds(InputStream stream) throws IOException {
        Set<String> result = new LinkedHashSet<>();

        BufferedReader reader = new BufferedReader(new InputStreamReader(stream, StandardCharsets.UTF_8));
        boolean inModsBlock = false;
        String line;

        while ((line = reader.readLine()) != null) {
            String text = line.trim();

            if (text.isEmpty() || text.startsWith("#")) {
                continue;
            }

            // 遇到任何 table 头（[xxx] 或 [[xxx]]）就切换当前所在的段
            if (text.startsWith("[")) {
                inModsBlock = MODS_HEADER.matcher(text).matches();
                continue;
            }

            if (!inModsBlock) {
                continue;
            }

            String value = matchModId(text);

            if (value != null && !value.isEmpty()) {
                result.add(value);
            }
        }

        return result;
    }

    /**
     * 判断一行是不是 modId 赋值行，是则返回它的值（去掉引号与行尾注释）
     */
    static String matchModId(String line) {
        java.util.regex.Matcher matcher = MOD_ID_LINE.matcher(line);

        if (!matcher.matches()) {
            return null;
        }

        return parseValue(matcher.group(1));
    }

    /**
     * 解析 toml 的值部分：支持双引号、单引号以及裸值，并去掉行尾注释
     */
    static String parseValue(String raw) {
        if (raw == null) {
            return null;
        }

        String text = raw.trim();

        if (text.isEmpty()) {
            return null;
        }

        char first = text.charAt(0);

        // 引号包裹的字符串，取到与之配对的引号为止，引号内的 # 不做注释处理
        if (first == '"' || first == '\'') {
            int end = text.indexOf(first, 1);

            if (end > 0) {
                return text.substring(1, end).trim();
            }

            // 引号没闭合，尽力而为
            return text.substring(1).trim();
        }

        // 裸值：取到第一个空白或注释符之前
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);

            if (c == '#' || Character.isWhitespace(c)) {
                return text.substring(0, i).trim();
            }
        }

        return text;
    }
}
