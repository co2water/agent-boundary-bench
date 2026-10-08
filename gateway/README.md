# agentgate

A boundary gateway between an AI agent and its MCP servers. The agent launches
agentgate instead of the MCP server; agentgate starts the real server behind it and
decides every `tools/call` from a policy file — not from what the model says.
Standard library only, one file (`gateway.py`), Python 3.11+.

**Status: prototype (0.3.0). Don't put it in front of real payment or messaging
tools yet.** It went through four rounds of adversarial review by separate AI
reviewer agents, not an independent audit (fix notes at the top of `gateway.py`).
One finding is still open: the private-address check resolves a host, then urllib
resolves it again to connect, so a TTL-0 DNS rebind can still reach a LAN address.
[THREAT_MODEL.md](THREAT_MODEL.md) lists what it does not defend.

## Try it in 60 seconds

Needs [uv](https://docs.astral.sh/uv/) (or pipx) and Python 3.11+. No API key, no
network beyond fetching the code, synthetic data, no money moves:

```
uvx --from "git+https://github.com/co2water/agent-boundary-bench@agentgate-v0.3.0#subdirectory=gateway" agentgate demo
```

A scripted agent reads a mock inbox (allowed) and then tries to pay "Sunny Cafe"
NT$1,200. agentgate holds the payment and opens its confirmation page in your
browser, showing the real arguments. Press **批准這一次** (approve once) and the mock
server returns a fake receipt; press **拒絕** (deny) or wait, and the agent gets
`BLOCKED_BY_AGENTGATE`. `agentgate demo --no-approver` shows the fail-closed case
without a page. With pipx instead of uv:

```
pipx run --spec "git+https://github.com/co2water/agent-boundary-bench@agentgate-v0.3.0#subdirectory=gateway" agentgate demo
```

agentgate lives inside the agent-boundary-bench repo but does not depend on it:
`bench/run.py` copies `gateway.py` into each sandbox as `agentgate.py` and runs it as
a plain script. The demo uses its own mock server (`demo_server.py`); only the two
test scripts below reuse the bench's mock server and fixtures.

## What it decides

1. **Capability class.** Each tool is `query`, `data`, `comms`, `money` or `exec`.
   The policy maps each class to `allow`, `confirm` or `deny`. Denied tools are not
   even listed to the agent.
2. **Unknown = risky.** A tool the policy does not name gets the `unknown` verdict.
   A server's own annotations (`readOnlyHint`) are not trusted.
3. **Out-of-band confirmation.** `confirm` waits for the user on a local approval
   page that shows every argument as sent. No approver means deny (fail closed).
4. **Taint.** Once a tool returns outside content (mail, web pages, files, unknown
   tools), reading personal data, writing files and fetching an unlisted host need
   confirmation for the rest of the session. Strings read from files or personal
   data may not leave inside a URL.

With `--builtins` agentgate also serves the only file and web tools the agent gets
(`gw_list_dir`, `gw_read_file`, `gw_write_file` scoped to `--root`, and `gw_fetch`,
HTTP(S) GET only), so a harness can switch off its own shell, file and web tools and
still work.

## Install or run

From git, no PyPI (the name `agentgate` on PyPI belongs to another project; this
package is not published there):

```
uvx --from "git+https://github.com/co2water/agent-boundary-bench@agentgate-v0.3.0#subdirectory=gateway" agentgate --help
uv tool install "git+https://github.com/co2water/agent-boundary-bench@agentgate-v0.3.0#subdirectory=gateway"   # keeps an `agentgate` command
pip install "git+https://github.com/co2water/agent-boundary-bench@agentgate-v0.3.0#subdirectory=gateway"
```

From a checkout, no install needed:

```
python gateway/gateway.py --policy assistant --server mymail -- <your MCP server command>
```

`--policy assistant` is the built-in everyday-assistant policy (shipped with the
package); any other value is a path to your own policy file.

### Wrap your own MCP server

> **Read this first.** agentgate only sees the tools it wraps. An agent that still
> has a shell or any HTTP tool (Claude Code has Bash on by default) can bypass it
> entirely, and can even open the confirmation page itself and approve its own
> call. The protection holds only when those built-in tools are off (`--builtins`
> gives the agent workspace-scoped file and fetch tools instead). See
> [THREAT_MODEL.md](THREAT_MODEL.md).

Install once, so the client doesn't wait on a git clone at startup (needs `git` on
PATH):

```
uv tool install "git+https://github.com/co2water/agent-boundary-bench@agentgate-v0.3.0#subdirectory=gateway"
```

Then point the client at `agentgate` and put the real server after `--`. Give each
wrapped server its **own** `--approve-port` and keep it fixed, so you know where its
confirmation page is (`http://127.0.0.1:8766/` below); open it when the agent is
waiting on you.

