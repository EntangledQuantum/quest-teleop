#!/usr/bin/env python3
"""Regenerates the derived files of the GitHub Pages site in site/ (standard library only).

    python tools/build_site.py

The hand-written sources are site/index.html, site/assets/site.css and the Markdown pages in site/docs/*.md. This
script writes, next to them:

    site/docs/protocol.md, measurements.md   copies of docs/PROTOCOL.md and docs/MEASUREMENTS.md
    site/docs/<page>.html                    every Markdown page as HTML
    site/llms.txt, site/llms-full.txt        the index for AI agents and all pages in one file
    site/sitemap.xml, site/404.html
    site/assets/img/*.png, assets/fonts/*    copies of docs/img and the status page's fonts

The output is committed, so the site needs no build step to be served. Run it after editing a page.
"""
from __future__ import annotations

import html
import os
import re
import shutil
import struct

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "site")
DOCS = os.path.join(SITE, "docs")
BASE = "https://entangledquantum.github.io/quest-teleop/"
REPO = "https://github.com/EntangledQuantum/quest-teleop"
VERSION = "0.2.0"

# slug, title, one-line description (used in the navigation, llms.txt and <meta name="description">)
PAGES = [
    ("index", "Overview", "What quest-teleop is, its parts, and the order to read these pages in."),
    ("install", "Install", "Install the Python library with pip; install the Android toolchain, build the APK and "
                           "install it on the headset; build the desktop client; run the tests."),
    ("quest-setup", "Quest 2 setup", "Developer mode, the USB detected / allow USB debugging prompts, Link auto "
                                     "connect off, the udev rule on Linux, firewall ports TCP 47800 and UDP "
                                     "47800-47801, USB link with adb reverse, launch options."),
    ("quick-start", "Quick start", "Run the bundled demo, connect the headset, the desktop client or the probe, "
                                   "the controls, record a take and open it on the status page."),
    ("library", "Use it with your own simulator", "The Python API: Scene, TeleopServer, controller input, haptics, "
                                                  "text cues, the headset panel and actions, safe hand-to-tool "
                                                  "mapping, the go-back cue, latency measurement."),
    ("recording", "Recording takes", "TakeRecorder, the files on disk, every array in a take, and loading takes."),
    ("status-page", "Status page and trajectory viewer", "The local web page: what it shows, the extra callback, "
                                                         "the trajectory viewer, the JSON endpoints."),
    ("protocol", "Protocol", "The wire format, version 1: TCP and UDP channels, discovery, session, datagrams, "
                             "state delta scheme, TCP messages, panel."),
    ("measurements", "Measurements", "Latency and bandwidth on loopback and on a Quest 2, over USB and Wi-Fi."),
    ("troubleshooting", "Troubleshooting", "The headset does not appear in adb, the build fails, the app does not "
                                           "find the server, nothing moves, high latency, the status page does "
                                           "not open."),
    ("faq", "FAQ", "Supported headsets, engines, Link, operating systems, Wi-Fi or USB, bandwidth, several "
                   "headsets, own clients, privacy, licence."),
]
COPIED = {"protocol": "docs/PROTOCOL.md", "measurements": "docs/MEASUREMENTS.md"}


