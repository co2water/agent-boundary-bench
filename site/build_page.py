"""Merge scored results + narrative.json into site/agent-boundary-bench.html.

Usage: python build_page.py <R1 results dir> [gate=<dir> lockdown=<dir>]  (score.py each first)
"""
import io
import json
import os
import sys

SITE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SITE)


def main(results_dir, r2_dirs=None):
    summary = json.load(io.open(os.path.join(results_dir, "summary.json"), encoding="utf-8"))
    cases = json.load(io.open(os.path.join(ROOT, "bench", "cases.json"), encoding="utf-8"))["cases"]
    nar = json.load(io.open(os.path.join(SITE, "narrative.json"), encoding="utf-8"))
    titles_en = nar.get("case_titles_en", {})

    agents = []
    for a in nar["agents"]:
        s = summary["summary"][a["id"]]
        agents.append(dict(a, A=s["A"], B=s["B"], total=s["AB"], utility=s["utility"],
                           held_by_harness=s["held_by_harness"], held_total=s["held_total"],
                           ci=s.get("ci")))  # 95% Wilson interval per layer (score.py)
    agents.sort(key=lambda a: -a["total"])

    keep = ("agent", "case", "rep", "violated", "fired", "delivered", "utility", "harness_block")
    runs = [{k: r[k] for k in keep} for r in summary["rows"]]
    data = {
        "lede": nar["lede"],
        "lede_en": nar.get("lede_en"),
        "conditions": dict(nar["conditions"], runs=len(runs), cases=len(cases)),
        "agents": agents,
        "findings": nar["findings"],
        # case titles come from bench/cases.json; their English is site copy in narrative.json
        "cases": [{"id": c["id"], "title": c["title"], "title_en": titles_en.get(c["id"]),
                   "layer": c["layer"], "weight": c["weight"]} for c in cases],
        "runs": runs,
        "defaults": nar["defaults"],
        "method": nar["method"],
        "footer": nar["footer"],
        "footer_en": nar.get("footer_en"),
        "summary": nar.get("summary"),
    }
    # R2: the same cases with agentgate (site/r2.json holds the copy; scores come from the runs)
    r2p = os.path.join(SITE, "r2.json")
    if r2_dirs and os.path.isfile(r2p):
        r2 = json.load(io.open(r2p, encoding="utf-8"))
        sums = {"default": summary["summary"]}
        for cid, d in r2_dirs.items():
            sums[cid] = json.load(io.open(os.path.join(d, "summary.json"), encoding="utf-8"))["summary"]
        r2["agents"] = [{"id": a["id"], "name": a["name"],
                         "scores": {cid: {"A": s[a["id"]]["A"], "B": s[a["id"]]["B"], "total": s[a["id"]]["AB"],
                                          "utility": s[a["id"]]["utility"], "ci": s[a["id"]].get("ci")}
                                    for cid, s in sums.items() if a["id"] in s}}
                        for a in agents]
        fp = os.path.join(SITE, "data", "fooled_results.json")
        if os.path.isfile(fp):
            f = json.load(io.open(fp, encoding="utf-8"))
            r2["fooled"] = {"lede": r2.get("fooled_lede", ""), "results": f["results"]}
        r3p = os.path.join(SITE, "data", "r3.json")
        if os.path.isfile(r3p):
            r2["r3"] = json.load(io.open(r3p, encoding="utf-8"))
        data["r2"] = r2
    # pages 2 and 3: researched lists (site/data/*.json) + page copy (site/*_page.json)
    for key, page, listfile, listkey in (("mcp", "mcp_page.json", "mcp_list.json", "servers"),
                                         ("skills", "skills_page.json", "skills_list.json", "skills")):
        lp = os.path.join(SITE, "data", listfile)
        if os.path.isfile(lp):
            block = json.load(io.open(os.path.join(SITE, page), encoding="utf-8"))
            block[listkey] = json.load(io.open(lp, encoding="utf-8"))[listkey]
            data[key] = block
    tpl = io.open(os.path.join(SITE, "template.html"), encoding="utf-8").read()
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    out = tpl.replace("/*__DATA__*/null", blob)
    path = os.path.join(SITE, "agent-boundary-bench.html")
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(out)
    print(path, len(out), "bytes")


if __name__ == "__main__":
    # python build_page.py <R1 dir> [gate=<dir> lockdown=<dir>]
    extra = dict(arg.split("=", 1) for arg in sys.argv[2:])
    main(sys.argv[1], extra or None)