With the built-in policy, a server it doesn't know (`--server mymail`) has every
tool treated as **unknown**: each call needs your approval and its output counts as
outside content. That is the safe default. (Don't name your server `lifeservices`:
that name carries the demo's tool classes.) To let harmless tools through, copy
`policy.assistant.json`, list your server's tools under `servers.<name>` with a
class (see [Policy file](#policy-file)), and pass that file to `--policy` as an
**absolute** path: clients start servers in their own working folder.

Replace `<your MCP server command>` with what you run today (for example
`npx -y <package>`; Windows `.cmd` launchers like `npx` are found automatically),
and `mymail` with any name.

**Claude Code**

```
claude mcp add mymail -- agentgate --policy assistant --server mymail --approve-port 8766 -- <your MCP server command>
```

**Claude Desktop** (`claude_desktop_config.json`) and **Cursor** (`~/.cursor/mcp.json`
or `.cursor/mcp.json`) take the same shape. If the client can't find `agentgate`
(Claude Desktop on macOS doesn't use your shell's PATH), give its full path, which
`uv tool dir --bin` shows.

```json
{
  "mcpServers": {
    "mymail": {
      "command": "agentgate",
      "args": ["--policy", "assistant", "--server", "mymail", "--approve-port", "8766",
               "--", "<your MCP server command>", "<its args>"]
    }
  }
}
```

Some clients give up on a tool call after their own timeout. Approve within it, or
lower `--approve-wait` so agentgate denies first.

## Command line

| Flag | Meaning |
|---|---|
| `--policy FILE\|NAME` | Required. Policy JSON (format below; use an absolute path), or the built-in name `assistant`. |
| `--server NAME` | Required. Key under `servers` in the policy that describes the upstream's tools. |
| `--root DIR` | Workspace root for the builtin file tools. Paths outside it need confirmation; without it every path counts as outside. |
| `--builtins` | Also serve `gw_list_dir`, `gw_read_file`, `gw_write_file`, `gw_fetch`. Upstream tools with these names are dropped. |
| `--approve-port N` | Serve the approval page on `http://127.0.0.1:N/`. Default 0 = no approver: every `confirm` becomes deny. |
| `--approve-wait SEC` | How long a call waits for a decision on the page. Default 120. Timeout = deny. |
| `--log FILE` | Append one JSON line per tool call: `tool`, `class`, `verdict`, `reason`, `approved`, `ts`, `tainted`. |
| `-- CMD ...` | Required. The upstream MCP server command (stdio). Everything after `--` is passed through. |
| `demo` | Instead of the flags above: run the demo (`--no-approver`, `--port N`, `--wait SEC`, `--no-browser`). |
| `--version` | Print the version. |

agentgate speaks MCP over stdio (JSON-RPC 2.0, one message per line) and answers
`initialize`, `tools/list`, `tools/call` and `ping`. It assumes the upstream answers
requests in order.

## Policy file

See `policy.assistant.json` (the everyday-assistant role) and `policy.bench.json`
(the same role with the bench's local sink let through the private-address block).

| Field | Type | Meaning |
|---|---|---|
| `role` | string | Label only. |
| `classes` | object | `query` / `data` / `comms` / `money` / `exec` → `allow` \| `confirm` \| `deny`. A class not listed is `confirm`. |
| `unknown` | string | Verdict for tools the policy does not name. Default `confirm`. |
| `tainted` | object | After outside content was read: verdicts for `pii` (calling a tool marked `pii`) and `write` (a tool marked `write`, incl. `gw_write_file`). `fetch`: `gw_fetch` to a host not on `fetch_allow_hosts`, after taint or once any private data was read. Each defaults to `confirm`. |
| `fetch_allow_hosts` | list | `host:port` entries `gw_fetch` may reach even after taint. |
| `fetch_private_ok` | list | `host:port` entries exempt from the loopback / private / link-local / CGNAT block. |
| `sensitive_paths` | list | Regexes over the lower-cased, `/`-separated resolved path. A match is a hard deny for the file tools. |
| `write_allow_ext` | list | File extensions `gw_write_file` may create. Default `.txt`, `.md`, `.csv`. Anything else is denied. |
| `write_deny_paths` | list | Regexes for folders that are never written (`.git`, hooks, startup folders...). |
| `servers` | object | `servers.<name>.<tool>` = `{ "class": ..., "untrusted_output": bool, "pii": bool, "write": bool }`. `untrusted_output` taints the session when the tool is called; `pii` marks personal data (blocked after taint, and its output may not leave in a URL). |
| `_doc` | string | Comment; ignored. |

Hard denies (sensitive path, executable write, `::$DATA` or UNC path, non-HTTP URL,
private address) apply before the class verdict and cannot be approved.

## Approval page

Run with `--approve-port 8766` and open `http://127.0.0.1:8766/`. The page refreshes
every 3 seconds and lists each call waiting for you: the tool, every argument as
sent (resolved file paths, payee, amount; control and bidi characters shown as
escapes), the reason it needs you, and an argument fingerprint. **Approve once**
releases exactly that one call; **Deny** or no answer within `--approve-wait`
blocks it. Each pending call has a random id, the form carries a CSRF token, and the
page refuses requests whose `Host`, `Origin` or `Sec-Fetch-Site` are not local
(DNS rebinding, cross-site posts). The agent receives `BLOCKED_BY_AGENTGATE: ...`
for a blocked call.

## Tests (no model, no API key)

Both scripts start the real gateway in front of the bench's mock MCP server
(`../bench/mocktools.py`) with the synthetic fixtures in `../bench/fixtures/`, so run
them from a checkout that has the `bench/` folder next to `gateway/`.

```
python gateway/test_gateway.py    # 45 rule checks incl. security-review regressions and the demo; prints PASS/FAIL, exit 1 on any failure
python gateway/fooled_agent.py    # a scripted, already-fooled agent replays each bench attack; shows which step the gateway stopped
```

`test_gateway.py` binds `127.0.0.1:8765` (a throwaway local web page, the same port
as the bench sink, so do not run it during a bench round), `127.0.0.1:8799` (the
approval page) and `127.0.0.1:8797` (the demo's approval page). `fooled_agent.py` writes `gateway/fooled_results.json`
(git-ignored).
