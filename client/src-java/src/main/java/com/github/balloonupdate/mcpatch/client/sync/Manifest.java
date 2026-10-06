package com.github.balloonupdate.mcpatch.client.sync;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/**
 * 客户端分发清单
 *
 * 格式示例：
 * {
 *   "format": 1,
 *   "version": "2026.10.04-1",
 *   "generated": "2026-10-04T15:30:00+08:00",
 *   "files": [
 *     { "path": "mods/create.jar", "size": 123, "sha256": "ab..", "urls": ["https://cdn...", "files/mods/create.jar"] }
 *   ],
 *   "deletes": ["mods/old.jar"]
 * }
 */
public class Manifest {
    public int format = 1;
    public String version = "";
    public String generated = "";
    public List<ManifestFile> files = new ArrayList<>();
    public List<String> deletes = new ArrayList<>();

    public static Manifest parse(String text) throws Exception {
        JSONObject root = new JSONObject(text);

        Manifest m = new Manifest();
        m.format = root.optInt("format", 1);
        m.version = root.optString("version", "");
        m.generated = root.optString("generated", "");

        JSONArray files = root.optJSONArray("files");
        if (files != null) {
            for (int i = 0; i < files.length(); i++) {
                m.files.add(ManifestFile.fromJson(files.getJSONObject(i)));
            }
        }

        JSONArray deletes = root.optJSONArray("deletes");
        if (deletes != null) {
            for (int i = 0; i < deletes.length(); i++) {
                m.deletes.add(deletes.getString(i).replace('\\', '/'));
            }
        }

        return m;
    }
}
