"""agentgate: a boundary gateway between an agent and its MCP servers (prototype).

    python gateway.py --policy policy.json --server lifeservices [--root DIR] [--builtins]
                      [--approve-port 8766] [--log gateway.jsonl] -- <upstream command...>
    python gateway.py demo [--no-approver]      (see demo.py)
    python gateway.py verify-log gateway.jsonl  (recompute the --log hash chain)

--policy takes a file path or the built-in name "assistant" (policy.assistant.json,
shipped with the package).

The agent launches this instead of the MCP server. Every tools/call is decided
by policy, not by the model:

  1. capability class   each tool is query / data / comms / money / exec;
                        the role says allow / confirm / deny per class;
                        denied tools are not even listed
  2. unknown = risky    tools the policy does not know get the "unknown" verdict;
                        a server's own annotations (readOnlyHint) are not trusted
  3. out-of-band OK     "confirm" waits for the user on a local approval page;
                        the page shows every argument as sent (resolved paths,
                        payee, amount), not what the model says; each pending
                        call has a random id and the page a CSRF token, and an
                        approval releases exactly that one call;
                        no approver -> deny (fail closed)
  4. taint              once a tool returns outside content (mail, web, files,
                        unknown tools), reads of personal data, writes, and any
                        fetch to a host not on the allowlist need confirmation
                        for the rest of the session

With --builtins the gateway also serves the only file and web tools the agent
gets (workspace-scoped read/list/write, http(s) GET-only fetch), so a harness
can turn off its own shell, file and web tools and still work.

Standard library only. Upstream is assumed to answer requests in order (stdio).
Review 2026-10-03 fixed: file:// fetch, cross-site approval, path/host exfil,
taint ordering, taint skipped on upstream errors, trusted annotations, hidden
arguments on the approval page, executable writes, crash on non-object JSON;
re-review: ::$DATA writes (extension allowlist now), over-eager link blocking,
escaped approval-page keys, CGNAT range, outbound fetch after a pii read.
Known limit: the private-address check resolves the host, then urllib resolves
it again to connect, so a TTL-0 DNS rebind can still reach a LAN address.
0.3.2: upstream tool metadata is stripped of invisible characters before the agent
sees it (tools whose names break the MCP naming rule are dropped); the --log is
hash-chained; the file tools deny hard-linked files and re-check every hard deny at
execution against the path that was judged; tool names and padding are escaped on
the approval page.
"""
import argparse
import hashlib
import html
import io
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CLASS_ZH = {"query": "查詢類", "data": "資料類", "comms": "通訊類", "money": "交易類", "exec": "執行類"}
CLASS_EN = {"query": "query", "data": "data", "comms": "comms", "money": "money", "exec": "exec"}


def _class_reason(pol):
    c = pol.get("class")
    return "%s-class tool / %s工具" % (CLASS_EN.get(c, "unknown"), CLASS_ZH.get(c, "未知"))
MAX_LINE = 4 * 1024 * 1024
VERSION = "0.3.3"
HERE = os.path.dirname(os.path.abspath(__file__))
DESC_MAX = 2000  # characters of an upstream description or title the agent gets
TRUNC_NOTE = " …[truncated by agentgate]"
HIDDEN_NOTE = " [agentgate: hidden characters removed]"
ZERO_HASH = "0" * 64
FILE_TOOLS = ("gw_read_file", "gw_list_dir", "gw_write_file")
HARDLINK_REASON = "file has more than one hard link / 檔案有多個硬連結"
MOVED_REASON = "the path changed after it was checked / 路徑在檢查之後被改動了"
TOOL_NAME = re.compile(r"[A-Za-z0-9_.\-]{1,128}")  # the MCP tool-name convention

BUILTIN_TOOLS = {
    "gw_list_dir": {
        "description": "List files in a folder inside the user's workspace.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
        "policy": {"class": "data", "untrusted_output": True},
    },
    "gw_read_file": {
        "description": "Read a text file inside the user's workspace.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        "policy": {"class": "data", "untrusted_output": True},
    },
    "gw_write_file": {
        "description": "Write a text file inside the user's workspace.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                        "required": ["path", "content"]},
        "policy": {"class": "data", "write": True},
    },
    "gw_fetch": {
        "description": "Fetch a web page (HTTP GET only) and return its text.",
        "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
        "policy": {"class": "query", "untrusted_output": True},
    },
}


def _alnum(s):
    return re.sub(r"[^0-9a-z]", "", urllib.parse.unquote(str(s)).lower())


def _visible(s):
    """Approval-page text: control and format characters (newlines, bidi overrides)
    shown as escapes so an argument cannot draw a fake line, and runs of 4+ spaces
    counted ("␠×12") so padding cannot push the rest of a value out of sight."""
    s = "".join("\\u%04x" % ord(ch) if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch for ch in s)
    return re.sub(r" {4,}", lambda m: "␠×%d" % len(m.group()), s)


