"""OpenClaw: provider + MCP server declared in a per-run run.json5; `agent exec` one-shot."""
import io
import json
import os

from . import _common as C

NAME = "openclaw"
CONFIG_FILES = ("run.json5", "prompt.txt")  # written per run; not agent memory

OPENCLAW_LOCKDOWN_DENY = ["group:runtime", "group:fs", "group:web", "group:ui", "group:nodes",
                          "group:messaging", "group:automation", "group:sessions"]


def _tools():
    if C.mode() == "lockdown":
        return "  tools: { deny: %s },\n" % json.dumps(OPENCLAW_LOCKDOWN_DENY)
    if os.environ.get("BENCH_GATED"):
        return '  tools: { deny: ["bench__pay_merchant", "bench__send_message"] },\n'
    return ""


def run(ctx, bench_env, prompt, timeout):
    state = ctx["state"]
    cfg = os.path.join(state, "run.json5")
    if C.OPENROUTER:
        prov = ('deepseek: {}, openrouter: { baseUrl: "%s", apiKey: "${OPENROUTER_API_KEY}", '
                'api: "openai-completions", models: [{ id: "%s", name: "%s", input: ["text"], '
                'contextWindow: 131072, maxTokens: 8192, '
                'params: { extra_body: { provider: { order: %s, allow_fallbacks: true } } } }] }'
                % (C.OR_BASE, C.MODEL, C.MODEL, json.dumps(C.OR_PROVIDERS)))
        primary = "openrouter/" + C.MODEL
    else:
        prov = ('deepseek: { baseUrl: "https://api.deepseek.com", apiKey: "${DEEPSEEK_API_KEY}", '
                'api: "openai-completions", models: [{ id: "%s", name: "DeepSeek V4 Flash", input: ["text"], '
                'contextWindow: 1000000, maxTokens: 384000, '
                'compat: { requiresReasoningContentOnAssistantMessages: true } }] }' % C.MODEL)
        primary = "deepseek/" + C.MODEL
    with io.open(cfg, "w", encoding="utf-8", newline="\n") as f:
        # provider declared in-config; the key stays an env reference, never written
        f.write("""{
  models: { mode: "merge", providers: { %s } },
  agents: { defaults: { model: { primary: "%s" } } },
  mcp: { servers: { bench: {
    command: "%s", args: %s,
    env: { BENCH_RUN: "%s" },
    requestTimeoutMs: 60000 } } },
%s}
""" % (prov, primary, C.PY, json.dumps(C.mcp_args(ctx)), bench_env["BENCH_RUN"], _tools()))
    pfile = os.path.join(state, "prompt.txt")
    with io.open(pfile, "w", encoding="utf-8", newline="\n") as f:
        f.write(prompt)
    env = C._base_env(ctx, bench_env)
    env["PATH"] = C.NODE24 + os.pathsep + env.get("PATH", "")
    env["OPENCLAW_HOME"] = os.path.join(state, "ochome")
    os.makedirs(env["OPENCLAW_HOME"], exist_ok=True)
    os.makedirs(os.path.join(state, "oc"), exist_ok=True)
    exe = C.config.npm_bin("openclaw", "openclaw")
    return C._exec([exe, "agent", "exec", "--config", cfg, "--state-dir", os.path.join(state, "oc"),
                    "--cwd", ctx["work"], "--model", primary, "--json",
                    "--message-file", pfile], ctx["work"], env, timeout)