# ------------------------------------------------------------------------------------------------ Markdown -> HTML
def slugify(text: str) -> str:
    """GitHub-style heading anchor."""
    text = re.sub(r"[`*]", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def png_size(path: str):
    with open(path, "rb") as f:
        head = f.read(24)
    return struct.unpack(">II", head[16:24])


def md_link(url: str) -> str:
    """Relative links to Markdown pages point at the HTML page in the HTML output."""
    if re.match(r"^[a-z]+:", url) or url.startswith("#"):
        return url
    m = re.match(r"^([\w\-./]*?)\.md(#.*)?$", url)
    return f"{m.group(1)}.html{m.group(2) or ''}" if m else url


def inline(text: str, page_dir: str) -> str:
    codes: list[str] = []

    def keep(s: str) -> str:
        codes.append(s)
        return f"\x00{len(codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", lambda m: keep(f"<code>{html.escape(m.group(1), quote=False)}</code>"), text)

    def image(m):
        alt, src = m.group(1), m.group(2)
        attrs = ""
        fp = os.path.normpath(os.path.join(page_dir, src))
        if src.endswith(".png") and os.path.isfile(fp):
            w, h = png_size(fp)
            attrs = f' width="{w}" height="{h}"'
        return keep(f'<img src="{html.escape(src)}" alt="{html.escape(alt)}"{attrs} loading="lazy" decoding="async">')

    text = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", image, text)
    text = re.sub(r"<(https?://[^>\s]+)>", lambda m: keep(f'<a href="{html.escape(m.group(1))}">{html.escape(m.group(1))}</a>'), text)
    links: list[tuple[str, str]] = []

    def link(m):
        links.append((m.group(1), m.group(2)))
        return f"\x01{len(links) - 1}\x01"

    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, text)
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    def anchor(m):
        label, url = links[int(m.group(1))]
        label = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html.escape(label, quote=False))
        return f'<a href="{html.escape(md_link(url))}">{label}</a>'

    text = re.sub(r"\x01(\d+)\x01", anchor, text)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


def split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    cells, cur, in_code = [], "", False
    for ch in line:
        if ch == "`":
            in_code = not in_code
        if ch == "|" and not in_code:
            cells.append(cur.strip())
            cur = ""
        else:
            cur += ch
    cells.append(cur.strip())
    return cells


def markdown_to_html(md: str, page_dir: str) -> tuple[str, str, list]:
    """(title, body html, [(level, id, text)] of the h2 / h3 headings)."""
    lines = md.split("\n")
    out, toc, title = [], [], ""
    i, n = 0, len(lines)
    para: list[str] = []
    used_ids: dict[str, int] = {}

    def flush_para():
        if para:
            text = " ".join(s.strip() for s in para)
            if re.fullmatch(r"!\[[^\]]*\]\([^)]+\)", text):
                out.append(f"<figure>{inline(text, page_dir)}</figure>")
            else:
                out.append(f"<p>{inline(text, page_dir)}</p>")
            para.clear()

    while i < n:
        line = lines[i]
        if line.startswith("```"):
            flush_para()
            lang = line[3:].strip()
            j = i + 1
            code = []
            while j < n and not lines[j].startswith("```"):
                code.append(lines[j])
                j += 1
            cls = f' class="language-{html.escape(lang)}"' if lang else ""
            label = html.escape(lang or "text")
            out.append(f'<div class="code"><pre tabindex="0" aria-label="{label} code"><code{cls}>'
                       f'{html.escape(chr(10).join(code), quote=False)}</code></pre></div>')
            i = j + 1
            continue
        m = re.match(r"^(#{1,4}) (.+)$", line)
        if m:
            flush_para()
            level, text = len(m.group(1)), m.group(2).strip()
            if level == 1 and not title:
                title = re.sub(r"[`*]", "", text)
            hid = slugify(text)
            if hid in used_ids:
                used_ids[hid] += 1
                hid = f"{hid}-{used_ids[hid]}"
            else:
                used_ids[hid] = 0
            if level in (2, 3):
                toc.append((level, hid, re.sub(r"[`*]", "", text)))
            anchor = f' <a class="anchor" href="#{hid}" aria-label="Link to this section">#</a>' if level > 1 else ""
            out.append(f'<h{level} id="{hid}">{inline(text, page_dir)}{anchor}</h{level}>')
            i += 1
            continue
        if line.startswith("|") and i + 1 < n and re.match(r"^\|?\s*:?-{3,}", lines[i + 1]):
            flush_para()
            head = split_row(line)
            rows = []
            j = i + 2
            while j < n and lines[j].startswith("|"):
                rows.append(split_row(lines[j]))
                j += 1
            t = ['<div class="table"><table>']
            if any(head):
                t.append("<thead><tr>" + "".join(f'<th scope="col">{inline(c, page_dir)}</th>' for c in head) + "</tr></thead>")
            t.append("<tbody>")
            for r in rows:
                cells = []
                for k, c in enumerate(r):
                    tag = "th" if k == 0 and not any(head) else "td"
                    cells.append(f"<{tag}>{inline(c, page_dir)}</{tag}>")
                t.append("<tr>" + "".join(cells) + "</tr>")
            t.append("</tbody></table></div>")
            out.append("".join(t))
            i = j
            continue
        m = re.match(r"^(\*|-|\d+\.) (.*)$", line)
        if m:
            flush_para()
            ordered = m.group(1)[0].isdigit()
            items: list[list[str]] = []
            j = i
            while j < n:
                mm = re.match(r"^(\*|-|\d+\.) (.*)$", lines[j])
                if mm and (mm.group(1)[0].isdigit()) == ordered:
                    items.append([mm.group(2)])
                elif lines[j].startswith((" ", "\t")) and lines[j].strip() and items:
                    items[-1].append(lines[j].strip())
                else:
                    break
                j += 1
            tag = "ol" if ordered else "ul"
            start = ""
            if ordered:
                first = int(re.match(r"^(\d+)", line).group(1))
                start = f' start="{first}"' if first != 1 else ""
            out.append(f"<{tag}{start}>" + "".join(f"<li>{inline(' '.join(it), page_dir)}</li>" for it in items) + f"</{tag}>")
            i = j
            continue
        if not line.strip():
            flush_para()
        else:
            para.append(line)
        i += 1
    flush_para()
    return title, "\n".join(out), toc


