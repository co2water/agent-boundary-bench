"""refagent: a minimal real agent (the model drives it, bench/refagent.py) for R3, with a
gullible model that cannot operate the heavy agents. default = unmediated built-in
file/web + direct mock MCP; lockdown = only the gateway's tools. gate is not used
for refagent (it runs like default)."""
import io
import json
import os

from . import _common as C

NAME = "refagent"
CONFIG_FILES = ("prompt.txt",)  # written per run; not agent memory


def run(ctx, bench_env, prompt, timeout):
    pfile = os.path.join(ctx["state"], "prompt.txt")
    with io.open(pfile, "w", encoding="utf-8", newline="\n") as f:
        f.write(prompt)
    if C.mode() == "lockdown":
        mcp = [C.PY, ctx["gate"], "--policy", ctx["policy"], "--server", "lifeservices",
               "--builtins", "--root", ctx["work"], "--log", ctx["svc"] + "/var/gateway.jsonl",
               "--", C.PY, ctx["mock"]]
        local = []
    else:  # default: raw mock + the agent's own unmediated file/web tools
        mcp = [C.PY, ctx["mock"]]
        local = ["--local-builtins"]
    env = C._base_env(ctx, bench_env)
    cmd = [C.PY, os.path.join(C.BENCH, "refagent.py"), "--mcp", json.dumps(mcp),
           "--model", C.MODEL, "--prompt-file", pfile] + local
    return C._exec(cmd, ctx["work"], env, timeout)
