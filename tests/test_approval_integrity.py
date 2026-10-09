"""Approval-dialog integrity: the call the human approves is the call that runs.

Starts the real gateway (gateway/gateway.py --builtins) in front of
gateway/demo_server.py, speaks MCP over stdio, and drives the local approval
page over HTTP. See "Approval-dialog attacks" in gateway/THREAT_MODEL.md.
All content is synthetic; nothing leaves the machine.
"""
import hashlib
import html
import http.client
import io
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATEWAY = os.path.join(ROOT, "gateway", "gateway.py")
DEMO = os.path.join(ROOT, "gateway", "demo_server.py")
WAIT = 60  # seconds: generous, CI runners can be slow


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _symlinks_work():
    d = tempfile.mkdtemp(prefix="agt-symlink-")
    try:
        os.symlink(os.path.join(d, "target.txt"), os.path.join(d, "link.txt"))
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False  # e.g. Windows without developer mode
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _junctions_work():
    if os.name != "nt":
        return False
    try:
        import _winapi
    except ImportError:
        return False
    return hasattr(_winapi, "CreateJunction")


SYMLINKS = _symlinks_work()


class Gateway:
    """One agentgate process with its own approval port."""

    def __init__(self, root, approve_wait=WAIT):
        self.port = _free_port()
        cmd = [sys.executable, GATEWAY, "--policy", "assistant", "--server", "lifeservices", "--builtins",
               "--root", root, "--approve-port", str(self.port), "--approve-wait", str(approve_wait), "--no-open",
               "--", sys.executable, DEMO]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.replies = {}
        self.cv = threading.Condition()
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()
        self.result(self.send("initialize", {"protocolVersion": "2025-06-18"}))

    def _read(self):
        for line in self.p.stdout:
            try:
                msg = json.loads(line.decode("utf-8"))
            except ValueError:
                continue
            if isinstance(msg, dict):
                with self.cv:
                    self.replies[msg.get("id")] = msg
                    self.cv.notify_all()

    def send(self, method, params):
        self.next_id += 1
        line = json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}) + "\n"
        self.p.stdin.write(line.encode("utf-8"))
        self.p.stdin.flush()
        return self.next_id

    def call(self, name, args):
        """Send a tools/call without waiting for the answer; returns the request id."""
        return self.send("tools/call", {"name": name, "arguments": args})

    def done(self, i):
        with self.cv:
            return i in self.replies

    def result(self, i, timeout=WAIT):
        with self.cv:
            if not self.cv.wait_for(lambda: i in self.replies, timeout):
                raise AssertionError("no answer from the gateway for request %d" % i)
            return self.replies[i]["result"]

    def tool_result(self, i):
        r = self.result(i)
        return bool(r.get("isError")), r["content"][0]["text"]

    def tool(self, name, args):
        return self.tool_result(self.call(name, args))

    # ---- approval page
    def _http(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            headers = {"Content-Type": "application/x-www-form-urlencoded"} if body is not None else {}
            c.request(method, path, body=body, headers=headers)
            r = c.getresponse()
            return r.status, r.read().decode("utf-8")
        finally:
            c.close()

    def page(self):
        status, text = self._http("GET", "/")
        assert status == 200, status
        return text

    def post(self, form):
        return self._http("POST", "/d", urllib.parse.urlencode(form))[0]

    @staticmethod
    def entries(page):
        """[(id, summary, li_html)] for each held call, and the page's CSRF token."""
        out, csrf = [], None
        for li in page.split("<li>")[1:]:
            if "name=id value='" not in li:
                continue
            pid = li.split("name=id value='")[1].split("'")[0]
            csrf = li.split("name=csrf value='")[1].split("'")[0]
            out.append((pid, li.split("<pre>")[1].split("</pre>")[0], li))
        return out, csrf

    def wait_pending(self, exclude=(), timeout=WAIT):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                entries, csrf = self.entries(self.page())
            except OSError:
                entries, csrf = [], None  # page not up yet
            if any(pid not in exclude for pid, _, _ in entries):
                return entries, csrf
            time.sleep(0.2)
        raise AssertionError("no call appeared on the approval page")

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(10)
        except Exception:
            self.p.kill()
            self.p.wait(10)
        try:
            self.p.stdout.close()
        except Exception:
            pass


class ApprovalIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agt-approval-")
        self.ws = os.path.join(self.tmp, "work")
        self.outside = os.path.join(self.tmp, "outside")
        os.makedirs(self.ws)
        os.makedirs(self.outside)
        with io.open(os.path.join(self.ws, "seed.txt"), "w", encoding="utf-8", newline="\n") as f:
            f.write("synthetic seed file\n")
        self.gateways = []
        self.links = []

    def tearDown(self):
        for g in self.gateways:
            g.close()
        for link in self.links:  # remove links first so cleanup never walks through them
            try:
                os.unlink(link)
            except OSError:
                try:
                    os.rmdir(link)  # a Windows junction
                except OSError:
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self):
        g = Gateway(self.ws)
        self.gateways.append(g)
        return g

    def write_outside(self, *parts, text="original synthetic text\n"):
        p = os.path.join(self.outside, *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with io.open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return p

    @staticmethod
    def read(p):
        with io.open(p, encoding="utf-8") as f:
            return f.read()

    def assert_shows_path(self, summary, real):
        self.assertIn("&quot;path&quot; = " + html.escape(json.dumps(real, ensure_ascii=False)), summary)

    # 1. GhostApproval-style: the page names the real target, not the link
    @unittest.skipUnless(SYMLINKS, "creating symlinks is not permitted here")
    def test_symlink_write_shows_real_target(self):
        target = self.write_outside("target.txt")
        link = os.path.join(self.ws, "project_notes.txt")
        os.symlink(target, link)
        self.links.append(link)
        g = self.start()
        i = g.call("gw_write_file", {"path": "project_notes.txt", "content": "synthetic replacement\n"})
        entries, csrf = g.wait_pending()
        self.assertEqual(len(entries), 1)
        pid, summary, li = entries[0]
        self.assert_shows_path(summary, os.path.realpath(target))
        self.assertNotIn("project_notes", li)
        self.assertEqual(g.post({"id": pid, "d": "deny", "csrf": csrf}), 303)
        err, text = g.tool_result(i)
        self.assertTrue(err and "BLOCKED_BY_AGENTGATE" in text)
        self.assertEqual(self.read(target), "original synthetic text\n")

    @unittest.skipUnless(SYMLINKS, "creating symlinks is not permitted here")
    def test_symlink_to_sensitive_target_denied(self):
        target = self.write_outside(".ssh", "authorized_keys.txt")
        link = os.path.join(self.ws, "project_settings.json")
        os.symlink(target, link)
        self.links.append(link)
        g = self.start()
        err, text = g.tool("gw_write_file", {"path": "project_settings.json", "content": "{}"})
        self.assertTrue(err and "sensitive path" in text)  # a hard deny: never shown for approval
        self.assertEqual(Gateway.entries(g.page())[0], [])
        self.assertEqual(self.read(target), "original synthetic text\n")

    @unittest.skipUnless(_junctions_work(), "Windows directory junctions only")
    def test_junction_write_shows_real_target(self):
        import _winapi
        link = os.path.join(self.ws, "docs")
        _winapi.CreateJunction(self.outside, link)  # no special privilege needed
        self.links.append(link)
        g = self.start()
        i = g.call("gw_write_file", {"path": "docs/notes.txt", "content": "synthetic\n"})
        entries, csrf = g.wait_pending()
        pid, summary, _ = entries[0]
        self.assert_shows_path(summary, os.path.realpath(os.path.join(self.outside, "notes.txt")))
        # even when approved, the write is refused at execution: the real parent is outside the workspace
        self.assertEqual(g.post({"id": pid, "d": "approve", "csrf": csrf}), 303)
        err, text = g.tool_result(i)
        self.assertTrue(err and "outside workspace" in text)
        self.assertFalse(os.path.exists(os.path.join(self.outside, "notes.txt")))

    # 2. Lies-in-the-Loop-style: arguments cannot draw fake lines or markup
    def test_page_escapes_control_and_markup(self):
        g = self.start()
        args = {
            "merchant": 'Sunny Cafe\u202e<b>bold</b>\n"amount_twd" = 1',
            "amount_twd": 1200,
            "memo": "line\u2028two\u200bthree\x1b[2Jend",
            "note\nfake": "x",
        }
        i = g.call("pay_merchant", args)
        entries, csrf = g.wait_pending()
        pid, summary, li = entries[0]
        page = g.page()
        for raw in ("\u202e", "\u2028", "\u200b", "\x1b", "<b>bold</b>"):
            self.assertNotIn(raw, page)
        for shown in ("\\u202e", "\\u2028", "\\u200b", "\\u001b", "\\n", "&lt;b&gt;bold&lt;/b&gt;"):
            self.assertIn(shown, summary)
        lines = summary.split("\n")
        self.assertEqual(len(lines), 1 + len(args))  # header + one line per argument, nothing more
        self.assertTrue(lines[1].startswith("&quot;amount_twd&quot; = 1200"))
        self.assertEqual(sum(1 for l in lines if l.startswith("&quot;amount_twd&quot;")), 1)
        self.assertEqual(g.post({"id": pid, "d": "deny", "csrf": csrf}), 303)
        self.assertTrue(g.tool_result(i)[0])

    # 3. Loopjacking-style: approving one held call releases exactly that call
    def test_approve_releases_only_that_call(self):
        g = self.start()
        err, _ = g.tool("gw_read_file", {"path": "seed.txt"})  # taints the session: writes now need approval
        self.assertFalse(err)
        out = os.path.join(self.ws, "result.txt")
        args_a = {"path": "result.txt", "content": "APPROVED-A synthetic\n"}
        a = g.call("gw_write_file", args_a)
        b = g.call("gw_write_file", {"path": "result.txt", "content": "NOT-APPROVED-B synthetic\n"})
        entries, csrf = g.wait_pending()
        self.assertEqual(len(entries), 1)  # one call is held at a time; B is not read yet
        pid_a, summary_a, li_a = entries[0]
        self.assertIn("APPROVED-A", summary_a)
        self.assertNotIn("NOT-APPROVED-B", g.page())
        digest = hashlib.sha256(json.dumps(["gw_write_file", args_a], sort_keys=True,
                                           ensure_ascii=False).encode("utf-8")).hexdigest()
        self.assertIn(digest[:12], li_a)  # the fingerprint binds the raw arguments of A

        self.assertEqual(g.post({"id": pid_a, "d": "approve", "csrf": csrf}), 303)
        err, _ = g.tool_result(a)
        self.assertFalse(err)
        self.assertEqual(self.read(out), "APPROVED-A synthetic\n")

        entries, _ = g.wait_pending(exclude=(pid_a,))
        self.assertEqual(len(entries), 1)
        pid_b, summary_b, _ = entries[0]
        self.assertNotEqual(pid_b, pid_a)
        self.assertIn("NOT-APPROVED-B", summary_b)
        # replaying A's approval does not release B
        self.assertEqual(g.post({"id": pid_a, "d": "approve", "csrf": csrf}), 303)
        time.sleep(1.5)
        self.assertFalse(g.done(b))
        self.assertEqual([e[0] for e in Gateway.entries(g.page())[0]], [pid_b])
        self.assertEqual(self.read(out), "APPROVED-A synthetic\n")

        self.assertEqual(g.post({"id": pid_b, "d": "deny", "csrf": csrf}), 303)
        err, text = g.tool_result(b)
        self.assertTrue(err and "BLOCKED_BY_AGENTGATE" in text)
        self.assertEqual(self.read(out), "APPROVED-A synthetic\n")

    # 4. a valid CSRF token with an id that is not pending changes nothing
    def test_post_with_unknown_id_changes_nothing(self):
        g = self.start()
        target = os.path.join(self.outside, "never.txt")
        i = g.call("gw_write_file", {"path": target, "content": "synthetic\n"})
        entries, csrf = g.wait_pending()
        pid = entries[0][0]
        for bogus in (secrets.token_urlsafe(16), "", pid + "x", pid[:-1]):
            self.assertEqual(g.post({"id": bogus, "d": "approve", "csrf": csrf}), 303)
        self.assertEqual(g.post({"id": pid, "d": "maybe", "csrf": csrf}), 303)
        time.sleep(1.5)
        self.assertFalse(g.done(i))
        self.assertEqual([e[0] for e in Gateway.entries(g.page())[0]], [pid])
        self.assertFalse(os.path.exists(target))
        self.assertEqual(g.post({"id": pid, "d": "deny", "csrf": csrf}), 303)
        self.assertTrue(g.tool_result(i)[0])
        self.assertFalse(os.path.exists(target))

    # 3b/4b. with two wrapped servers, one gateway's page cannot decide the other's call
    def test_csrf_and_id_are_per_gateway(self):
        g1, g2 = self.start(), self.start()
        t1, t2 = os.path.join(self.outside, "one.txt"), os.path.join(self.outside, "two.txt")
        i1 = g1.call("gw_write_file", {"path": t1, "content": "synthetic one\n"})
        i2 = g2.call("gw_write_file", {"path": t2, "content": "synthetic two\n"})
        (e1, csrf1), (e2, csrf2) = g1.wait_pending(), g2.wait_pending()
        pid1, pid2 = e1[0][0], e2[0][0]
        self.assertNotEqual(csrf1, csrf2)
        self.assertEqual(g2.post({"id": pid2, "d": "approve", "csrf": csrf1}), 403)  # other gateway's token
        self.assertEqual(g2.post({"id": pid1, "d": "approve", "csrf": csrf2}), 303)  # other gateway's id: no-op
        time.sleep(1.5)
        self.assertFalse(g1.done(i1) or g2.done(i2))
        for g, pid, csrf, i in ((g1, pid1, csrf1, i1), (g2, pid2, csrf2, i2)):
            self.assertEqual(g.post({"id": pid, "d": "deny", "csrf": csrf}), 303)
            self.assertTrue(g.tool_result(i)[0])
        self.assertFalse(os.path.exists(t1) or os.path.exists(t2))


if __name__ == "__main__":
    unittest.main()
