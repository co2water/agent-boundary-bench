# agentgate threat model

agentgate (`gateway/gateway.py`) is a stdio MCP proxy. An agent launches it in
place of an MCP server. It decides every `tools/call` by policy before the call
reaches the upstream server. It is a research prototype, not a hardened
product. For usage, see [README.md](README.md).

## Assets

| Asset | Example in the bench | How it can be harmed |
|---|---|---|
| Money | `pay_merchant` | a payment the user did not approve at that step |
| The user's voice | `send_message` | a message sent in the user's name |
| Personal data | contacts, mail, files under the home folder, `.secrets/` | read and then sent off the device |
| Workspace integrity | files under `--root` | executable files, git hooks or startup entries written for later execution |
| Local network | LAN and loopback services | reached through the fetch tool (SSRF) |
| The approval decision | the approval page | forged, replayed, or shown with misleading content |

## Trust boundaries

```
 untrusted                         trusted                 partly trusted
+-----------------------+      +-------------------+      +------------------+
| model + agent harness | ---> |    agentgate      | ---> | upstream MCP     |
| (may be injected)     | stdio| policy, state,    | stdio| server(s)        |
+-----------------------+      | builtin tools     |      +------------------+
                               +---------+---------+
 untrusted content                       | http://127.0.0.1:<port>
 (mail, web pages, files)                v
 enters through tool results   +-------------------+
                               | human on the      |  trusted
                               | approval page     |
                               +-------------------+
```

- **The model and the harness are untrusted.** The model may have read an
  injection. Tool names and every argument may be chosen by an attacker.
- **Tool results are untrusted content.** Mail, web pages, files and the
  output of unknown tools may carry instructions. Reading them changes what
  the session may do next (taint).
- **Upstream servers are partly trusted.** agentgate forwards calls to them
  and relays their results. It does not trust what they say about their own
  tools: annotations such as `readOnlyHint` are ignored. Only the operator's
  policy assigns a tool's class.
- **The human on the approval page is trusted.** An approval from that page
  is the only way a "confirm" call goes through.
- **The operator, the policy file and the local machine are trusted.** The
  gateway process, the policy, the workspace root and the local user account
  are assumed not to be hostile.

## What agentgate defends

### Capability classes

Each tool has a class: `query`, `data`, `comms`, `money` or `exec`. The policy
role maps each class to `allow`, `confirm` or `deny`. In
`policy.assistant.json`, query and data are allowed, comms and money need
confirmation, and exec is denied. A tool in a denied class is removed from
`tools/list`, so the agent does not learn it exists. A tool that needs
confirmation gets a note in its description.

### Unknown means risky

A tool that the policy does not name gets the `unknown` verdict (`confirm` in
the assistant role). It is also treated as returning untrusted content. A tool
name that is not in the upstream list or the builtin list is denied. An
upstream tool may not shadow a builtin tool name.

### Out-of-band approval

A "confirm" call waits for the user on a local page
(`--approve-port`, bound to 127.0.0.1).

- The page shows every argument as sent, sorted by key, with resolved file
  paths, not the model's description of the call. Control and format
  characters (newlines, bidi overrides) are shown as escapes, so an argument
  cannot draw a fake line. A long `content` argument is cut at 2,000
  characters, with the full length shown.
- Each pending call has a random id. The page shows a fingerprint of the
  SHA-256 over the tool name and arguments.
- A POST must carry the page's CSRF token. Requests are rejected if the
  `Host` header is not the local address and port (a DNS rebinding defense),
  if `Origin` is set to anything else, or if `Sec-Fetch-Site` is present and
  is not `same-origin` or `none`.
- The page sends `X-Frame-Options: DENY` and `frame-ancestors 'none'`.
- An approval releases exactly one call. The pending entry is removed after
  the decision or the timeout.
- Fail closed: with no approval port, or after `--approve-wait` seconds
  (default 120), the call is denied.

### Taint

Once a tool marked `untrusted_output` runs (mail, file reads, directory
listings, web fetch, unknown tools), the session is tainted for the rest of
its life. The flag is set before the call, so an error message that carries
outside text also taints. After taint:

- reads of tools marked `pii` (for example `contacts_list`) need confirmation;
- writes need confirmation;
- a fetch to a host that is not on `fetch_allow_hosts` needs confirmation.
  While the session has only read web pages, hosts it already visited stay
  allowed, so ordinary browsing keeps working.

### secret_seen

Once any non-query tool or any `pii` tool has run (files, mail, contacts,
unknown tools), every fetch to a host off the allowlist needs confirmation.
This includes hosts the session already visited. A token check cannot catch
an encoded copy sent back to a page's own host, so the visited-host exemption
ends when private data has been read.

### URL token leak check

After `gw_read_file` or a `pii` tool, agentgate records tokens from the
result: runs of at least 6 letters or digits that contain a digit. Before a
fetch, the URL is URL-decoded, lowercased and stripped to letters and digits.
If it contains a recorded token, the fetch needs confirmation. This catches a
value sent as-is, split across path segments, or with changed punctuation.

