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
  characters, with the full length and the SHA-256 of the full text shown.
  Runs of 4 or more spaces are shown as `␠×N` and long lines wrap. The tool
  name is escaped too.
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
- Files with more than one hard link are denied for read and write, and every
  hard deny is re-checked at execution time against the path that was judged
  (see "Approval-dialog attacks" below).
- `gw_fetch` allows only `http` and `https`, GET only. There are no file,
  FTP or data handlers, and ambient proxy settings are ignored. The resolved
  addresses must not be private, loopback, link-local, reserved, multicast or
  CGNAT (100.64.0.0/10). Each redirect target is checked again. Script and
  style blocks are stripped, and responses are size-capped.

### Robustness

Request lines over 4 MiB are rejected. Non-object and batch JSON requests get
an error instead of crashing the gateway. Every decision is written to the
`--log` file, hash-chained (`prev`, `hash`, `args_sha256`, no raw arguments);
`agentgate verify-log` checks it. The chain is tamper-evident, not
tamper-proof: there is no key, so whoever can rewrite the file can rebuild the
chain, and lines cut off the end leave no trace.

### Tool metadata

Before the agent sees an upstream tool, agentgate strips invisible characters
(zero-width, bidi, the Unicode tag block, control characters, stray variation
selectors) from its description and schema, marks the description
`[agentgate: hidden characters removed]` when it did, and caps descriptions at
2,000 characters. Tools whose names do not match `[A-Za-z0-9_.-]{1,128}` are
not listed.

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
- **Malicious upstream servers.** agentgate ignores annotations, strips
  invisible characters from tool metadata, caps descriptions, and drops tools
  with non-conforming names. Visible instructions in a description, tool
  results, and tool definitions that change between sessions (rug pulls) are
  not handled. A server can still do harm inside its own implementation (a tool
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
  and user account are inside the trust boundary. That includes the agent itself
  when it still has a shell or an HTTP tool of its own (Claude Code's Bash, for
  example): it can approve its own call. Turn those tools off and use `--builtins`.
- **Coverage of the path lists.** `sensitive_paths` and `write_deny_paths` are
  pattern lists. A secret stored under an unlisted name is treated as an
  ordinary file.
- **Platforms other than Windows.** The bench and the path checks were tested
  on Windows only.

## Approval-dialog attacks

An approval protects the user only if the page shows the operation that will
run. Four publicised attack classes target that link. For each one, this
section gives the attack, what agentgate 0.3.2 and later do, what remains, and the
test that covers it. The tests are in `tests/test_approval_integrity.py`. They
start the real gateway with `--builtins` in front of `gateway/demo_server.py`
and drive the approval page over HTTP.

### Symlinked targets ("GhostApproval")

**Attack.** In [GhostApproval](https://www.wiz.io/blog/ghostapproval-a-trust-boundary-gap-in-ai-coding-assistants)
(Wiz Research, July 2026), a repository carries a symlink with a harmless
name, such as a config file, that points at a sensitive file such as
`~/.ssh/authorized_keys`. The approval prompt names the link, and the write
lands on the target.

**What agentgate does.** `decide()` resolves the path with `_resolve()`
(`os.path.realpath` against `--root`) before any check. The sensitive-path
deny, the write extension allowlist and the workspace check all see the real
target. `summary()` replaces the `path` argument with the same resolved path,
so the page shows the target, not the link name. A link to a sensitive target
is denied and never reaches the page. On Windows, `realpath` also resolves
directory junctions, which any user can create. Before the write,
`run_builtin()` resolves the path again and refuses the write if both the file
and its parent folder are outside the workspace. So even an approved write
through a link to an outside folder fails.

- *Hard links.* `realpath` cannot see them, so since 0.3.2 `gw_read_file` and
  `gw_write_file` hard-deny a regular file with more than one hard link
  (`st_nlink > 1`), at decision time and again at execution. `gw_list_dir` is
  not affected. (This also denies legitimate hard-linked files in a workspace,
  such as a pnpm store.)
- *Check-to-use race.* Before a builtin file tool runs, agentgate resolves the
  path again, repeats every hard deny (sensitive path, extension allowlist,
  hard link) and refuses the call unless the path still resolves to the one
  that was judged and shown. It opens with `O_NOFOLLOW` (POSIX), creates new
  files with `O_EXCL`, and checks with `fstat` and `samestat` that the handle is
  a regular file with no more than one link and is the same file found at the path before
  and after the open. A write truncates only after these checks.

**What remains.**

- *A narrow race at file creation.* A link swapped in at a new file's name or
  its parent folder in the instant between the re-check and the open can
  create an empty file at the link's target; the write is then refused and
  nothing is written into it. A full fix would need `openat`-style traversal.
- *Upstream file tools.* agentgate resolves paths only for its builtin tools.
  For an upstream server's file tool, the page shows the path argument as
  sent, and the upstream follows links on its own.

**Tests.** `test_symlink_write_shows_real_target`,
`test_symlink_to_sensitive_target_denied` (both skipped where symlinks cannot
be created, for example Windows without developer mode) and
`test_junction_write_shows_real_target` (Windows only). In
`gateway/test_gateway.py`: the "hard link: ..." checks, "race: a hard link
placed at the path during the wait ..." and "race: a path that resolves
elsewhere after the approval ...".

### Approve A, run B ("Loopjacking")

**Attack.** The [Loopjacking](https://arxiv.org/abs/2609.21081) preprint
(September 2026) describes a human who approves operation A while the system
applies the approval to a different operation B. B is either left out of the
dialog or swapped into mutable pending state after the approval.

**What agentgate does.** `ask_user()` gives each held call a random id
(`secrets.token_urlsafe(16)`) and a SHA-256 over `[name, args]`. The page
shows the first 12 hex digits of that hash. The pending entry holds only
strings (tool name, summary, reason, digest) and the decision. It holds no
reference to the arguments. The page thread can only set the decision of the
entry whose id the POST names (`do_POST`). The call that runs is the same
in-process `args` object that `decide()` checked and `summary()` rendered, and
no other thread holds it. `serve()` handles one request at a time. At most one
call per gateway is held, and the next call is not read until the held one is
decided. The entry is removed after the decision or the timeout, so a stale
approval matches nothing. Each gateway process has its own CSRF token. With
several wrapped servers, a POST to one gateway cannot decide another
gateway's call.

**What remains.** The digest covers the raw arguments and is not checked
again before execution. That is safe only because the arguments never leave
the process. For builtin file tools, a change in the file system between
approval and execution is caught by the execution-time re-check (see above),
except the narrow file-creation race.
Since 0.3.2 the `--log` entry records the same digest as `args_sha256`, so a
logged approval can be matched to what the user saw. An upstream
server receives the approved arguments as sent, and what it does with them is
its own (see "Malicious upstream servers").

**Tests.** `test_approve_releases_only_that_call`,
`test_post_with_unknown_id_changes_nothing`, `test_csrf_and_id_are_per_gateway`.

### Forged dialog content ("Lies-in-the-Loop")

**Attack.** In [Lies-in-the-Loop](https://checkmarx.com/zero-post/bypassing-ai-agent-defenses-with-lies-in-the-loop/)
(Checkmarx Zero, September 2025), the agent is led to pad or format a
dangerous request so that the confirmation dialog looks harmless. Long text
pushes the real command out of view; formatting tricks such as control
characters are a natural extension.

**What agentgate does.** The page text comes from `summary()`, not from the
model's description of the call. Each key and value is JSON-encoded, so
newlines and other C0 control characters become `\n`, `\u001b` and so on.
`_visible()` then shows control and format characters (bidi overrides,
zero-width characters) and line and paragraph separators as `\uXXXX`. Every
argument is shown, sorted by key, one per line. `do_GET` HTML-escapes the tool
name, the summary and the reason, so HTML or Markdown in an argument is shown
as text. Since 0.3.2: upstream tool names must match `[A-Za-z0-9_.-]{1,128}`
or the tool is not listed, and the summary's first line and the page's tool
name also go through `_visible()`, so a name cannot draw a fake line; runs of 4
or more spaces show as `␠×N` and the summary block wraps, so padding cannot push
text out of view.

**What remains.**

- *Truncated content.* After the 2,000-character cut, the rest of `content` is
  not shown, but the page shows the SHA-256 of the full text and the total
  length.
- *Look-alike characters.* Homoglyphs (for example Cyrillic letters in a Latin
  name) and combining marks are shown as they are.

**Tests.** `test_page_escapes_control_and_markup`.

### Approval fatigue

**Attack.** A confirmation step works only while the human checks each
request. Repeated prompts can turn approval into a reflex. The DEF CON 34 AI
Village poster [Stop Pressing 1: Measuring Human Rubber-Stamping in Agent
Oversight](https://aivillage.org/posters/stop-pressing-1-measuring-human-rubber-stamping-in-agent-oversight/)
proposes a way to measure this.

**What agentgate does.** Each approval covers one call ("Approve once").
There is no "always allow" button that turns the check off for good. The page
opens at most once a minute (`_tell_user()`).

**What remains.** This is a known limitation. In `policy.assistant.json`,
`unknown` is `confirm`. If the policy does not list a server's tools under
`servers`, every call to that server waits for approval. Unknown tools also
count as untrusted output, so after the first call every write needs approval
too. This is safe by default, but it makes many prompts and can train the user
to approve without reading. Classify each server's tools in the policy
(`query` and `data` allowed, `comms` and `money` confirmed, `exec` denied), so
that only consequential calls reach the page. agentgate does not measure or
limit how often it asks.

**Tests.** None: this is a human factor. The policy behaviour is covered by
`gateway/test_gateway.py`.

## Review history

Before R2, the gateway went through four rounds of adversarial security
review by separate AI reviewer agents with a fresh context (not an independent
human audit). The fix notes at the top of `gateway.py` and the `R*`-labelled
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

`gateway/test_gateway.py` runs 75 model-free checks, including these
regressions. `gateway/fooled_agent.py` replays an already-fooled agent
against the gateway; in the R2 lockdown configuration it blocked the harmful
step in 10 of 10 cases.

To report a vulnerability, see [SECURITY.md](../SECURITY.md).
