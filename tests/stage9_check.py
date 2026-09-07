"""Stage-9 acceptance checks: update_targets (mass self-update).

The tool must POST the router binary to <target>/hs/rsvdata/update for
the chosen aliases (or all), reuse the install-chain /update step and
report per target: updated (было -> стало), unchanged (пропуск),
un-updatable (404 — no endpoint: primary install hint, NO EDT/
configurator attempt), or a classified error. No registration happens
and unknown aliases are rejected.

Needs a router launched with --rsvdatabinary on a dummy file and
--rsvmcp "" / bogus --designer (the smoke launcher does all three):
SMOKE_URL (default http://127.0.0.1:8790/mcp), home .test-home9.
"""

import asyncio
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "src"))

from fastmcp import Client  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402


def make_handler(kind):
    """kind: updated | skip | missing — the /update behavior of this mock."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            if not self.path.endswith("/hs/rsvdata/update"):
                body = b"404 - not found"
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if kind == "missing":
                body = b"404 - not found"
                status = 404
            elif kind == "skip":
                status = 200
                body = json.dumps({"результат": "пропуск"},
                                  ensure_ascii=False).encode("utf-8")
            else:
                status = 200
                body = json.dumps({
                    "результат": "обновлено",
                    "было": {"имя": "RSVData", "версия": "1.3.4"},
                    "стало": {"имя": "RSVData", "версия": "1.3.5",
                              "хеш": "s9hash="},
                }, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass

    return Handler


def _dead_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


async def run(url=None, home=None, ports=None, verbose=True):
    """Returns (passed, failed)."""
    url = url or os.environ.get("SMOKE_URL") or "http://127.0.0.1:8790/mcp"
    counters = {"pass": 0, "fail": 0}

    def check(name, cond, extra=""):
        if cond:
            counters["pass"] += 1
            if verbose:
                print(f"PASS  {name}")
        else:
            counters["fail"] += 1
            print(f"FAIL  {name}: {extra}")

    async def call(client, tool, args):
        try:
            result = await client.call_tool(tool, args)
            return None, result.content[0].text
        except ToolError as exc:
            return str(exc), None

    servers = []
    urls = {}
    try:
        for alias, kind in (("s9a", "updated"), ("s9b", "skip"),
                            ("s9c", "missing")):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(kind))
            httpd.daemon_threads = True
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            servers.append(httpd)
            urls[alias] = f"http://127.0.0.1:{httpd.server_address[1]}"
        urls["s9dead"] = f"http://127.0.0.1:{_dead_port()}"

        async with Client(url) as client:
            for alias in ("s9a", "s9b", "s9c", "s9dead"):
                _err, text = await call(client, "register_target", {
                    "url": urls[alias], "base": alias, "check": False})
                check(f"setup: {alias} registered (check=false)",
                      _err is None and "зарегистрирована" in text, _err or text)

            # Unknown alias: rejected up front, nothing to update.
            _err, text = await call(client, "update_targets", {
                "targets": ["s9a", "нет_такой"]})
            check("update_targets: unknown alias rejected",
                  _err and "не зарегистрированы" in _err and "нет_такой" in _err,
                  _err or text)

            # All targets at once (default): one line per target + summary.
            _err, text = await call(client, "update_targets", {})
            check("update all: updated target reports было->стало",
                  not _err and "  s9a: Расширение обновлено через /update: "
                               "1.3.4 -> 1.3.5 (хеш s9hash=)." in text,
                  _err or text)
            check("update all: unchanged target reports пропуск",
                  "  s9b: Расширение актуально: /update вернул «пропуск» "
                  "(хеш совпал)." in text, text)
            check("update all: no-endpoint target hints primary install",
                  "  s9c: точки /update нет" in text
                  and "register_target(install=true)" in text, text)
            check("update all: dead publication reported per target",
                  "  s9dead: цель недоступна (сеть/веб-сервер не отвечает)" in text,
                  text)
            check("update all: summary counts",
                  "Итог: обновлено/актуально 2, нет точки /update 1, ошибок 1."
                  in text, text)
            check("update all: session-pool note present",
                  "новые HTTP-сеансы" in text, text)
            check("update all: no registration text",
                  "зарегистрирована" not in text
                  and "Вариант EDT" not in text
                  and "конфигуратор" not in text.lower(), text)

            # Single alias (string form) and array form.
            _err, text = await call(client, "update_targets", {"targets": "s9a"})
            check("update single (string): only that target",
                  not _err and "s9a:" in text and "s9b:" not in text
                  and "Итог: обновлено/актуально 1" in text, _err or text)
            _err, text = await call(client, "update_targets",
                                    {"targets": ["s9b", "s9c"]})
            check("update subset (array): only chosen targets",
                  not _err and "s9b:" in text and "s9c:" in text
                  and "s9a:" not in text and "s9dead:" not in text, _err or text)

            for alias in ("s9a", "s9b", "s9c", "s9dead"):
                await call(client, "unregister_target", {"base": alias})
            _err, _ = await call(client, "ping", {"base": "s9a"})
            check("cleanup: stage-9 targets removed",
                  _err and "не зарегистрирована" in _err, _err)
    finally:
        for httpd in servers:
            httpd.shutdown()

    return counters["pass"], counters["fail"]


if __name__ == "__main__":
    passed, failed = asyncio.run(run())
    print(f"stage9_check: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