### Builtin tools (`--builtins`)

With `--builtins`, agentgate serves the only file and web tools the agent
gets, so a harness can turn off its own shell, file and web tools.

- `gw_list_dir`, `gw_read_file`, `gw_write_file` resolve paths with
  `realpath` against `--root`. A path outside the workspace needs
  confirmation.
- Paths matching `sensitive_paths` (`.secrets`, `.ssh`, `.aws`, key and
  certificate files, password stores and similar) are denied. This hard deny
  comes before any confirm verdict, so a tainted write to a sensitive path is
  denied, not sent for approval.
- UNC paths, device paths and any `:` after the drive letter (for example an
  NTFS alternate data stream such as `::$DATA`) are denied before the path is
  resolved. On Windows, resolving a UNC path can open an SMB connection on its
  own.
- Writes are limited to an extension allowlist (`write_allow_ext`: text,
  Markdown, CSV and similar document types). Scripts, executables, links and
  extensionless files are denied. `write_deny_paths` also blocks `.git`,
  `.github`, editor config folders, `hooks` folders and the Windows Startup
  folder. The parent folder must be inside the workspace.
- `gw_fetch` allows only `http` and `https`, GET only. There are no file,
  FTP or data handlers, and ambient proxy settings are ignored. The resolved
  addresses must not be private, loopback, link-local, reserved, multicast or
  CGNAT (100.64.0.0/10). Each redirect target is checked again. Script and
  style blocks are stripped, and responses are size-capped.

### Robustness

Request lines over 4 MiB are rejected. Non-object and batch JSON requests get
an error instead of crashing the gateway. Every decision is written to the
`--log` file for audit.

## What agentgate does not defend

- **Traffic that does not pass through it.** agentgate sees only the MCP
  calls routed to it. A harness's built-in shell, file or web tool that is
  left on bypasses it completely. R2's `gate` configuration shows this: secret
  reads, writes outside the workspace and uploads still happened through the
  agents' own tools. Narrowing the built-ins is part of the defense.
- **DNS rebinding (time of check vs. time of use).** The private-address check
  resolves the host, then the HTTP client resolves it again to connect. A
  TTL-0 rebind can make the second lookup return a LAN address.
- **Approved but mistaken actions.** If the user approves a harmful call, it
  runs. The page shows the real arguments, but it cannot judge intent.
  Frequent prompts can also lead to approval fatigue.
- **Malicious upstream servers.** Ignoring annotations is the only upstream
  defense. A server can still do harm inside its own implementation (a tool
  classed as `query` that sends data), return misleading results, or
  misbehave on the stdio channel. The upstream is assumed to answer requests
  in order. A wrong class in the operator's policy is also not detected.
- **Encoded exfiltration to allowlisted hosts.** The token check matches
  literal values. A host on `fetch_allow_hosts` is exempt from the taint and
  secret_seen fetch rules, so encoded data sent to such a host is not caught.
- **What the model says.** agentgate does not filter the agent's reply. Data
  the policy allows the agent to read (for example a file inside the
  workspace) can be printed back to the user or reach the model provider.
- **Other local processes.** Any process on the same machine can open the
  approval page, read the CSRF token and post an approval. The local machine
  and user account are inside the trust boundary.
- **Coverage of the path lists.** `sensitive_paths` and `write_deny_paths` are
  pattern lists. A secret stored under an unlisted name is treated as an
  ordinary file.
- **Platforms other than Windows.** The bench and the path checks were tested
  on Windows only.

## Review history

Before R2, the gateway went through four rounds of adversarial security
review. The fix notes at the top of `gateway.py` and the `R*`-labelled
regression checks in `test_gateway.py` record the findings.

1. **First review (2026-10-03).** Fixed: `file://` URLs in fetch; cross-site
   approval (CSRF token, Origin and Host checks); exfiltration through paths
   and hosts (URL token check, UNC paths); taint set after the call instead of
   before; taint skipped when the upstream returned an error; trusted
   `readOnlyHint` annotations; arguments hidden from the approval page;
   executable and git-hook writes; a crash on non-object JSON; loopback fetch
   allowed by default; a tainted write to a sensitive path going to
   confirmation instead of a hard deny.
2. **Re-review.** Fixed: `::$DATA` writes (replaced the write denylist with an
   extension allowlist); link blocking that was too eager (only secret-bearing
   reads now feed the token check); unescaped keys on the approval page; the
   CGNAT range missing from the private-address check; outbound fetch allowed
   after a `pii` read.
3. **Third round.** Fixed: a host the session had already visited was still
   exempt after private data had been read. This added the secret_seen rule.
4. **Fourth round.** The code does not list its fixes separately. After it,
   one known limitation remained open: the DNS rebinding gap described above.

`gateway/test_gateway.py` runs 39 model-free checks, including these
regressions. `gateway/fooled_agent.py` replays an already-fooled agent
against the gateway; in the R2 lockdown configuration it blocked the harmful
step in 10 of 10 cases.

To report a vulnerability, see [SECURITY.md](../SECURITY.md).