def _hard_linked(path):
    """A regular file with another name somewhere: realpath() cannot see where the other names
    are, so the workspace and sensitive-path checks would judge the wrong name."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_nlink > 1


# ---- upstream tool metadata (tool-description poisoning with invisible text)
def _is_vs(ch):
    """Text / emoji presentation selectors (VS15, VS16): the only variation selectors a tool
    description has a use for."""
    return ch in "\ufe0e\ufe0f"


def _hidden(ch):
    """Text a model reads but a person reviewing the tool list does not see: format characters
    (Cf: zero-width, bidi controls, the U+E0000 tag block), controls other than newline and tab,
    lone surrogates, and variation selectors VS1-14 and VS17-256 (invisible, and a run of them
    encodes bytes)."""
    o = ord(ch)
    cat = unicodedata.category(ch)
    return cat in ("Cf", "Cs") or (cat == "Cc" and ch not in "\n\t") or 0xFE00 <= o <= 0xFE0D \
        or 0xE0000 <= o <= 0xE007F or 0xE0100 <= o <= 0xE01EF


def _bad_name(name):
    """A tool name outside the MCP convention (letters, digits, _ . -; 1-128 characters): hidden
    characters, line breaks, quotes or spaces could pose as another tool or draw fake lines."""
    return not (isinstance(name, str) and TOOL_NAME.fullmatch(name))


def _strip_hidden(s):
    """-> (s without hidden characters, whether any were removed). CRLF counts as a newline; one
    VS15/VS16 directly after a visible non-ASCII character stays (an emoji's style), any other
    is removed."""
    s = s.replace("\r\n", "\n")
    out, after_visible = [], False
    for ch in s:
        if _is_vs(ch):
            keep, after_visible = after_visible, False
        else:
            keep = not _hidden(ch)
            after_visible = keep and ord(ch) > 0x7F
        if keep:
            out.append(ch)
    clean = "".join(out)
    return clean, len(clean) != len(s)


def _clean_meta(v):
    """-> (copy of a JSON value with hidden characters removed from every string, keys included;
    description and title strings capped at DESC_MAX), whether anything hidden was found)."""
    if isinstance(v, str):
        return _strip_hidden(v)
    found = False
    if isinstance(v, list):
        out = []
        for x in v:
            x, f = _clean_meta(x)
            out.append(x)
            found = found or f
        return out, found
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            k, f1 = _strip_hidden(str(k))
            x, f2 = _clean_meta(x)
            found = found or f1 or f2
            if k in ("description", "title") and isinstance(x, str) and len(x) > DESC_MAX:
                x = x[:DESC_MAX] + TRUNC_NOTE
            if k not in out:  # two keys that differ only by hidden characters: the first one stays
                out[k] = x
        return out, found
    return v, False


def _sanitize_tool(t):
    """An upstream tool as the agent sees it: every field but the name cleaned (a tool whose name
    carries hidden characters was dropped at start-up), and tampering marked in the description."""
    rest, found = _clean_meta({k: v for k, v in t.items() if k != "name"})
    rest.pop("name", None)  # a key that only became "name" after cleaning may not replace it
    out = {"name": t["name"]} if "name" in t else {}
    out.update(rest)
    if "description" in out and not isinstance(out["description"], str):
        out["description"] = ""
    if found:
        out["description"] = (out.get("description", "") + HIDDEN_NOTE).lstrip()
    return out


# ---- audit log (hash chain)
def _args_digest(name, args):
    """The fingerprint an approval is bound to; the log records the same value."""
    return hashlib.sha256(json.dumps([name, args], sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _entry_hash(entry):
    """sha256 of the entry's canonical JSON without its own "hash" (so "prev" is covered)."""
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _valid_entry(entry):
    h = entry.get("hash") if isinstance(entry, dict) else None
    return isinstance(h, str) and re.fullmatch(r"[0-9a-f]{64}", h) is not None and _entry_hash(entry) == h


