package com.github.balloonupdate.mcpatch.client.sync;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/**
 * 清单里的一个文件条目
 */
public class ManifestFile {
    /**
     * 相对于游戏目录的路径，例如 mods/create-1.21.1.jar
     */
    public String path;

    /**
     * 文件大小，-1 表示未知
     */
    public long size = -1;

    /**
     * SHA-256，空字符串表示跳过校验
     */
    public String sha256 = "";

    /**
     * 下载地址列表，按顺序尝试。
     * 绝对地址（http/https）直接使用；相对地址会拼到清单所在更新源后面。
     */
    public List<String> urls = new ArrayList<>();

    public static ManifestFile fromJson(JSONObject o) {
        ManifestFile f = new ManifestFile();

        f.path = o.getString("path").replace('\\', '/');
        f.size = o.optLong("size", -1);
        f.sha256 = o.optString("sha256", "").toLowerCase();

        JSONArray arr = o.optJSONArray("urls");
        if (arr != null) {
            for (int i = 0; i < arr.length(); i++) {
                String u = arr.optString(i, "");
                if (!u.isEmpty()) {
                    f.urls.add(u);
                }
            }
        }

        return f;
    }

    @Override
    public String toString() {
        return path + " (" + size + " bytes, " + urls.size() + " sources)";
    }
}