# ------------------------------------------------------------------------------------------------ page template
FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect width='16' "
           "height='16' fill='%23060709'/%3E%3Crect x='4' y='4' width='8' height='8' fill='%238d90ff'/%3E%3C/svg%3E")


def page_html(slug: str, title: str, desc: str, body: str, toc: list) -> str:
    nav = []
    for s, t, _ in PAGES:
        cur = ' aria-current="page"' if s == slug else ""
        nav.append(f'<li><a href="{s}.html"{cur}>{html.escape(t)}</a></li>')
    k = [p[0] for p in PAGES].index(slug)
    prev_next = []
    if k > 0:
        s, t, _ = PAGES[k - 1]
        prev_next.append(f'<a class="pn prev" href="{s}.html"><span class="label">Previous</span>{html.escape(t)}</a>')
    if k + 1 < len(PAGES):
        s, t, _ = PAGES[k + 1]
        prev_next.append(f'<a class="pn next" href="{s}.html"><span class="label">Next</span>{html.escape(t)}</a>')
    on_page = ""
    if len([1 for lv, _, _ in toc if lv == 2]) >= 3:
        items = "".join(f'<li><a href="#{hid}">{html.escape(text)}</a></li>' for lv, hid, text in toc if lv == 2)
        on_page = f'<nav class="onpage" aria-label="On this page"><p class="label">On this page</p><ol>{items}</ol></nav>'
    body = body.replace("</h1>", "</h1>" + on_page, 1) if on_page else body
    full_title = "quest-teleop docs" if slug == "index" else f"{title} · quest-teleop docs"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(full_title)}</title>
<meta name="description" content="{html.escape(desc)}">
<link rel="canonical" href="{BASE}docs/{slug}.html">
<link rel="alternate" type="text/markdown" href="{slug}.md" title="This page as Markdown">
<link rel="help" href="../llms.txt" type="text/plain" title="llms.txt">
<link rel="icon" href="{FAVICON}">
<link rel="stylesheet" href="../assets/site.css">
</head>
<body class="docs">
<a class="skip" href="#main">Skip to content</a>
<header class="topbar">
  <a class="brand" href="../index.html">quest-teleop</a>
  <nav aria-label="Site">
    <ul>
      <li><a href="index.html"{' aria-current="page"' if slug == "index" else ""}>Docs</a></li>
      <li><a href="{REPO}">GitHub</a></li>
      <li><a href="../llms.txt">llms.txt</a></li>
    </ul>
  </nav>