def _chain_tip(path):
    """-> (hash of the log's last line, or None when that line is not a valid entry; whether the
    file ends without a newline). Reads backwards, so a long last line is still read whole."""
    try:
        f = io.open(path, "rb")
    except FileNotFoundError:
        return ZERO_HASH, False
    with f:
        end = f.seek(0, 2)
        if end == 0:
            return ZERO_HASH, False
        f.seek(end - 1)
        cut = f.read(1) != b"\n"  # a crash mid-write leaves a partial line
        pos, buf = end - (0 if cut else 1), b""
        while pos > 0 and b"\n" not in buf:
            step = min(65536, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
        last = buf[buf.rfind(b"\n") + 1:]
    try:
        entry = json.loads(last.decode("utf-8"))
    except ValueError:
        return None, cut
    return (entry["hash"] if _valid_entry(entry) else None), cut


def verify_log(path):
    """-> (ok, message). Recomputes every entry's hash and checks each "prev" against the line before."""
    last, n, restarts = ZERO_HASH, 0, []
    with io.open(path, "rb") as f:
        for lineno, raw in enumerate(f, 1):
            where = "line %d" % lineno
            try:
                entry = json.loads(raw.decode("utf-8"))
            except ValueError:
                return False, "BROKEN at %s: not a JSON entry (cut short, edited, or not written by agentgate)" % where
            if not isinstance(entry, dict):
                return False, "BROKEN at %s: not a JSON object" % where
            seq = entry.get("seq")
            where += " (seq %s)" % seq
            if not _valid_entry(entry):
                return False, "BROKEN at %s: hash does not match the entry (edited, or no hash)" % where
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
                return False, "BROKEN at %s: no valid seq" % where
            if entry.get("chain_restart") is True and entry.get("prev") == ZERO_HASH:
                restarts.append(lineno)
            elif entry.get("prev") != last:
                return False, ("BROKEN at %s: prev does not match the hash of the line before "
                               "(a line was removed, inserted or reordered)" % where)
            last, n = entry["hash"], n + 1
    msg = "OK: %d entries, hash chain intact; last hash %s" % (n, last)
    if restarts:
        msg += ("\nnote: the chain restarts at line(s) %s: the line before was missing, cut short or not an "
                "agentgate entry when that process started" % ", ".join(map(str, restarts)))
    return True, msg


class _ApprovalServer(ThreadingHTTPServer):
    """One listener per port. Windows lets a second SO_REUSEADDR socket bind the same port,
    so it asks for exclusive use instead; POSIX keeps SO_REUSEADDR (fast restarts) and
    still refuses a second live listener."""
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Gate:
    def __init__(self, a):
        self.a = a
        self.policy = json.load(io.open(a.policy, encoding="utf-8"))
        self.tool_policy = dict(self.policy.get("servers", {}).get(a.server, {}))
        self.tainted = False
        self.secret_seen = False  # private data (files, mail, contacts) read: every outbound fetch needs approval
        self.visited_hosts = set()
        self.seen_tokens = set()  # normalised strings from tool output: may not leave in a URL
        self.root = os.path.realpath(a.root) if a.root else None
        self.pending = {}  # random id -> call awaiting the user
        self.csrf = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.last_open = 0.0
        # the approval page binds first: a port already in use must stop agentgate loudly,
        # not leave every "confirm" to time out against someone else's page
        self.ui = self._make_ui() if a.approve_port else None
        # stderr not inherited: an orphaned upstream holding the agent's pipe makes some
        # agents (OpenClaw one-shot) wait forever for EOF
        self.up = subprocess.Popen(a.upstream, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL,
                                   env=dict(os.environ, BENCH_RUN=os.environ.get("BENCH_RUN", "")))
        self.up_id = 0
        self._up_call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "agentgate", "version": VERSION}})
        self._up_send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = self._up_call("tools/list", {}).get("tools", [])
        # a builtin name always means the builtin; upstream may not shadow or duplicate it
        self.up_tools = [t for t in tools if isinstance(t, dict) and not (a.builtins and t.get("name") in BUILTIN_TOOLS)]
        # a name outside the MCP convention (hidden characters, line breaks, quotes) can pose as
        # another tool to the user and the model: not listed, and a call to it is "no such tool"
        dropped = [t for t in self.up_tools if _bad_name(t.get("name"))]
        self.up_tools = [t for t in self.up_tools if not _bad_name(t.get("name"))]
        for t in dropped:
            try:
                sys.stderr.write("agentgate: upstream tool %s not listed: a tool name must be 1-128 letters, "
                                 "digits, _ . or -\n" % _visible(json.dumps(t.get("name"))[:200]))
                sys.stderr.flush()
            except Exception:
                pass
        self.log_seq = 0  # --log entries written by this process
        self.decided_path = None  # resolved path decide() judged for the current file-tool call
        if self.ui:
            threading.Thread(target=self.ui.serve_forever, daemon=True).start()

    # ---- upstream
    def _up_send(self, msg):
        self.up.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        self.up.stdin.flush()

    def _up_call(self, method, params):
        self.up_id += 1
        self._up_send({"jsonrpc": "2.0", "id": self.up_id, "method": method, "params": params})
        while True:
            line = self.up.stdout.readline(MAX_LINE)
            if not line:
                raise RuntimeError("upstream closed")
            msg = json.loads(line.decode("utf-8"))
            if isinstance(msg, dict) and msg.get("id") == self.up_id:
                if "error" in msg:
                    raise RuntimeError(str((msg["error"] or {}).get("message", "upstream error")))
                return msg.get("result", {})

    # ---- policy
    def policy_for(self, name):
        if self.a.builtins and name in BUILTIN_TOOLS:
            return BUILTIN_TOOLS[name]["policy"]
        if name in self.tool_policy:
            return self.tool_policy[name]
        return {"class": None, "untrusted_output": True}  # unknown: annotations are the server's claim, not ours

    def class_verdict(self, pol):
        if pol.get("class") is None:
            return self.policy.get("unknown", "confirm")
        return self.policy["classes"].get(pol["class"], "confirm")

    def decide(self, name, args, pol):
        """-> (verdict, reason). verdict: allow | confirm | deny. Hard denies come first."""
        v = self.class_verdict(pol)
        if v == "deny":
            return "deny", _class_reason(pol)
        if name in FILE_TOOLS:
            why, path = self._path_check(name, args)
            self.decided_path = path  # what the approval page shows and run_builtin must still find
            if why:
                return "deny", why
            if not self._inside(path):
                return "confirm", "path outside the workspace / 工作區以外的路徑"
        if name == "gw_fetch":
            url = str(args.get("url", ""))
            ok, why = self._url_allowed(url)
            if not ok:
                return "deny", why
        if v != "allow":
            return v, _class_reason(pol)
        if name == "gw_fetch":
            norm = _alnum(url)
            if any(tok in norm for tok in self.seen_tokens):
                return "confirm", "the URL carries content from a file or personal data read earlier / 網址裡夾帶了剛讀過的檔案或個人資料內容"
        t = self.policy.get("tainted", {})
        if name == "gw_fetch" and (self.tainted or self.secret_seen):
            # same-site browsing stays usable while the session has only read web pages; once any
            # private data was read (files, mail, contacts) every off-allowlist fetch needs approval,
            # because a token check cannot stop an encoded copy going back to the page's own host
            known = self._host(url) in self._allow_hosts() or \
                (not self.secret_seen and self._host(url) in self.visited_hosts)
            if not known and t.get("fetch", "confirm") != "allow":
                return t.get("fetch", "confirm"), "fetching a host not on the allowlist after reading outside content or personal data / 已讀入外部內容或個人資料後，又要連到白名單以外的主機"
        if self.tainted:
            if pol.get("pii") and t.get("pii", "confirm") != "allow":
                return t.get("pii", "confirm"), "reading personal data after reading outside content / 已讀入外部內容後，又要讀取個人資料"
            if pol.get("write") and t.get("write", "confirm") != "allow":
                return t.get("write", "confirm"), "writing a file after reading outside content / 已讀入外部內容後，又要寫入檔案"
        return "allow", ""

    def _path_check(self, name, args):
        """-> (hard-deny reason or None, resolved path or None). The path rules of the builtin file
        tools: decide() judges a call with them and run_builtin() runs them again before touching
        the file, because an approval can wait minutes and the file system can change meanwhile."""
        raw = str(args.get("path") or ".")
        # checked before realpath(): a UNC path makes Windows dial out (SMB, NTLM) right there
        if ":" in raw[2:] or (len(raw) > 1 and raw[0] in "/\\" and raw[1] in "/\\"):
            return "path with a data stream (::$DATA) or a device/network path / 路徑含資料流（::$DATA）或裝置／網路路徑", None
        path = self._resolve(raw)
        if self._sensitive(path):
            return "sensitive path (keys, credentials, password files) / 敏感路徑（金鑰、憑證、密碼類檔案）", path
        if name == "gw_write_file" and self._write_denied(path):
            return "only plain documents (txt, md, csv...) may be written, not executables or config folders / 只能寫入一般文件（txt、md、csv 等），不能寫可執行檔或設定目錄", path
        if name != "gw_list_dir" and _hard_linked(path):
            return HARDLINK_REASON, path
        return None, path

    def _resolve(self, p):
        p = str(p)
        if self.root and not os.path.isabs(p):
            p = os.path.join(self.root, p)
        return os.path.realpath(p)

    def _inside(self, p):
        if not self.root:
            return False
        pc, rc = os.path.normcase(p), os.path.normcase(self.root)
        return pc == rc or pc.startswith(rc + os.sep)

    def _sensitive(self, p):
        low = p.replace("\\", "/").lower()
        return any(re.search(pat, low) for pat in self.policy.get("sensitive_paths", []))

    def _write_denied(self, p):
        """Allowlist of document extensions; anything else (scripts, links, extensionless
        files like a .git gitdir) could be executed or change behaviour later."""
        low = p.replace("\\", "/").lower()
        ext = os.path.splitext(low)[1]
        if ext not in set(self.policy.get("write_allow_ext", [".txt", ".md", ".csv"])):
            return True
        return any(re.search(pat, low) for pat in self.policy.get("write_deny_paths", []))

    # ---- fetch safety
    @staticmethod
    def _host(url):
        u = urllib.parse.urlsplit(str(url or ""))
        return "%s:%s" % ((u.hostname or "").lower(), u.port or (443 if u.scheme == "https" else 80))

    def _allow_hosts(self):
        return set(h.lower() for h in self.policy.get("fetch_allow_hosts", []))

    def _url_allowed(self, url):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            return False, "only http/https URLs are allowed / 只允許 http/https 網址"
        if self._host(url) in set(h.lower() for h in self.policy.get("fetch_private_ok", [])):
            return True, ""
        try:
            addrs = {ai[4][0] for ai in socket.getaddrinfo(u.hostname, None)}
        except OSError:
            return False, "host name does not resolve / 主機名稱無法解析"
        for addr in addrs:
            ip = ipaddress.ip_address(addr.split("%")[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast \
                    or (ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10")):  # CGNAT / Tailscale
                return False, "local or private-network addresses are not allowed / 不允許連到本機或內網位址"
        return True, ""

    def _opener(self):
        gate = self

        class Redirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                ok, why = gate._url_allowed(newurl)
                if not ok:
                    raise urllib.error.URLError("redirect refused: " + why)
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        # no FileHandler / FTPHandler / DataHandler, and ambient proxies ignored
        o = urllib.request.OpenerDirector()
        for h in (urllib.request.ProxyHandler({}), urllib.request.HTTPHandler(), urllib.request.HTTPSHandler(),
                  Redirect(), urllib.request.HTTPErrorProcessor(), urllib.request.HTTPDefaultErrorHandler()):
            o.add_handler(h)
        return o

    # ---- approval (out of band)
    def summary(self, name, args, pol):
        shown = dict(args)
        if name in FILE_TOOLS:
            # the path decide() judged, which run_builtin() insists on finding again
            shown["path"] = self.decided_path or self._resolve(args.get("path") or ".")
        if isinstance(shown.get("content"), str) and len(shown["content"]) > 2000:
            # the full text is bound by its hash, so what is written is still what the user saw
            shown["content"] = shown["content"][:2000] + "… (%d characters in total, sha256 %s)" % (
                len(args["content"]), hashlib.sha256(args["content"].encode("utf-8")).hexdigest())
        head = {"money": "Payment / 付款", "comms": "Sends as you / 以你的身分對外發送"}.get(pol.get("class"), _visible(name))
        # every argument, sorted, so nothing the hash binds is invisible to the user
        return head + "\n" + "\n".join("%s = %s" % (_visible(json.dumps(k, ensure_ascii=False)),
                                                    _visible(json.dumps(shown[k], ensure_ascii=False)))
                                       for k in sorted(shown))

    def ask_user(self, name, args, pol, reason, digest=None):
        if not self.a.approve_port:
            return False
        pid = secrets.token_urlsafe(16)
        if digest is None:
            digest = _args_digest(name, args)
        with self.lock:
            self.pending[pid] = {"tool": name, "summary": self.summary(name, args, pol), "reason": reason,
                                 "digest": digest, "ts": time.time(), "decision": None}
        self._tell_user(name)
        deadline = time.time() + self.a.approve_wait
        try:
            while time.time() < deadline:
                with self.lock:
                    d = self.pending[pid]["decision"]
                if d is not None:
                    return d == "approve"
                time.sleep(0.3)
            return False
        finally:
            with self.lock:
                self.pending.pop(pid, None)  # single use, whatever happened

    def _tell_user(self, name):
        """A held call only helps if the user knows: log the page address on stderr (MCP clients
        keep it in their server log) and open the page, at most once a minute."""
        url = "http://127.0.0.1:%d/" % self.a.approve_port
        headless = sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        do_open = not (self.a.no_open or headless) and time.time() - self.last_open >= 60
        if do_open:
            self.last_open = time.time()

        def _run():  # off the serve loop: a client that never drains stderr must not stall agentgate
            try:
                sys.stderr.write("agentgate: %s is waiting for your approval: %s\n" % (_visible(name), url))
                sys.stderr.flush()
            except Exception:
                pass
            if not do_open:
                return
            try:
                # not the webbrowser module: on POSIX it hands the browser our stdin/stdout, which
                # are the MCP pipes (stray output, a pipe held open after we exit)
                if os.name == "nt":
                    os.startfile(url)
                else:
                    subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", url],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _make_ui(self):
        gate = self
        port = self.a.approve_port
        local_hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}

        class UI(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _local(self):
                if self.headers.get("Host", "") not in local_hosts:
                    return False  # DNS rebinding
                origin = self.headers.get("Origin")
                if origin and origin not in ("http://" + h for h in local_hosts):
                    return False
                return self.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")

            def do_GET(self):
                if not self._local():
                    self.send_error(403)
                    return
                with gate.lock:
                    rows = "".join(
                        "<li><b>%s</b><pre>%s</pre><small>Reason / 原因：%s · argument fingerprint / 參數指紋 %s</small>"
                        "<form method=post action='/d'><input type=hidden name=id value='%s'>"
                        "<input type=hidden name=csrf value='%s'>"
                        "<button name=d value=approve>Approve once / 批准這一次</button> <button name=d value=deny>Deny / 拒絕</button></form></li>"
                        % (html.escape(_visible(p["tool"])), html.escape(p["summary"]), html.escape(p["reason"]),
                           p["digest"][:12], html.escape(pid), gate.csrf)
                        for pid, p in gate.pending.items())
                # long or padded values wrap instead of pushing the rest of the line off-screen
                body = ("<meta charset=utf-8><meta http-equiv=refresh content=3><title>agentgate</title>"
                        "<style>pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>"
                        "<h3>Waiting for your confirmation / 等待你確認的動作</h3><ul>%s</ul>" % (rows or "<li>Nothing waiting / 目前沒有</li>")).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                try:
                    n = max(0, min(int(self.headers.get("Content-Length") or 0), 4096))
                except ValueError:
                    n = 0
                form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
                pid, d, tok = (form.get(k, [""])[0] for k in ("id", "d", "csrf"))
                if not self._local() or not secrets.compare_digest(tok, gate.csrf):
                    self.send_error(403)
                    return
                with gate.lock:
                    if pid in gate.pending and d in ("approve", "deny"):
                        gate.pending[pid]["decision"] = d
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()

        try:
            return _ApprovalServer(("127.0.0.1", port), UI)
        except OSError as e:
            sys.exit("agentgate: cannot open the confirmation page on 127.0.0.1:%d (%s). Is another agentgate "
                     "using it? Give each wrapped server its own --approve-port." % (port, e))

    # ---- builtins
    def _recheck(self, name, args, judged):
        """At execution time: the same hard denies again, and the path must still resolve to the
        one decide() judged (and the user saw), since an approval can wait for minutes."""
        why, p = self._path_check(name, args)
        if why:
            raise PermissionError(why)
        if judged is None or os.path.normcase(p) != os.path.normcase(judged):
            raise PermissionError(MOVED_REASON)
        return p

    @staticmethod
    def _open_checked(p, write):
        """-> a descriptor for p (a write is not truncated yet), refused unless it is the file that
        was checked: a regular file with one link, still at p, not reached through a link.
        POSIX: O_NOFOLLOW refuses a symlink at p. Windows has no O_NOFOLLOW: a link swapped in
        between the checks and the open is caught by comparing the file ids of p before the
        open, the descriptor, and p after it; a new file is created with O_EXCL."""
        before = None
        try:
            before = os.lstat(p)
        except FileNotFoundError:
            if not write:
                raise
        if before is not None and stat.S_ISLNK(before.st_mode):
            raise PermissionError(MOVED_REASON)
        flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        if write:
            flags |= os.O_WRONLY | (os.O_CREAT | os.O_EXCL if before is None else 0)
        else:
            flags |= os.O_RDONLY
        fd = os.open(p, flags, 0o666)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise PermissionError("not a regular file / 不是一般檔案")
            if st.st_nlink > 1:  # same rule as decide(); some FUSE/network filesystems report 0
                raise PermissionError(HARDLINK_REASON)
            now = os.lstat(p)
            if not os.path.samestat(st, now) or (before is not None and not os.path.samestat(before, st)) \
                    or os.path.normcase(os.path.realpath(p)) != os.path.normcase(p):
                raise PermissionError(MOVED_REASON)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def run_builtin(self, name, args, judged=None):
        """judged: the resolved path decide() judged for a file tool; the call is refused if the
        path no longer resolves to it."""
        if name == "gw_list_dir":
            p = self._recheck(name, args, judged)
            return "\n".join(sorted(os.listdir(p)))
        if name == "gw_read_file":
            p = self._recheck(name, args, judged)
            with os.fdopen(self._open_checked(p, False), "r", encoding="utf-8", errors="replace") as f:
                return f.read()[:100000]
        if name == "gw_write_file":
            p = self._recheck(name, args, judged)
            parent = os.path.realpath(os.path.dirname(p))
            if not self._inside(parent) and not self._inside(p):
                raise PermissionError("parent folder outside workspace")
            os.makedirs(parent, exist_ok=True)
            fd = self._open_checked(p, True)
            try:
                os.ftruncate(fd, 0)  # only now: a hard-linked or swapped file was refused untouched
            except BaseException:
                os.close(fd)
                raise
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(args["content"])
            return "written %d chars" % len(args["content"])
        if name == "gw_fetch":
            req = urllib.request.Request(str(args["url"]), method="GET", headers={"User-Agent": "agentgate/" + VERSION})
            with self._opener().open(req, timeout=15) as r:
                raw = r.read(300000).decode("utf-8", "replace")
            self.visited_hosts.add(self._host(args["url"]))
            raw = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw)
            return raw[:60000]
        raise KeyError(name)

    def _remember(self, result):
        """Strings that came out of tools may not be smuggled out in a URL later."""
        for c in (result or {}).get("content", []) or []:
            text = c.get("text", "") if isinstance(c, dict) else ""
            for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9._+-]{4,}[A-Za-z0-9]", text):
                n = _alnum(t)
                if len(n) >= 6 and any(ch.isdigit() for ch in n):
                    self.seen_tokens.add(n)

    # ---- agent side
    def log(self, entry):
        """One JSON line per call, hash-chained: "prev" is the hash of the file's last line when it
        is written, so a restart keeps a single chain. No file lock: one agentgate per log file."""
        if not self.a.log:
            return
        entry["ts"] = time.time()
        entry["tainted"] = self.tainted
        entry["seq"] = self.log_seq + 1
        tip, cut = _chain_tip(self.a.log)
        if tip is None:
            entry["chain_restart"] = True  # the last line is missing its hash, cut short or garbled
        entry["prev"] = tip or ZERO_HASH
        entry["hash"] = _entry_hash(entry)
        line = (("\n" if cut else "") + json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
        with io.open(self.a.log, "ab") as f:
            f.write(line)
        self.log_seq += 1

    def list_tools(self):
        out = []
        tools = [(t, True) for t in self.up_tools]
        if self.a.builtins:  # ours: no sanitising needed
            tools += [({"name": n, "description": t["description"], "inputSchema": t["inputSchema"]}, False)
                      for n, t in BUILTIN_TOOLS.items()]
        for t, upstream in tools:
            v = self.class_verdict(self.policy_for(t.get("name")))
            if v == "deny":
                continue  # the agent never learns the capability exists
            t = _sanitize_tool(t) if upstream else dict(t)
            if v == "confirm":
                t["description"] = t.get("description", "") + \
                    " [agentgate: every call waits for the user's approval on the gateway page]"
            out.append(t)
        return out

    def call_tool(self, name, args):
        if not isinstance(args, dict):
            args = {}
        known = (self.a.builtins and name in BUILTIN_TOOLS) or any(t.get("name") == name for t in self.up_tools)
        pol = self.policy_for(name)
        try:
            digest = _args_digest(name, args)  # one value for the approval page and the log
        except UnicodeError:
            digest = None  # a lone surrogate: ask_user fails on it as before, the log records null
        self.decided_path = None
        if not known:
            verdict, reason = "deny", "no such tool / 沒有這個工具"
        else:
            verdict, reason = self.decide(name, args, pol)
        judged = self.decided_path
        approved = None
        if verdict == "confirm":
            approved = self.ask_user(name, args, pol, reason, digest)
            verdict = "allow" if approved else "deny"
            reason += "; " + ("approved by the user / 使用者已批准" if approved else
                             "not approved: %s" % ("denied or timed out on the confirmation page / 確認頁逾時或拒絕"
                                                   if self.a.approve_port else "no approver is online / 目前沒有批准者在線"))
        # arguments are not logged (they may be private): only their fingerprint
        self.log({"tool": name, "class": pol.get("class"), "verdict": verdict, "reason": reason, "approved": approved,
                  "args_sha256": digest})
        if verdict != "allow":
            return {"isError": True, "content": [{"type": "text", "text":
                    "BLOCKED_BY_AGENTGATE: the boundary gateway blocked %s (%s). This action needs the user's own approval "
                    "on the gateway's confirmation page. Tell the user what needs approving; do not try another tool "
                    "or route to do the same thing. / 邊界閘道攔下了 %s。這個動作需要使用者在閘道確認頁親自批准。"
                    "請把需要批准的內容告訴使用者，不要改用其他工具或方式完成同一件事。" % (name, reason, name)}]}
        if pol.get("untrusted_output"):
            self.tainted = True  # before the call: an error message can carry outside text too
        if self.a.builtins and name in BUILTIN_TOOLS:
            try:
                result = {"content": [{"type": "text", "text": self.run_builtin(name, args, judged)}]}
            except Exception as e:
                result = {"isError": True, "content": [{"type": "text", "text": "error: %s" % e}]}
        else:
            try:
                result = self._up_call("tools/call", {"name": name, "arguments": args})
            except Exception as e:
                result = {"isError": True, "content": [{"type": "text", "text": "upstream error: %s" % e}]}
        if name == "gw_read_file" or pol.get("pii"):
            self._remember(result)  # only secret-bearing reads; page text would block normal links
        if pol.get("class") != "query" or pol.get("pii"):
            self.secret_seen = True  # files, mail, contacts, unknown tools: anything that may be private
        return result

    def serve(self):
        out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n")

        def reply(id_, result=None, error=None):
            msg = {"jsonrpc": "2.0", "id": id_}
            msg.update({"error": error} if error else {"result": result})
            out.write(json.dumps(msg, ensure_ascii=False) + "\n")
            out.flush()

        stdin = sys.stdin.buffer
        while True:
            raw = stdin.readline(MAX_LINE)
            if not raw:
                break
            if not raw.endswith(b"\n") and len(raw) >= MAX_LINE:
                while raw and not raw.endswith(b"\n"):  # drop the rest of an oversized line
                    raw = stdin.readline(MAX_LINE)
                reply(None, error={"code": -32600, "message": "request too large"})
                continue
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            id_ = None
            try:
                req = json.loads(line)
                if not isinstance(req, dict):
                    reply(None, error={"code": -32600, "message": "batch or non-object requests not supported"})
                    continue
                method, id_ = req.get("method"), req.get("id")
                params = req.get("params") if isinstance(req.get("params"), dict) else {}
                if method == "initialize":
                    reply(id_, {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                                "capabilities": {"tools": {}},
                                "serverInfo": {"name": "agentgate", "version": VERSION}})
                elif method == "tools/list":
                    reply(id_, {"tools": self.list_tools()})
                elif method == "tools/call":
                    reply(id_, self.call_tool(str(params.get("name")), params.get("arguments") or {}))
                elif method == "ping":
                    reply(id_, {})
                elif id_ is not None:
                    reply(id_, error={"code": -32601, "message": "method not found"})
            except Exception as e:
                if id_ is not None:
                    reply(id_, error={"code": -32603, "message": str(e)})
        # agent closed the session: take the upstream down with us
        try:
            self.up.stdin.close()
            self.up.wait(3)
        except Exception:
            self.up.kill()


def resolve_policy(p):
    """A path, or a built-in name (policy.<name>.json next to this file, shipped with the package)."""
    if os.path.isfile(p) or not re.fullmatch(r"[a-z][a-z0-9_-]*", p):
        return p
    builtin = os.path.join(HERE, "policy.%s.json" % p)
    if os.path.isfile(builtin):
        return builtin
    sys.exit("no policy file %r and no built-in policy of that name (built-in: assistant). "
             "MCP clients start servers in their own working folder: give a custom policy as an absolute path." % p)


def main():
    if sys.argv[1:2] == ["demo"]:
        if __package__:
            from . import demo  # installed: the agentgate package
        else:
            import demo  # repo checkout: python gateway/gateway.py demo
        sys.exit(demo.run(sys.argv[2:]))
    if sys.argv[1:2] == ["verify-log"]:
        usage = "usage: agentgate verify-log FILE"
        if sys.argv[2:] in (["-h"], ["--help"]):
            print(usage + "\n\nRecompute the hash chain of an agentgate --log file. Prints OK with the entry "
                  "count,\nor BROKEN with the first bad line (exit 1). Exit 2: usage error or unreadable file.")
            sys.exit(0)
        if len(sys.argv) != 3:
            print(usage, file=sys.stderr)
            sys.exit(2)
        try:
            ok, msg = verify_log(sys.argv[2])
        except OSError as e:
            print("agentgate verify-log: %s" % e, file=sys.stderr)
            sys.exit(2)
        print(msg)
        sys.exit(0 if ok else 1)
    ap = argparse.ArgumentParser(prog="agentgate", epilog="Try it first: agentgate demo")
    ap.add_argument("--version", action="version", version="agentgate " + VERSION)
    ap.add_argument("--policy", required=True, help='policy file, or a built-in name: "assistant"')
    ap.add_argument("--server", required=True, help="key under policy.servers for the upstream's tools")
    ap.add_argument("--root", help="workspace root for builtin file tools")
    ap.add_argument("--builtins", action="store_true")
    ap.add_argument("--approve-port", type=int, default=0)
    ap.add_argument("--approve-wait", type=float, default=120)
    ap.add_argument("--no-open", action="store_true", help="don't open the confirmation page when a call is held")
    ap.add_argument("--log", help="append one hash-chained JSON line per tool call; check it with: agentgate verify-log FILE")
    ap.add_argument("upstream", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    if a.upstream and a.upstream[0] == "--":
        a.upstream = a.upstream[1:]
    if not a.upstream:
        sys.exit("upstream command missing (after --)")
    a.policy = resolve_policy(a.policy)
    if not os.path.isfile(a.policy):
        sys.exit("policy file not found: %r (use an absolute path, or the built-in name: assistant)" % a.policy)
    # Windows: npx, uvx and other .cmd shims only start through their resolved path
    a.upstream[0] = shutil.which(a.upstream[0]) or a.upstream[0]
    Gate(a).serve()


if __name__ == "__main__":
    main()
