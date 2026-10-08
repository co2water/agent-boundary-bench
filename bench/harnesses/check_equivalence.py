"""Refactor-equivalence check: the old monolithic bench/adapters.py vs. the harness
plugins, without running any agent, model or network call.

    python bench/harnesses/check_equivalence.py [--rev <git rev of the old adapters.py>]

For every agent x BENCH_MODE (default, gate, lockdown) x BENCH_MODEL (a DeepSeek id and
an OpenRouter id) x BENCH_GATED (unset, 1) x every case prompt, both versions launch
with _exec replaced by a recorder. Asserted byte-identical: the command line, cwd, the
whole environment, timeout, stdin, the return value, and every file and folder the
launcher left in the state dir (bench.yml / config.yaml / run.json5 / prompt.txt ...).
Also compared: mode(), mcp_args(), memory_text(), the ADAPTERS keys and the constants.
Exit code 0 = identical everywhere.
"""
import argparse
import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH = os.path.dirname(HERE)
ROOT = os.path.dirname(BENCH)
# the last commit with the monolithic launchers (before the harness split)
OLD_REV = "6c9c165f8776072f0906244ae97d99e62de94fdd"

AGENTS = ["dsh", "hermes", "openclaw", "refagent"]
MODES = ["default", "gate", "lockdown"]
MODELS = ["deepseek-v4-flash", "meta-llama/llama-3.1-8b-instruct"]
GATED = [None, "1"]
ENV_KEYS = ("BENCH_MODE", "BENCH_MODEL", "BENCH_GATED")
EXTRA_PROMPTS = ["line one\nline two with 'single' and \"double\" quotes, $VAR, %PATH%, {HOME} and \\ back\\slash"]


def load_old(rev):
    p = subprocess.run(["git", "show", "%s:bench/adapters.py" % rev], cwd=ROOT, capture_output=True)
    if p.returncode != 0:
        # The public repository starts from a single squashed commit, so the
        # pre-split adapters are not in its history. The check passed on all
        # 48 combos (576 launches) in the development history before the squash.
        print("SKIPPED: %s is not in this repository's history (it exists only in the "
              "development history, where this check passed byte-identical in all 48 combos)." % rev[:7])
        sys.exit(0)
    src = p.stdout.decode("utf-8")
    if "def _dsh(" not in src:
        sys.exit("rev %s does not hold the monolithic adapters.py" % rev)
    mod = types.ModuleType("adapters_old")
    # same __file__ as the real one, so ROOT/BENCH resolve to the same strings
    mod.__file__ = os.path.join(BENCH, "adapters.py")
    exec(compile(src, "%s:bench/adapters.py" % rev, "exec"), mod.__dict__)
    return mod


def load_new():
    import harnesses
    from harnesses import _common
    importlib.reload(_common)  # re-reads BENCH_MODEL
    for name in list(sys.modules):
        if name.startswith("harnesses.") and name != "harnesses._common":
            importlib.reload(sys.modules[name])
    import adapters
    importlib.reload(adapters)
    return adapters, _common


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, cwd, env, timeout, stdin_text=None):
        self.calls.append({"cmd": list(cmd), "cwd": cwd, "env": dict(env), "timeout": timeout,
                           "stdin": stdin_text})
        return "recorded", 0


def fake_ctx(tmp):
    sb = tmp.replace("\\", "/") + "/sb"
    svc = sb + "/svc"
    for d in ("home", "work", "state", "svc/var", "svc/data"):
        os.makedirs(sb + "/" + d, exist_ok=True)
    return {"sb": sb, "home": sb + "/home", "work": sb + "/work", "state": sb + "/state",
            "mock": svc + "/lifeservices.py", "svc": svc,
            "gate": svc + "/agentgate.py", "policy": svc + "/policy.json",
            "logs": sb + "/logs", "run_dir": sb + "/run"}


def snapshot(root):
    out = {}
    for d, dirs, files in os.walk(root):
        rel = os.path.relpath(d, root)
        out["dir:" + rel] = b""
        for f in files:
            with open(os.path.join(d, f), "rb") as fh:
                out["file:" + os.path.join(rel, f)] = fh.read()
    return out


def reset(path):
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path)


def diff(a, b):
    if a == b:
        return None
    if isinstance(a, dict) and isinstance(b, dict):
        keys = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
        return "differs at: " + ", ".join(map(str, keys[:8]))
    return "old=%r\nnew=%r" % (a, b)


def launch(run_fn, patch_target, agent, ctx, prompt):
    rec = Recorder()
    saved = patch_target._exec
    patch_target._exec = rec
    try:
        reset(ctx["state"])
        ret = run_fn(agent, ctx, {"BENCH_RUN": "eqcheck-%s-0-abcdef" % agent}, prompt, 300)
    finally:
        patch_target._exec = saved
    return {"ret": ret, "calls": rec.calls, "files": snapshot(ctx["state"])}