</header>
<div class="edge" role="presentation"></div>
<div class="docs-layout">
  <nav class="sidenav" aria-label="Documentation pages">
    <p class="label">Documentation</p>
    <ol>
      {chr(10).join('      ' + x for x in nav).strip()}
    </ol>
  </nav>
  <main id="main">
    <article class="prose">
{body}
    </article>
    <footer class="page-foot">
      <p class="md-link">This page as Markdown: <a href="{slug}.md" type="text/markdown"><code>{BASE}docs/{slug}.md</code></a></p>
      <nav class="prev-next" aria-label="Previous and next page">{''.join(prev_next)}</nav>
    </footer>
  </main>
</div>
<footer class="site-foot">
  <p>quest-teleop {VERSION}, MIT licence. <a href="{REPO}">Source on GitHub</a>. Fonts: Host Grotesk and Martian Mono,
  <a href="../assets/fonts/OFL.txt">SIL Open Font License 1.1</a>. No tracking, no cookies.</p>
</footer>
</body>
</html>
"""


# ------------------------------------------------------------------------------------------------ agent files
def absolutize(md: str, slug: str) -> str:
    """Relative links in a page -> absolute URLs (for llms-full.txt, where the page's own location is lost)."""
    def fix(m):
        bang, text, url = m.group(1), m.group(2), m.group(3)
        if not re.match(r"^[a-z]+:", url):
            url = url if not url.startswith("#") else f"{slug}.md{url}"
            url = BASE + os.path.normpath(os.path.join("docs", url)).replace(os.sep, "/")
        return f"{bang}[{text}]({url})"
    return re.sub(r"(!?)\[([^\]]*)\]\(([^)\s]+)\)", fix, md)


LLMS_HEAD = f"""# quest-teleop

> Generic VR teleoperation for the Meta Quest 2: a native OpenXR + OpenGL ES 3 headset app (C++, no game engine,
> no Gradle) and a pip-installable Python library (numpy only) that streams any 3D scene to the headset, receives
> both controllers every display frame, sends haptics and text cues, shows an in-headset panel with your own
> actions, records takes, and serves a local status page with a trajectory viewer. Includes a desktop test client
> and a demo.

- Repository: {REPO} (MIT licence), version {VERSION}
- Install the library: `pip install git+{REPO}` (Python 3.10+). Commands: `quest-teleop-demo`, `quest-teleop-probe`
- Build and install the headset app (Linux, from a clone): `android/setup_toolchain.sh` then `android/build_apk.sh install`
- Network: TCP 47800 and UDP 47800-47801 open on the computer, or `adb reverse tcp:47800 tcp:47800` over USB
- Status page: http://localhost:8800 while the demo or your server runs
- Every page below is plain Markdown; the same page as HTML is at the same path with `.html`

## Instructions for agents

Setting up quest-teleop for a user, in order:

1. Install the Python library and check it: `python -c "import quest_teleop; print(quest_teleop.__version__)"`.
   This alone is enough to run `quest-teleop-demo` and check it with `quest-teleop-probe` (no headset).
2. For the headset app, clone the repository and run `android/setup_toolchain.sh`. Tell the user first: it
   downloads about 2.4 GB into `~/android-sdk` and accepts the Android SDK licence agreements on their behalf.
   It needs cmake, make, curl, unzip, zip and Java 17+ (see Install).
3. Some steps need the user in person; ask them to do these and to tell you when done: enable developer mode in the
   Meta Horizon phone app; plug in the headset, put it on, tap "USB detected" and allow; accept "Allow USB
   debugging"; turn "Link auto connect" off. Then check `adb devices` lists the headset as `device`.
4. Some steps need root: the udev rule on Linux and the firewall ports. Show the user the exact command and ask
   before running anything with sudo. Over USB (`adb reverse`), the firewall change is not needed.
5. `android/build_apk.sh install`, then `quest-teleop-demo`, then start Quest Teleop in the headset. The status
   page at http://localhost:8800 (or `curl -s http://localhost:8800/api/state`) shows whether the headset connected.
6. To connect the user's own simulator or robot, follow "Use it with your own simulator" (a complete runnable
   example is at the top) and "Recording takes".
7. If something fails, read Troubleshooting before changing anything.
"""


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def main() -> None:
    # copies: Markdown docs kept in docs/, images, fonts
    for slug, src in COPIED.items():
        with open(os.path.join(ROOT, src), encoding="utf-8") as f:
            md = f.read()
        first, rest = md.split("\n", 1)
        note = f"\n\nThis page is [`{src}`]({REPO}/blob/main/{src}) from the repository.\n"
        write(os.path.join(DOCS, f"{slug}.md"), first + note + rest)
    img_dst = os.path.join(SITE, "assets", "img")
    os.makedirs(img_dst, exist_ok=True)
    for f in sorted(os.listdir(os.path.join(ROOT, "docs", "img"))):
        shutil.copyfile(os.path.join(ROOT, "docs", "img", f), os.path.join(img_dst, f))
    font_src = os.path.join(ROOT, "server", "quest_teleop", "status_page", "fonts")
    font_dst = os.path.join(SITE, "assets", "fonts")
    os.makedirs(font_dst, exist_ok=True)
    for f in sorted(os.listdir(font_src)):
        shutil.copyfile(os.path.join(font_src, f), os.path.join(font_dst, f))

    full = [LLMS_HEAD.replace("## Instructions for agents", "This file contains every documentation page, in "
                              "reading order. The index is " + BASE + "llms.txt.\n\n## Instructions for agents")]
    llms = [LLMS_HEAD, "\n## Docs\n\n"]
    for slug, title, desc in PAGES:
        with open(os.path.join(DOCS, f"{slug}.md"), encoding="utf-8") as f:
            md = f.read()
        md_title, body, toc = markdown_to_html(md, DOCS)
        write(os.path.join(DOCS, f"{slug}.html"), page_html(slug, md_title or title, desc, body, toc))
        llms.append(f"- [{title}]({BASE}docs/{slug}.md): {desc}\n")
        full.append(f"\n\n---\n\n<!-- page: {BASE}docs/{slug}.md -->\n\n{absolutize(md, slug).strip()}\n")
    llms.append(f"\n## Optional\n\n"
                f"- [All pages in one file]({BASE}llms-full.txt): the whole documentation as one Markdown file\n"
                f"- [README]({REPO}/blob/main/README.md): the repository's own overview\n"
                f"- [Python package source]({REPO}/tree/main/server/quest_teleop): the library, with docstrings\n"
                f"- [Headset app source]({REPO}/tree/main/client): C++ core, Quest entry point, desktop client\n")
    write(os.path.join(SITE, "llms.txt"), "".join(llms))
    write(os.path.join(SITE, "llms-full.txt"), "".join(full).rstrip() + "\n")

    urls = [BASE] + [f"{BASE}docs/{s}.html" for s, _, _ in PAGES] + [f"{BASE}llms.txt", f"{BASE}llms-full.txt"]
    write(os.path.join(SITE, "sitemap.xml"), '<?xml version="1.0" encoding="UTF-8"?>\n'
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
          + "".join(f"  <url><loc>{u}</loc></url>\n" for u in urls) + "</urlset>\n")
    write(os.path.join(SITE, "robots.txt"), f"User-agent: *\nAllow: /\nSitemap: {BASE}sitemap.xml\n")
    write(os.path.join(SITE, "404.html"), f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Not found · quest-teleop</title>
<link rel="icon" href="{FAVICON}">
<link rel="stylesheet" href="/quest-teleop/assets/site.css">
</head>
<body>
<main id="main" class="wrap notfound">
  <p class="label">404</p>
  <h1 class="page-title">Page not found</h1>
  <p class="page-desc">Try the <a href="/quest-teleop/">home page</a>, the <a href="/quest-teleop/docs/index.html">documentation</a>
  or <a href="/quest-teleop/llms.txt">llms.txt</a>.</p>
</main>
</body>
</html>
""")
    print(f"wrote {len(PAGES)} pages, llms.txt, llms-full.txt, sitemap.xml, robots.txt, 404.html into {SITE}")


if __name__ == "__main__":
    main()
