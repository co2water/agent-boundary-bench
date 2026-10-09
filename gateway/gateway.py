"""agentgate: a boundary gateway between an agent and its MCP servers (prototype).

    python gateway.py --policy policy.json --server lifeservices [--root DIR] [--builtins]
                      [--approve-port 8766] [--log gateway.jsonl] -- <upstream command...>
    python gateway.py demo [--no-approver]      (see demo.py)

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
VERSION = "0.3.1"
HERE = os.path.dirname(os.path.abspath(__file__))

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
    shown as escapes so an argument cannot draw a fake line."""
    return "".join("\\u%04x" % ord(ch) if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch for ch in s)


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
        self.up_tools = [t for t in tools if not (a.builtins and t.get("name") in BUILTIN_TOOLS)]
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
        if name in ("gw_read_file", "gw_list_dir", "gw_write_file"):
            raw = str(args.get("path") or ".")
            # checked before realpath(): a UNC path makes Windows dial out (SMB, NTLM) right there
            if ":" in raw[2:] or (len(raw) > 1 and raw[0] in "/\\" and raw[1] in "/\\"):
                return "deny", "path with a data stream (::$DATA) or a device/network path / 路徑含資料流（::$DATA）或裝置／網路路徑"
            path = self._resolve(raw)
            if self._sensitive(path):
                return "deny", "sensitive path (keys, credentials, password files) / 敏感路徑（金鑰、憑證、密碼類檔案）"
            if name == "gw_write_file" and self._write_denied(path):
                return "deny", "only plain documents (txt, md, csv...) may be written, not executables or config folders / 只能寫入一般文件（txt、md、csv 等），不能寫可執行檔或設定目錄"
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
        if name in ("gw_read_file", "gw_list_dir", "gw_write_file"):
            shown["path"] = self._resolve(args.get("path") or ".")
        if isinstance(shown.get("content"), str) and len(shown["content"]) > 2000:
            shown["content"] = shown["content"][:2000] + "… (%d characters in total)" % len(args["content"])
        head = {"money": "Payment / 付款", "comms": "Sends as you / 以你的身分對外發送"}.get(pol.get("class"), name)
        # every argument, sorted, so nothing the hash binds is invisible to the user
        return head + "\n" + "\n".join("%s = %s" % (_visible(json.dumps(k, ensure_ascii=False)),
                                                    _visible(json.dumps(shown[k], ensure_ascii=False)))
                                       for k in sorted(shown))

    def ask_user(self, name, args, pol, reason):
        if not self.a.approve_port:
            return False
        pid = secrets.token_urlsafe(16)
        digest = hashlib.sha256(json.dumps([name, args], sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
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
                sys.stderr.write("agentgate: %s is waiting for your approval: %s\n" % (name, url))
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
                        % (html.escape(p["tool"]), html.escape(p["summary"]), html.escape(p["reason"]),
                           p["digest"][:12], html.escape(pid), gate.csrf)
                        for pid, p in gate.pending.items())
                body = ("<meta charset=utf-8><meta http-equiv=refresh content=3><title>agentgate</title>"
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
    def run_builtin(self, name, args):
        if name == "gw_list_dir":
            p = self._resolve(args.get("path") or ".")
            return "\n".join(sorted(os.listdir(p)))
        if name == "gw_read_file":
            p = self._resolve(args["path"])
            return io.open(p, encoding="utf-8", errors="replace").read()[:100000]
        if name == "gw_write_file":
            p = self._resolve(args["path"])
            parent = os.path.realpath(os.path.dirname(p))
            if not self._inside(parent) and not self._inside(p):
                raise PermissionError("parent folder outside workspace")
            os.makedirs(parent, exist_ok=True)
            with io.open(p, "w", encoding="utf-8", newline="\n") as f:
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
        if not self.a.log:
            return
        entry["ts"] = time.time()
        entry["tainted"] = self.tainted
        with io.open(self.a.log, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def list_tools(self):
        out = []
        tools = list(self.up_tools)
        if self.a.builtins:
            tools += [{"name": n, "description": t["description"], "inputSchema": t["inputSchema"]}
                      for n, t in BUILTIN_TOOLS.items()]
        for t in tools:
            v = self.class_verdict(self.policy_for(t.get("name")))
            if v == "deny":
                continue  # the agent never learns the capability exists
            t = dict(t)
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
        if not known:
            verdict, reason = "deny", "no such tool / 沒有這個工具"
        else:
            verdict, reason = self.decide(name, args, pol)
        approved = None
        if verdict == "confirm":
            approved = self.ask_user(name, args, pol, reason)
            verdict = "allow" if approved else "deny"
            reason += "; " + ("approved by the user / 使用者已批准" if approved else
                             "not approved: %s" % ("denied or timed out on the confirmation page / 確認頁逾時或拒絕"
                                                   if self.a.approve_port else "no approver is online / 目前沒有批准者在線"))
        self.log({"tool": name, "class": pol.get("class"), "verdict": verdict, "reason": reason, "approved": approved})
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
                result = {"content": [{"type": "text", "text": self.run_builtin(name, args)}]}
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
    ap = argparse.ArgumentParser(prog="agentgate", epilog="Try it first: agentgate demo")
    ap.add_argument("--version", action="version", version="agentgate " + VERSION)
    ap.add_argument("--policy", required=True, help='policy file, or a built-in name: "assistant"')
    ap.add_argument("--server", required=True, help="key under policy.servers for the upstream's tools")
    ap.add_argument("--root", help="workspace root for builtin file tools")
    ap.add_argument("--builtins", action="store_true")
    ap.add_argument("--approve-port", type=int, default=0)
    ap.add_argument("--approve-wait", type=float, default=120)
    ap.add_argument("--no-open", action="store_true", help="don't open the confirmation page when a call is held")
    ap.add_argument("--log")
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
