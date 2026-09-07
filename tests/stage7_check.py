"""Stage-7 acceptance checks: register_target(install=True) chain.

The launch starts with a preflight: a chain known-dead in advance (no
extension endpoint and no connection source — url-only registration — or
no platform version for the configurator) is refused right in the register
answer — NO background job (a live-case lesson).
Live chains run as jobs: /update -> EDT (MCP:RSV disabled in smoke) ->
configurator (no sources, refused) -> availability check; every branch
must end in a user-ready report and never raise. The publication gates
nothing. The mock serves GET (root probe), POST /hs/rsvdata/update
(mode: updated | skip | missing) and POST /hs/rsvdata/mcp (ping:
ok | unauth | missing).

Needs a router launched with --rsvmcp "" and sandboxed env sources
(empty EDT projects registry, bogus 1cv8 roots — the smoke launcher sets
both): SMOKE_URL (default http://127.0.0.1:8787/mcp), home .test-home7.
"""

import asyncio
import json
import os
import re
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "src"))

from fastmcp import Client  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402

PING_TEXT = "pong — stage7; расширение 1.3.5, хеш s7hash="


def make_handler(state):
    """state["update"]: updated | skip | missing (/update behavior);
    state["ping"]: ok | unauth | missing; state["root"]: ok | missing."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _answer(self, status, body, content_type="application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if state.get("root") == "missing":
                self._answer(404, b"404 - not found", "text/plain")
            else:
                self._answer(200, b"publication-ok", "text/plain")

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            if self.path.endswith("/hs/rsvdata/mcp"):
                mode = state.get("ping", "ok")
                if mode == "unauth":
                    self._answer(401, b"401 - unauthorized", "text/plain")
                    return
                if mode == "missing":
                    self._answer(404, b"404 - not found", "text/plain")
                    return
                self._answer(200, json.dumps({
                    "jsonrpc": "2.0", "id": 1,
                    "result": {"content": [{"type": "text", "text": PING_TEXT}]},
                }).encode("utf-8"))
                return
            if self.path.endswith("/hs/rsvdata/update"):
                mode = state["update"]
                if mode == "missing":
                    self._answer(404, b"404 - not found", "text/plain")
                    return
                answer = {"результат": "пропуск"} if mode == "skip" else {
                    "результат": "обновлено",
                    "было": {"имя": "RSVData", "версия": "1.3.4"},
                    "стало": {"имя": "RSVData", "версия": "1.3.5", "хеш": "s7hash="},
                }
                self._answer(200, json.dumps(answer, ensure_ascii=False).encode("utf-8"))
                return
            self._answer(404, b"404 - not found", "text/plain")

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
    url = url or os.environ.get("SMOKE_URL") or "http://127.0.0.1:8787/mcp"
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

    async def install_and_wait(client, args, timeout_s=30):
        """register_target(install=True) now runs as a background job:
        the call answers at once with a job id, the chain report arrives
        via job_status polls. Returns (err, register_text, final_status,
        job_id)."""
        _err, text = await call(client, "register_target", args)
        match = re.search(r"id задания ([0-9a-f]+)", text or "")
        if _err or not match:
            return _err, text, None, None
        job_id = match.group(1)
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            _e, status = await call(client, "job_status", {"job_id": job_id})
            if status and "ВЫПОЛНЯЕТСЯ" not in status:
                return _err, text, status, job_id
            await asyncio.sleep(0.3)
        return _err, text, f"TIMEOUT waiting for job {job_id}", job_id

    state = {"update": "updated", "ping": "ok", "root": "ok"}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    sb = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        async with Client(url) as client:
            _err, reg, text, _jid = await install_and_wait(client, {
                "url": sb, "base": "s7up", "check": True, "install": True})
            check("install: started as a background job (id in answer)",
                  _err is None and "ФОНОВЫМ ЗАДАНИЕМ" in reg and "job_status" in reg
                  and _jid, _err or reg)
            check("install: /update applied (no publication gate)",
                  _err is None and "Расширение обновлено через /update: 1.3.4 -> 1.3.5" in text,
                  _err or text)
            check("install: availability ping ok with version",
                  text and "Проверка связи: ок" in text and "1.3.5, хеш s7hash=" in text, text)

            state["update"] = "missing"
            _err, reg, text, _jid = await install_and_wait(client, {
                "url": sb, "base": "s7old", "check": True, "install": True})
            check("install missing /update: moves on",
                  "Точки /update нет" in text, text)
            check("install missing /update: EDT skipped (rsvmcp off)",
                  "адрес MCP:RSV не задан" in text, text)
            check("install missing /update: configurator refused, chain ends",
                  "Вариант конфигуратора недоступен" in text
                  and "Установка не выполнена ни одним из способов." in text, text)
            check("install missing /update: availability checked anyway",
                  "Проверка связи: ок" in text, text)

            state["update"] = "updated"
            state["ping"] = "unauth"
            _err, text = await call(client, "register_target", {
                "url": sb, "base": "s7auth", "check": True, "install": True})
            check("install ping 401: refused before any job",
                  _err is None and "id задания" not in text
                  and "Установка НЕ запущена" in text, _err or text)
            check("install ping 401: auth diagnosis with next step",
                  "HTTP 401" in text and "Данные авторизации не заданы" in text, text)
            check("install ping 401: url-registration cause named",
                  "регистрация только по url" in text
                  and "connection" in text, text)

            state["update"] = "missing"
            state["ping"] = "missing"
            _err, text = await call(client, "register_target", {
                "url": sb, "base": "s7nosvc", "check": True, "install": True})
            check("install, service 404, root alive: refused, no job",
                  "id задания" not in text and "Установка НЕ запущена" in text,
                  text)
            check("install, service 404, root alive: publication exists",
                  "публикация есть" in text and "сервис rsvdata" in text, text)
            check("install, service 404, root alive: publish-service next step",
                  "Следующий шаг: опубликовать сервис rsvdata" in text, text)

            _err, text = await call(client, "register_target", {
                "url": f"http://127.0.0.1:{_dead_port()}/", "base": "s7dead",
                "check": True, "install": True})
            check("install dead publication: refused, no job",
                  "id задания" not in text and "Установка НЕ запущена" in text,
                  text)
            check("install dead publication: publication missing + next step",
                  "публикация не обнаружена" in text
                  and "выполнить публикацию" in text, text)

            # The per-target platform key + the stored connection unblock
            # the launch: the configurator variant has both inputs, so the
            # job starts and the chain reports the configurator branch
            # itself (it stops at the missing auth key — no secrets here).
            state["ping"] = "missing"
            _err, reg, text, _jid = await install_and_wait(client, {
                "url": sb, "base": "s7key", "check": True, "install": True,
                "platform": "8.3.27.2214",
                "connection": 'Srvr="Key-Host:1541";Ref="s7key";'})
            check("install with platform key: job starts despite dead ping",
                  _err is None and _jid and "ФОНОВЫМ ЗАДАНИЕМ" in reg, _err or reg)
            check("install with platform key: fallback echoed in answer",
                  "Платформа (фолбэк" in reg, reg)
            check("install with platform key: configurator branch reached",
                  "Вариант конфигуратора" in text
                  and "Установка не выполнена ни одним из способов." in text,
                  text)

            state.update(update="updated", ping="ok", root="ok")
            _err, text = await call(client, "register_target", {
                "url": sb, "base": "s7nc", "check": False, "install": True})
            check("install with check=False: ignored",
                  "ключ install игнорируется" in text and "Публикация" not in text,
                  text)

            for alias in ("s7up", "s7old", "s7auth", "s7nosvc", "s7dead",
                          "s7key", "s7nc"):
                await call(client, "unregister_target", {"base": alias})
            _err, _ = await call(client, "ping", {"base": "s7up"})
            check("cleanup: stage-7 targets removed",
                  _err and "не зарегистрирована" in _err, _err)
    finally:
        httpd.shutdown()

    return counters["pass"], counters["fail"]


if __name__ == "__main__":
    passed, failed = asyncio.run(run())
    print(f"stage7_check: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
