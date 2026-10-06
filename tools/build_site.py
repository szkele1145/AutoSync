#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 docs/*.md 转成站内 HTML 页面（供 GitHub Pages 使用）。

用法::

    python tools/build_site.py

做的事：
    1. 读 docs/ 下的全部 .md（中文文件名）
    2. 用 markdown 库转成 HTML 片段（启用 extra + toc 扩展）
    3. 套上站点外壳（与 docs/index.html 同一套视觉：深色 + 青紫渐变 + 透视网格）
    4. 生成 docs/<slug>.html（英文 slug，避免 URL 编码问题）
       - 顶部导航 + 左侧文档列表 + 正文 + 上一页/下一页
    5. 同时把 docs/index.html 里文档卡片的链接指向站内页面

依赖：pip install markdown
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

try:
    import markdown
except ImportError:
    print("缺少 markdown 库，请先：pip install markdown")
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"
INDEX = DOCS / "index.html"

# (中文文件名, slug, 标题, emoji, 一句话说明)
PAGES: list[tuple[str, str, str, str, str]] = [
    ("相比原版的改进.md", "compare",        "相比原版的改进", "⚖️", "逐条对比 McPatch2 的改动、痛点与实测数据"),
    ("部署-Linux.md",    "deploy-linux",   "Linux 部署",     "🐧", "Ubuntu 24 从零部署 + systemd + frp"),
    ("部署-Windows.md",  "deploy-windows", "Windows 部署",   "🪟", "Windows 上跑服务端 + 两种自启方式"),
    ("客户端配置.md",     "client-config",  "客户端配置",     "⚙️", "mcpatch.yml 每个键的含义与建议值"),
    ("服务端-Python版.md", "server-python",  "服务端 · Python 版", "🐍", "独立版安装与完整命令表"),
    ("服务端-MCDR版.md",  "server-mcdr",    "服务端 · MCDR 版",   "🧩", "MCDR 插件安装、命令与权限"),
    ("常见问题.md",       "faq",            "常见问题",       "❓", "连不上、缺前缀、下载慢、误删恢复"),
    ("MSFP协议.md",      "msfp",           "MSFP 协议",      "📡", "MSFP v1 协议规范（二次开发用）"),
]

GITHUB = "https://github.com/szkele1145/AutoSync"
PAGES_URL = "https://szkele1145.github.io/AutoSync"

# 与 index.html 共用的视觉变量
BASE_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#0a0e14;--panel:#10151f;--panel2:#141b26;--line:#1e2733;
  --txt:#dfe6f0;--dim:#78839a;--dim2:#57606f;
  --cyan:#56b6c2;--green:#98c379;--yellow:#e5c07b;--red:#e06c75;
  --purple:#c678dd;--blue:#61afef;
}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--txt);line-height:1.8;
  font-family:"Cascadia Code","JetBrains Mono",Consolas,"Microsoft YaHei",-apple-system,sans-serif;
  font-size:15px;-webkit-font-smoothing:antialiased}
#bg{position:fixed;inset:0;z-index:-2;pointer-events:none;
  background:
    radial-gradient(ellipse 70% 50% at 50% -8%,rgba(86,182,194,.12),transparent 62%),
    radial-gradient(ellipse 50% 40% at 88% 106%,rgba(198,120,221,.09),transparent 60%),
    var(--bg)}
