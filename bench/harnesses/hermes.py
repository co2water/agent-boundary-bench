"""Hermes: shipped defaults except model + MCP server; one-shot `chat -q` without a TTY."""
import io
import json
import os

from . import _common as C

NAME = "hermes"
CONFIG_FILES = ()  # config.yaml is deliberately still scanned by memory_text, as before

HERMES_EXE = C.config.hermes_exe()


def run(ctx, bench_env, prompt, timeout):
    home = ctx["state"]
    # shipped defaults except model + MCP; approvals section deliberately omitted
    with io.open(os.path.join(home, "config.yaml"), "w", encoding="utf-8", newline="\n") as f:
        f.write(
            "model:\n"
            "  provider: %s\n"
            "  default: %s\n"
            "mcp_servers:\n"
            "  bench:\n"
            "    command: '%s'\n"
            "    args: %s\n"
            "    env:\n"
            "      BENCH_RUN: '%s'\n"
            "%s"
            % ("openrouter" if C.OPENROUTER else "deepseek", C.MODEL, C.PY, json.dumps(C.mcp_args(ctx)),
               bench_env["BENCH_RUN"],
               # BENCH_GATED: the per-agent hardening shown on the site (checked 2026-10-03)
               "    trust: untrusted\n" if os.environ.get("BENCH_GATED") else ""))
        if C.OPENROUTER:  # pin the backend so Llama tool calls don't land on a broken one
            f.write("providers:\n  openrouter:\n    extra_body:\n      provider:\n"
                    "        order: %s\n        allow_fallbacks: true\n" % json.dumps(C.OR_PROVIDERS))
    env = C._base_env(ctx, bench_env)
    # Windows paths for the native installer; keep POSIX paths elsewhere (Linux / Docker)
    env["HERMES_HOME"] = home.replace("/", "\\") if C.config.WINDOWS else home
    for k in ("HERMES_YOLO_MODE", "HERMES_EXEC_ASK", "HERMES_GATEWAY_SESSION", "HERMES_INTERACTIVE",
              "HERMES_ACCEPT_HOOKS"):
        env.pop(k, None)
    # `chat -q` without a TTY = single-query mode; NOT `hermes -z`, which forces yolo
    cmd = [HERMES_EXE, "chat", "-q", prompt, "--oneshot", "-Q"]
    if C.mode() == "lockdown":
        cmd += ["-t", "mcp-bench,todo"]  # only agentgate's tools (+ a scratch todo list)
    return C._exec(cmd, ctx["work"], env, timeout, stdin_text="")
