"""Minimal RSVData-like upstream for manual checks and smoke tests.

Serves POST /mcp answering initialize/ping/tools/list/tools/call the same way
the real RSVData HTTP service does (stateless JSON-RPC 2.0, text content).
Every tools/call answer echoes the arguments so tests can verify that router
parameters (base/cwd) were stripped before forwarding.

Usage: python tests/mock_upstream.py [port]   (default 8791)
"""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOLS = [
    {
        "name": "ping",
        "description": "Проверка связи с сервером MCP:RSV Data (mock).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "config",
        "description": "Паспорт базы (mock).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "query",
        "description": "Чтение данных одной таблицы (mock).",
        "inputSchema": {
            "type": "object",
            "properties": {"table": {"type": "string"}},
            "required": ["table"],
        },
    },
]


def text_result(text, is_error=False):
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


def handle_message(message, auth_header=None, port=0):
    method = message.get("method")
    msg_id = message.get("id")
    if msg_id is None:
        return None  # notification
    # Echo the auth kind and own port so tests can distinguish targets
    # and verify Authorization forwarding.
    auth = "none"
    if auth_header:
        auth = auth_header.split(" ", 1)[0] if " " in auth_header else auth_header
    if method == "initialize":
        params = message.get("params") or {}
        result = {
            "protocolVersion": str(params.get("protocolVersion") or "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "MCP:RSV Data (mock)", "version": "0.0.1"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params") or {}
        result = text_result(
            f"mock{port}/{params.get('name')} auth={auth} "
            f"args={json.dumps(params.get('arguments') or {}, ensure_ascii=False)}")
    else:
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": f"Метод не поддерживается: {method}"}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        # Any path is accepted: real upstream publications differ in path,
        # and the EDT-derived URLs in stage-4 checks point here under
        # /<ref>/hs/rsvdata/mcp rather than /mcp.
        if not self.path:
            self._plain(404, "not found")
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            message = json.loads(raw.decode("utf-8"))
            answer = handle_message(message, self.headers.get("Authorization"),
                                    self.server.server_address[1]) if isinstance(message, dict) else {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "Ожидался объект JSON-RPC"}}
        except ValueError:
            answer = {"jsonrpc": "2.0", "id": None,
                      "error": {"code": -32700, "message": "Ошибка разбора JSON"}}
        if answer is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps(answer, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._plain(405, "POST only")

    def _plain(self, code, text):
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
    httpd = ThreadingHTTPServer(("127.0.0.1", port), MockHandler)
    httpd.daemon_threads = True
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} mock upstream: http://127.0.0.1:{port}/mcp",
          file=sys.stderr, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