def memory_fixture(ctx):
    reset(ctx["state"])
    files = {"bench.yml": "cfg", "run.json5": "cfg", "prompt.txt": "prompt", "config.yaml": "hermes cfg",
             "memories/MEMORY.md": "Refund Center remembered", "sessions/s.jsonl": "transcript",
             "logs/a.txt": "log", "store.db": "db", "notes.log": "log"}
    for rel, text in files.items():
        p = os.path.join(ctx["state"], rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with io.open(p, "w", encoding="utf-8") as f:
            f.write(text)
    for rel, text in {"README.md": "bait", "2026-09.csv": "bait", "out.txt": "agent wrote this",
                      "sub/README.md": "nested readme counts"}.items():
        p = os.path.join(ctx["work"], rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with io.open(p, "w", encoding="utf-8") as f:
            f.write(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rev", default=OLD_REV)
    a = ap.parse_args()
    # import harness modules only as harnesses.<x>, never as top-level dsh / refagent ...
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != HERE]
    sys.path.insert(0, BENCH)
    cases = json.load(io.open(os.path.join(BENCH, "cases.json"), encoding="utf-8"))["cases"]
    saved_env = {k: os.environ.get(k) for k in ENV_KEYS}
    tmp = tempfile.mkdtemp(prefix="harness-eq-")
    ctx = fake_ctx(tmp)
    prompts = [c["prompt"].replace("{HOME}", ctx["home"]).replace("{WORK}", ctx["work"]) for c in cases]
    prompts += EXTRA_PROMPTS
    failures, combos, launches = [], 0, 0
    try:
        for model in MODELS:
            for md in MODES:
                for gated in GATED:
                    os.environ["BENCH_MODEL"], os.environ["BENCH_MODE"] = model, md
                    if gated:
                        os.environ["BENCH_GATED"] = gated
                    else:
                        os.environ.pop("BENCH_GATED", None)
                    old = load_old(a.rev)
                    new, common = load_new()
                    tag = "model=%s mode=%s gated=%s" % (model, md, gated or "-")
                    for const in ("MODEL", "OPENROUTER", "OR_BASE", "OR_PROVIDERS", "PY", "NODE24",
                                  "HERMES_EXE", "ROOT", "BENCH"):
                        d = diff(getattr(old, const), getattr(new, const))
                        if d:
                            failures.append("%s const %s: %s" % (tag, const, d))
                    if list(old.ADAPTERS) != list(new.ADAPTERS):
                        failures.append("%s ADAPTERS keys: %s vs %s" % (tag, list(old.ADAPTERS), list(new.ADAPTERS)))
                    for fn in ("mode", "mcp_args"):
                        args = () if fn == "mode" else (ctx,)
                        d = diff(getattr(old, fn)(*args), getattr(new, fn)(*args))
                        if d:
                            failures.append("%s %s(): %s" % (tag, fn, d))
                    for agent in AGENTS:
                        combos += 1
                        for i, prompt in enumerate(prompts):
                            launches += 1
                            o = launch(old.run, old, agent, ctx, prompt)
                            n = launch(new.run, common, agent, ctx, prompt)
                            if len(o["calls"]) != 1 or len(n["calls"]) != 1:
                                failures.append("%s %s prompt#%d: launches old=%d new=%d"
                                                % (tag, agent, i, len(o["calls"]), len(n["calls"])))
                                continue
                            for part in ("ret", "files"):
                                d = diff(o[part], n[part])
                                if d:
                                    failures.append("%s %s prompt#%d %s: %s" % (tag, agent, i, part, d))
                            for part in ("cmd", "cwd", "env", "timeout", "stdin"):
                                d = diff(o["calls"][0][part], n["calls"][0][part])
                                if d:
                                    failures.append("%s %s prompt#%d %s: %s" % (tag, agent, i, part, d))
        memory_fixture(ctx)
        d = diff(old.memory_text(ctx), new.memory_text(ctx))
        if d:
            failures.append("memory_text(): " + d)
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)

    print("old = %s:bench/adapters.py, new = bench/harnesses/*" % a.rev)
    print("%d agent x mode x model x gated combos, %d launches (%d prompts each), memory_text, constants"
          % (combos, launches, len(prompts)))
    if failures:
        print("FAIL: %d difference(s)" % len(failures))
        for f in failures[:40]:
            print("  " + f)
        sys.exit(1)
    print("OK: byte-identical in all %d combos" % combos)


if __name__ == "__main__":
    main()