#grid{position:fixed;inset:-60%;z-index:-1;pointer-events:none;
  background-image:
    linear-gradient(rgba(86,182,194,.045) 1px,transparent 1px),
    linear-gradient(90deg,rgba(86,182,194,.045) 1px,transparent 1px);
  background-size:52px 52px;
  transform:perspective(680px) rotateX(60deg);
  mask-image:radial-gradient(ellipse 60% 55% at 50% 26%,#000 10%,transparent 70%);
  -webkit-mask-image:radial-gradient(ellipse 60% 55% at 50% 26%,#000 10%,transparent 70%)}

header{position:sticky;top:0;z-index:50;backdrop-filter:blur(11px);
  background:rgba(10,14,20,.86);border-bottom:1px solid var(--line)}
.nav{max-width:1240px;margin:0 auto;padding:0 22px;display:flex;align-items:center;gap:22px;height:58px}
.brand{font-weight:800;font-size:18px;text-decoration:none;
  background:linear-gradient(100deg,var(--cyan),var(--blue) 45%,var(--purple) 82%);
  -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.nav a.lnk{color:var(--dim);text-decoration:none;font-size:13.5px}
.nav a.lnk:hover{color:var(--cyan)}
.nav .sp{flex:1}
.nav a.gh{border:1px solid var(--line);padding:5px 13px;border-radius:7px;color:var(--txt);
  text-decoration:none;font-size:13px}
.nav a.gh:hover{border-color:var(--cyan);color:var(--cyan)}

.layout{max-width:1240px;margin:0 auto;padding:30px 22px 70px;display:grid;
  grid-template-columns:238px 1fr;gap:34px;align-items:start}
aside{position:sticky;top:80px;background:var(--panel);border:1px solid var(--line);
  border-radius:11px;padding:15px;max-height:calc(100vh - 110px);overflow:auto}
aside .hd{font-size:11.5px;letter-spacing:.1em;color:var(--dim2);margin-bottom:11px}
aside a{display:flex;gap:9px;padding:8px 10px;border-radius:7px;color:var(--dim);
  text-decoration:none;font-size:13.5px;transition:.16s;margin-bottom:2px}
aside a:hover{background:var(--panel2);color:var(--txt)}
aside a.on{background:rgba(86,182,194,.11);color:var(--cyan);font-weight:600}
aside a .ic{flex:none}

main{min-width:0}
.crumb{font-size:12.5px;color:var(--dim2);margin-bottom:16px}
.crumb a{color:var(--dim);text-decoration:none}
.crumb a:hover{color:var(--cyan)}

article{background:var(--panel);border:1px solid var(--line);border-radius:13px;
  padding:clamp(22px,3.4vw,42px);overflow-wrap:break-word}
article h1{font-size:clamp(25px,3.6vw,36px);font-weight:800;margin-bottom:8px;line-height:1.3}
article h1::after{content:'';display:block;width:50px;height:3px;margin-top:15px;border-radius:2px;
  background:linear-gradient(90deg,var(--cyan),var(--purple))}
article h2{font-size:clamp(19px,2.5vw,25px);font-weight:700;margin:38px 0 13px;
  padding-top:19px;border-top:1px solid var(--line)}
article h3{font-size:17.5px;font-weight:700;margin:26px 0 10px;color:var(--cyan)}
article h4{font-size:15.5px;font-weight:700;margin:20px 0 8px;color:#b9c3d1}
article p{margin:12px 0;color:#c6cedb}
article a{color:var(--cyan);text-decoration:none;border-bottom:1px solid rgba(86,182,194,.3)}
article a:hover{border-bottom-color:var(--cyan)}
article ul,article ol{margin:12px 0 12px 24px;color:#c6cedb}
article li{margin:6px 0}
article li::marker{color:var(--dim)}
article strong{color:#eaf0f8;font-weight:700}
article em{color:var(--yellow);font-style:normal}
article hr{border:none;border-top:1px solid var(--line);margin:30px 0}
article blockquote{border-left:3px solid var(--cyan);background:rgba(86,182,194,.05);
  padding:11px 17px;border-radius:0 8px 8px 0;margin:16px 0;color:#aab4c2}
article blockquote p{margin:5px 0;color:#aab4c2}

article code{background:#0c1119;border:1px solid var(--line);border-radius:5px;
  padding:2px 6px;font-size:13px;color:var(--yellow);font-family:inherit}
article pre{background:#0c1119;border:1px solid var(--line);border-radius:9px;
  padding:15px 18px;overflow-x:auto;margin:15px 0;font-size:13px;line-height:1.85}
article pre code{background:none;border:none;padding:0;color:#c3cbd8;font-size:13px;
  white-space:pre;display:block}

article table{width:100%;border-collapse:collapse;margin:17px 0;font-size:13.5px;display:block;
  overflow-x:auto}
article th,article td{text-align:left;padding:9px 13px;border-bottom:1px solid var(--line);
  white-space:nowrap}
article th{color:var(--dim);font-weight:600;font-size:12.5px;letter-spacing:.04em;
  background:rgba(86,182,194,.035)}
article td{color:#c2cad6}
article tr:hover td{background:rgba(86,182,194,.03)}

.pager{display:flex;justify-content:space-between;gap:13px;margin-top:24px;flex-wrap:wrap}
.pager a{flex:1;min-width:180px;padding:13px 17px;border-radius:10px;border:1px solid var(--line);
  background:var(--panel);text-decoration:none;transition:.2s}
.pager a:hover{border-color:rgba(86,182,194,.45);background:var(--panel2)}
.pager a .l{font-size:11.5px;color:var(--dim2);display:block;margin-bottom:3px}
.pager a .t{font-size:14px;color:var(--txt);font-weight:600}
.pager a.nx{text-align:right}

footer{max-width:1240px;margin:0 auto;padding:30px 22px 50px;text-align:center;
  color:var(--dim2);font-size:12.5px;border-top:1px solid var(--line)}
footer a{color:var(--dim);text-decoration:none}
footer a:hover{color:var(--cyan)}

@media(max-width:900px){
  .layout{grid-template-columns:1fr;gap:20px}
  aside{position:static;max-height:none}
  aside .list{display:flex;flex-wrap:wrap;gap:5px}
  aside a{margin:0}
}
"""

SHELL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — AutoSync 文档</title>
<meta name="description" content="{desc}">
<style>{css}</style>
</head>
<body>
<div id="bg"></div><div id="grid"></div>

<header>
  <div class="nav">
    <a class="brand" href="index.html">AutoSync</a>
    <a class="lnk" href="index.html">首页</a>
    <a class="lnk" href="{pages_url}/compare.html">改进对比</a>
    <span class="sp"></span>
    <a class="gh" href="{github}" target="_blank" rel="noopener">GitHub ↗</a>
  </div>
</header>

<div class="layout">
  <aside>
    <div class="hd">文档</div>
    <div class="list">{nav}</div>
  </aside>
  <main>
    <div class="crumb"><a href="index.html">首页</a> / 文档 / {title}</div>
    <article>
{body}
    </article>
    <div class="pager">{pager}</div>
  </main>
</div>

<footer>
  <a href="{github}" target="_blank" rel="noopener">GitHub</a> ·
  <a href="{github}/blob/main/LICENSE" target="_blank" rel="noopener">MIT License</a> ·
  基于 <a href="https://github.com/BalloonUpdate/McPatch2" target="_blank" rel="noopener">McPatch2</a> 改造 ·
  本项目由 <b style="color:var(--purple)">DeepSeek-V4.1-Flash</b> 辅助制作
</footer>
</body>
</html>
"""


def build_nav(current_slug: str) -> str:
    out = ['<a href="index.html"><span class="ic">🏠</span>首页</a>']
    for _, slug, title, icon, _ in PAGES:
        on = ' class="on"' if slug == current_slug else ""
        out.append(f'<a href="{slug}.html"{on}><span class="ic">{icon}</span>{html.escape(title)}</a>')
    return "\n      ".join(out)


def build_pager(i: int) -> str:
    parts = []
    if i > 0:
        _, slug, title, _, _ = PAGES[i - 1]
        parts.append(f'<a href="{slug}.html"><span class="l">上一篇</span><span class="t">← {html.escape(title)}</span></a>')
    if i < len(PAGES) - 1:
        _, slug, title, _, _ = PAGES[i + 1]
        parts.append(f'<a class="nx" href="{slug}.html"><span class="l">下一篇</span><span class="t">{html.escape(title)} →</span></a>')
    return "\n      ".join(parts)


def convert_md(md_path: Path) -> str:
    text = md_path.read_text(encoding="utf-8")
    md = markdown.Markdown(
        extensions=["extra", "toc", "sane_lists", "nl2br"],
        extension_configs={"toc": {"permalink": False}},
    )
    return md.convert(text)


def main() -> int:
    if not DOCS.is_dir():
        print(f"找不到文档目录：{DOCS}")
        return 1

    made = 0
    for i, (md_name, slug, title, _icon, desc) in enumerate(PAGES):
        src = DOCS / md_name
        if not src.is_file():
            print(f"  !! 跳过（文件不存在）：{md_name}")
            continue
        body = convert_md(src)
        out_html = SHELL.format(
            title=html.escape(title),
            desc=html.escape(desc),
            css=BASE_CSS,
            nav=build_nav(slug),
            pager=build_pager(i),
            body=body,
            github=GITHUB,
            pages_url=PAGES_URL,
        )
        (DOCS / f"{slug}.html").write_text(out_html, encoding="utf-8")
        size = len(out_html.encode("utf-8")) / 1024
        print(f"  {md_name:<20} -> {slug}.html  ({size:.1f} KB)")
        made += 1

    # 把 index.html 的文档卡片指向站内页面
    if INDEX.is_file():
        s = INDEX.read_text(encoding="utf-8")
        for md_name, slug, *_ in PAGES:
            s = s.replace(f"{REPO_URL}{md_name}", f"{slug}.html")
        INDEX.write_text(s, encoding="utf-8")
        print(f"  index.html 文档卡片已改为站内链接")

    print(f"\n完成：生成 {made} 个文档页")
    return 0


REPO_URL = "https://github.com/szkele1145/AutoSync/blob/main/docs/"

if __name__ == "__main__":
    sys.exit(main())
