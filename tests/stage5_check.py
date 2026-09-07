"""Stage-5 acceptance checks: shared upstream-failure classification.

forward.post_json must carry status/body, explain_failure must produce the
user-ready block (401 auth hint, 404 publication, 409 verbatim extension
mismatch text, 5xx web-server) ending with the "don't investigate, show the
user" note; router-level: register_target check=True/False, ping errors and
list_targets(check=True) all surface that block.

Standalone (defaults): router on :8786, mocks on :8791/:8792, home .test-home5.
Via tests/smoke.py: run(url, home, ports) with dynamically picked ports.
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

from rsv_data_router import forward  # noqa: E402
from fastmcp import Client  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402

MISMATCH_TEXT = ("Версия модуля расширения веб-сервера (8.3.24.1368) отличается "
                 "от версии сервера (8.3.27.1234) — обновите одну из сторон")


class StatusHandler(BaseHTTPRequestHandler):
    """Answers POST <port>/<code>[/anything] with that HTTP status (the code
    is the first path segment — router-level targets carry the full
    /hs/rsvdata/mcp tail); /rpc-error answers 200 with a JSON-RPC error
    envelope (tool-level, not transport)."""

    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self.path == "/rpc-error":
            code, body = 200, json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "error": {"code": -32000, "message": "Таблица не найдена"}}).encode("utf-8")
        else:
            first = self.path.strip("/").split("/")[0]
            code = int(first) if first.isdigit() else 200
            bodies = {401: "401 - Unauthorized: access is denied.",
                      404: "404 - File or directory not found.",
                      409: json.dumps({"version": "8.3.24.1368", "error": MISMATCH_TEXT},
                                      ensure_ascii=False),
                      502: "<html><body><h1>502 Bad Gateway</h1></body></html>"}
            body = bodies.get(code, "teapot").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def _dead_port():
    """A port that currently refuses connections (bind, read, close)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


