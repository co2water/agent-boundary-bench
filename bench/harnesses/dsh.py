"""DeepSeek Harness (dsh): shipped defaults patched with the model and the bench MCP server."""
import io
import json
import os

from . import _common as C

NAME = "dsh"
CONFIG_FILES = ("bench.yml",)  # written per run; not agent memory

DSH_LOCKDOWN_ROWS = ["tool-pwsh", "tool-bash", "tool-jobs", "tool-fs", "tool-fs-search", "tool-web",
                     "tool-subagent", "tool-subagent-fork", "tool-workflow"]


def run(ctx, bench_env, prompt, timeout):
    home = ctx["state"]
    patch = os.path.join(home, "bench.yml")
    provider = "openrouter" if C.OPENROUTER else "deepseek-official"
    with io.open(patch, "w", encoding="utf-8", newline="\n") as f:
        f.write(
            "- id: agent-default-model\n"
            "  config: {provider: %s, model: %s}\n"
            "- id: session-title-llm\n"
            "  disabled: true\n"
            "- id: session-persistence-jsonl\n"
            "  config: {root: '%s/sessions', compression: none}\n"
            % (provider, C.MODEL, home))
        if C.OPENROUTER:
            f.write(
                "- insert:\n"
                "    - name: '@deepseek-ai/dsh-llm-pi-ai'\n"
                "      config:\n"
                "        providers:\n"
                "          openrouter:\n"
                "            api: openai-completions\n"
                "            baseURL: %s\n"
                "            apiKeyEnv: OPENROUTER_API_KEY\n"
                "            models: [{id: %s, contextWindow: 131072}]\n"
                % (C.OR_BASE, C.MODEL))
        f.write(
            "- insert:\n"
            "    - id: mcp-bench\n"
            "      name: '@deepseek-ai/dsh-mcp-client'\n"
            "      config:\n"
            "        serverName: bench\n"
            "        transport: stdio\n"
            "        command: '%s'\n"
            "        args: %s\n"
            "        env: {BENCH_RUN: '%s'}\n"
            "        failOnStartupError: true\n"
            % (C.PY, json.dumps(C.mcp_args(ctx)), bench_env["BENCH_RUN"]))
        if C.mode() == "lockdown":
            for row in DSH_LOCKDOWN_ROWS:
                f.write("- id: %s\n  disabled: true\n" % row)
    env = C._base_env(ctx, bench_env)
    env["DSH_HOME"] = home
    env["DSH_TELEMETRY_DISABLED"] = "1"
    exe = C.config.npm_bin("dsh", "dsh")
    return C._exec([exe, "headless", "--patch", patch, "--json", prompt], ctx["work"], env, timeout)
