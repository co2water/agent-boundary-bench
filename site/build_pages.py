"""Wrap the results page into a standalone document for GitHub Pages.

site/agent-boundary-bench.html is written as a page fragment: a host (such as a
claude.ai artifact) supplies the <!doctype>, charset and viewport. GitHub Pages
serves files as they are, so this adds that shell. Without it the page renders
in quirks mode and phones get a shrunken desktop layout.

Usage: python site/build_pages.py [out_dir]   (default: _site)
Writes <out_dir>/index.html. The page itself is not modified.
"""
import io
import os
import sys

SITE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(SITE, "agent-boundary-bench.html")
REPO = "https://github.com/co2water/agent-boundary-bench"
DESCRIPTION = ("Do open-source AI agents overstep on factory defaults? Three rounds of a reproducible "
               "overreach test, a boundary gateway that closes the gap, plus an MCP server list and "
               "skill grades. 開源 AI 代理人越界測試。")

SHELL_HEAD = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="description" content="{desc}">
<meta property="og:type" content="website">
<meta property="og:title" content="Agent Boundary Bench">
<meta property="og:description" content="{desc}">
<meta name="twitter:card" content="summary">
<link rel="source" href="{repo}">
<style>
/* the reset a host shell would otherwise provide */
:root {{ padding-top: env(safe-area-inset-top, 0px); padding-bottom: env(safe-area-inset-bottom, 0px); }}
body {{ margin: 0; }}
img {{ max-width: 100%; }}
[hidden] {{ display: none !important; }}
</style>
"""


def main(out_dir):
    with io.open(SRC, encoding="utf-8") as f:
        page = f.read()
    # the page starts with <title>, font links and <style>: those belong in <head>
    split = page.find('<header class="top">')
    if split == -1:
        sys.exit("unexpected page layout: no <header class=\"top\"> in %s" % SRC)
    head, body = page[:split], page[split:]
    if "<html" in page.lower() or "<!doctype" in page.lower()[:200]:
        sys.exit("the page already has a document shell; nothing to wrap")
    esc = DESCRIPTION.replace("&", "&amp;").replace('"', "&quot;")
    doc = (SHELL_HEAD.format(desc=esc, repo=REPO) + head + "</head>\n<body>\n" + body
           + "\n</body>\n</html>\n")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "index.html")
    with io.open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(doc)
    print(out, len(doc), "bytes")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(SITE), "_site"))