async def unit_checks(check):
    """explain_failure / explain_short against a live status server."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), StatusHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        try:
            forward.post_json(f"{base}/401", b"{}")
            check("401 raises TransportError", False, "no exception")
        except forward.TransportError as exc:
            text = forward.explain_failure(exc)
            check("401: status carried", exc.status == 401, exc.status)
            check("401: auth hint", "set-secret" in text and "keyring" in text, text)
            check("401: agent note", "Не разбирайте ошибку сами" in text, text)
            check("401: no body noise", "access is denied" not in text, text)

        try:
            forward.post_json(f"{base}/404", b"{}")
            check("404 raises", False, "no exception")
        except forward.TransportError as exc:
            text = forward.explain_failure(exc)
            check("404: publication missing, honestly",
                  "публикация не найдена" in text and "RSVData" in text, text)

        try:
            forward.post_json(f"{base}/409", b"{}")
            check("409 raises", False, "no exception")
        except forward.TransportError as exc:
            text = forward.explain_failure(exc)
            check("409: platform mismatch named",
                  "несовпадение версий платформы" in text and "веб-сервера" in text, text)
            check("409: server text verbatim", MISMATCH_TEXT in text, text)
            check("409: no rsvdata confusion", "RSVData" not in text, text)

        try:
            forward.post_json(f"{base}/502", b"{}")
            check("502 raises", False, "no exception")
        except forward.TransportError as exc:
            text = forward.explain_failure(exc)
            check("502: web-server internal error",
                  "внутренняя ошибка веб-сервера" in text, text)
            check("502: HTML body suppressed", "Bad Gateway" not in text, text)

        try:
            forward.post_json(f"{base}/418", b"{}")
            check("418 raises", False, "no exception")
        except forward.TransportError as exc:
            check("other status: unexpected answer",
                  "неожиданный ответ веб-сервера" in forward.explain_failure(exc),
                  forward.explain_failure(exc))

        try:
            forward.post_json(f"http://127.0.0.1:{_dead_port()}/mcp", b"{}")
            check("network error raises", False, "no exception")
        except forward.TransportError as exc:
            text = forward.explain_failure(exc)
            check("network: unreachable wording",
                  exc.status is None and "Цель недоступна" in text, text)

        for code, marker in ((401, "нужна авторизация"), (404, "публикация не найдена"),
                             (409, "рассинхрон версий платформы"), (502, "внутренняя ошибка")):
            short = forward.explain_short(forward.TransportError("x", status=code))
            check(f"explain_short {code}: one line with marker",
                  "\n" not in short and marker in short, short)

        try:
            forward.call_tool(f"{base}/rpc-error", "query", {})
            check("json-rpc error: plain RuntimeError passthrough", False, "no exception")
        except RuntimeError as exc:
            check("json-rpc error: plain RuntimeError passthrough",
                  "Таблица не найдена" in str(exc) and "Не разбирайте" not in str(exc), exc)
    finally:
        httpd.shutdown()


async def run(url=None, home=None, ports=None, verbose=True):
    """Returns (passed, failed)."""
    url = url or os.environ.get("SMOKE_URL") or "http://127.0.0.1:8786/mcp"
    home = home or os.environ.get("SMOKE_HOME") or os.path.join(ROOT, ".test-home5")
    p1, _p2 = ports or (8791, 8792)
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

    await unit_checks(check)

    status = ThreadingHTTPServer(("127.0.0.1", 0), StatusHandler)
    status.daemon_threads = True
    threading.Thread(target=status.serve_forever, daemon=True).start()
    sb = f"http://127.0.0.1:{status.server_address[1]}"
    try:
        async with Client(url) as client:
            listing = (await client.call_tool("list_targets", {})).content[0].text
            if "mock1" not in listing:
                await client.call_tool("register_target", {
                    "url": f"http://127.0.0.1:{p1}/mcp", "base": "mock1"})

            _err, text = await call(client, "register_target", {
                "url": f"{sb}/401/hs/rsvdata/mcp", "base": "err401"})
            check("register 401-target: saved + classified",
                  not _err and "Запись сохранена" in text and "HTTP 401" in text
                  and "set-secret" in text and "Не разбирайте ошибку сами" in text,
                  _err or text)

            _err, text = await call(client, "register_target", {
                "url": f"{sb}/409/hs/rsvdata/mcp", "base": "err409"})
            check("register 409-target: mismatch text verbatim",
                  not _err and MISMATCH_TEXT in text and "несовпадение версий" in text,
                  _err or text)

            _err, text = await call(client, "register_target", {
                "url": f"{sb}/404/hs/rsvdata/mcp", "base": "skip404", "check": False})
            check("register with check=False: no check section",
                  not _err and "Проверка связи" not in text, _err or text)

            err, _ = await call(client, "ping", {"base": "err401"})
            check("ping 401: classified error with agent note",
                  err and "HTTP 401" in err and "Не разбирайте ошибку сами" in err, err)

            err, text = await call(client, "ping", {"base": "mock1"})
            check("ping healthy target: still ok",
                  not err and f"mock{p1}/ping" in text, err or text)

            _err, text = await call(client, "list_targets", {"check": True})
            check("list_targets check: short lines + note",
                  "НЕДОСТУПНА — HTTP 401" in text and "рассинхрон версий платформы" in text
                  and "Не разбирайте ошибку сами" in text and "HTTP 404" in text,
                  _err or text)

            for alias in ("err401", "err409", "skip404", "mock1"):
                _err, _ = await call(client, "unregister_target", {"base": alias})
            err, _ = await call(client, "ping", {"base": "err401"})
            check("cleanup: err-targets removed",
                  err and "не зарегистрирована" in err, err)
            err, _ = await call(client, "ping", {"base": "mock1"})
            check("cleanup: mock1 removed too (clean map for later stages)",
                  err and "не зарегистрирована" in err, err)
    finally:
        status.shutdown()

    return counters["pass"], counters["fail"]


if __name__ == "__main__":
    passed, failed = asyncio.run(run())
    print(f"stage5_check: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
