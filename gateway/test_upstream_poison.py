"""Test-only MCP server (stdio) for test_gateway.py: tool metadata with invisible characters.

Every text is harmless and synthetic ("Lists the inbox."); the invisible characters are
written as escapes so this file itself contains none. Not a demo: see demo_server.py.
"""
import io
import json
import sys

ZW = "\u200b"           # zero width space
BIDI = "\u202e"         # right-to-left override
TAGS = "".join(chr(0xE0000 + ord(c)) for c in "TEST") + "\U000E007F"  # Unicode tag characters
VS = "\U000E0100\U000E0101\ufe00\ufe0f"  # variation selectors
HIDDEN = ZW + BIDI + TAGS + VS

TOOLS = [
    {"name": "mail_list",
     "title": "Inbox" + ZW,
     "description": "Lists" + ZW + " the inbox." + BIDI + "x" + TAGS + VS + "\x07",
     "inputSchema": {"type": "object",
                     "properties": {
                         "folder": {"type": "string", "title": "Fol" + ZW + "der",
                                    "description": "Folder" + TAGS + " name.",
                                    "enum": ["inbox" + ZW, "archive" + BIDI],
                                    "default": "inbox" + TAGS},
                         "lim" + ZW + "it": {"type": "array", "items": {"type": "string", "description": "One\ufe0f" + VS}}},
                     "required": ["folder"]},
     "annotations": {"title": "Inbox" + BIDI}},
    # a name with a hidden character: must not be listed or callable
    {"name": "mail" + ZW + "read", "description": "Reads one mail.",
     "inputSchema": {"type": "object", "properties": {}}},
    # names outside the MCP convention: a line break that would draw a fake argument row, a space
    {"name": 'mail_list\n"path" = "x.txt"', "description": "Lists the inbox.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "mail list", "description": "Lists the inbox.", "inputSchema": {"type": "object", "properties": {}}},
    # clean text that keeps its newline, tab, CRLF and one emoji variation selector: no marker
    {"name": "mail_clean", "description": "Lists the inbox.\nSecond line.\tTabbed.\r\nThird ⚠\ufe0f.",
     "inputSchema": {"type": "object", "properties": {"q": {"type": "string", "description": "Query."}}}},
    # an over-long description is capped
    {"name": "mail_long", "description": "Lists the inbox. " * 300,
     "inputSchema": {"type": "object", "properties": {}}},
]


def main():
    out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n")
    for raw in io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8"):
        raw = raw.strip()
        if not raw:
            continue
        req = json.loads(raw)
        method, id_ = req.get("method"), req.get("id")
        params = req.get("params") or {}
        if id_ is None:
            continue
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
                      "serverInfo": {"name": "agentgate-test-poison", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "called " + str(params.get("name"))}]}
        else:
            out.write(json.dumps({"jsonrpc": "2.0", "id": id_, "error": {"code": -32601, "message": "method not found"}}) + "\n")
            out.flush()
            continue
        out.write(json.dumps({"jsonrpc": "2.0", "id": id_, "result": result}, ensure_ascii=False) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
