# agentgate

A boundary gateway between an AI agent and its MCP servers. The agent launches
agentgate instead of the MCP server; agentgate starts the real server behind it and
decides every `tools/call` from a policy file — not from what the model says.
Standard library only, one file (`gateway.py`), Python 3.11+.

**Status: prototype (0.2.0).** It went through four rounds of adversarial review
(fix notes at the top of `gateway.py`). One known limit remains: the private-address
check resolves a host, then urllib resolves it again to connect, so a TTL-0 DNS
rebind can still reach a LAN address. Not a hardened product.

agentgate lives inside the agent-boundary-bench repo but does not depend on it:
`bench/run.py` copies `gateway.py` into each sandbox as `agentgate.py` and runs it as
a plain script. Only the two test scripts below reuse the bench's mock server and
fixtures.

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

No install needed:

```
python gateway.py --policy policy.assistant.json --server lifeservices -- python my_mcp_server.py
```

Or install the console script from this folder (no dependencies):

```
pip install ./gateway        # from the repo root; provides the `agentgate` command
agentgate --policy policy.assistant.json --server lifeservices -- python my_mcp_server.py
```

The policy files are not part of the package; pass a path with `--policy`.

### Use from an MCP client

Point the client's server entry at agentgate and put the real server after `--`.
Example (any client that takes `command` + `args`):

```json
{ "command": "python",
  "args": ["/path/to/gateway.py", "--policy", "/path/to/policy.assistant.json",
           "--server", "lifeservices", "--approve-port", "8766",
           "--log", "/path/to/gateway.jsonl",
           "--", "python", "/path/to/my_mcp_server.py"] }
```

## Command line

| Flag | Meaning |
|---|---|
| `--policy FILE` | Required. Policy JSON (format below). |
| `--server NAME` | Required. Key under `servers` in the policy that describes the upstream's tools. |
| `--root DIR` | Workspace root for the builtin file tools. Paths outside it need confirmation; without it every path counts as outside. |
| `--builtins` | Also serve `gw_list_dir`, `gw_read_file`, `gw_write_file`, `gw_fetch`. Upstream tools with these names are dropped. |
| `--approve-port N` | Serve the approval page on `http://127.0.0.1:N/`. Default 0 = no approver: every `confirm` becomes deny. |
| `--approve-wait SEC` | How long a call waits for a decision on the page. Default 120. Timeout = deny. |
| `--log FILE` | Append one JSON line per tool call: `tool`, `class`, `verdict`, `reason`, `approved`, `ts`, `tainted`. |
| `-- CMD ...` | Required. The upstream MCP server command (stdio). Everything after `--` is passed through. |

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
python gateway/test_gateway.py    # rule checks incl. security-review regressions; prints PASS/FAIL, exit 1 on any failure
python gateway/fooled_agent.py    # a scripted, already-fooled agent replays each bench attack; shows which step the gateway stopped
```

`test_gateway.py` binds `127.0.0.1:8765` (a throwaway local web page, the same port
as the bench sink, so do not run it during a bench round) and `127.0.0.1:8799` (the
approval page). `fooled_agent.py` writes `gateway/fooled_results.json`
(git-ignored).
