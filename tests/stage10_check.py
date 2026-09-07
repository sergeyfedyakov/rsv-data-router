"""Stage-10 acceptance checks: unregister_target(uninstall=True) chain.

/remove first (RSVData 1.3.6 self-removal) -> EDT (MCP:RSV disabled in
smoke) -> configurator (bogus binary in smoke). The endpoint answers
удалено | отключено | отклонено (mock modes ok | disabled | rejected);
404 means an extension older than 1.3.6. удалено/отключено — the target
record is deleted; anything else falls through to EDT/configurator and,
when they can't run either, the record STAYS with the reason and the next
step. Plain unregister_target (no uninstall) keeps its old one-line
behavior.

Needs a router launched with --rsvmcp "" and sandboxed env sources (the
smoke launcher sets both): SMOKE_URL (default http://127.0.0.1:8787/mcp),
home .test-home10.
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

PING_TEXT = "pong — stage10; расширение 1.3.6, хеш s10hash="


def make_handler(state):
    """state["remove"]: ok | disabled | rejected | missing (/remove behavior)."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _answer(self, status, body, content_type="application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            if self.path.endswith("/hs/rsvdata/mcp"):
                self._answer(200, json.dumps({
                    "jsonrpc": "2.0", "id": 1,
                    "result": {"content": [{"type": "text", "text": PING_TEXT}]},
                }).encode("utf-8"))
                return
            if self.path.endswith("/hs/rsvdata/remove"):
                mode = state["remove"]
                if mode == "missing":
                    self._answer(404, b"404 - not found", "text/plain")
                    return
                was = {"имя": "RSVData", "версия": "1.3.6", "хеш": "s10hash="}
                if mode == "disabled":
                    answer = {"результат": "отключено", "было": was,
                              "проблемы": ["базу держат активные сеансы"]}
                elif mode == "rejected":
                    answer = {"результат": "отклонено",
                              "проблемы": ["не удалось удалить расширение"]}
                else:
                    answer = {"результат": "удалено", "было": was, "проблемы": []}
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

    async def uninstall_and_wait(client, base, timeout_s=30):
        """uninstall=True now runs as a background job: the call answers
        at once with a job id, the chain report (and the record fate)
        arrives via job_status polls."""
        _err, text = await call(client, "unregister_target",
                                {"base": base, "uninstall": True})
        match = re.search(r"id задания ([0-9a-f]+)", text or "")
        if _err or not match:
            return _err, text
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            _e, status = await call(client, "job_status", {"job_id": match.group(1)})
            if status and "ВЫПОЛНЯЕТСЯ" not in status:
                return _err, status
            await asyncio.sleep(0.3)
        return _err, f"TIMEOUT waiting for job {match.group(1)}"

    state = {"remove": "ok"}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    sb = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        async with Client(url) as client:
            state["remove"] = "ok"
            await call(client, "register_target", {"url": sb, "base": "s10ok",
                                                   "check": False})
            _err, text = await uninstall_and_wait(client, "s10ok")
            check("remove ok: job started and finished",
                  _err is None and "ГОТОВО" in text, _err or text)
            check("remove ok: /remove deleted (was-version named)",
                  "Расширение удалено через /remove (была версия 1.3.6)" in text,
                  _err or text)
            check("remove ok: transition window named",
                  "Переходное окно ~25–30 с" in text, text)
            check("remove ok: target record deleted",
                  "Запись цели «s10ok» удалена из мапы" in text, text)
            _err, _ = await call(client, "ping", {"base": "s10ok"})
            check("remove ok: alias gone from the map",
                  _err and "не зарегистрирована" in _err, _err)

            state["remove"] = "disabled"
            await call(client, "register_target", {"url": sb, "base": "s10dis",
                                                   "check": False})
            _err, text = await uninstall_and_wait(client, "s10dis")
            check("remove disabled: not an error, disabled explained",
                  "ОТКЛЮЧЕНО" in text and "не загружается" in text, _err or text)
            check("remove disabled: later-removal hint",
                  "повторить unregister_target(uninstall=true)" in text, text)
            check("remove disabled: target record deleted",
                  "Запись цели «s10dis» удалена из мапы" in text, text)

            state["remove"] = "rejected"
            await call(client, "register_target", {"url": sb, "base": "s10rej",
                                                   "check": False})
            _err, text = await uninstall_and_wait(client, "s10rej")
            check("remove rejected: falls through to EDT",
                  "отклонила удаление" in text and "пробую через EDT" in text,
                  _err or text)
            check("remove rejected: EDT skipped (rsvmcp off)",
                  "адрес MCP:RSV не задан" in text, text)
            check("remove rejected: configurator refused, chain ends",
                  "Вариант конфигуратора недоступен" in text
                  and "Удаление не выполнено ни одним из способов" in text, text)
            check("remove rejected: record kept with next step",
                  "Запись цели «s10rej» СОХРАНЕНА" in text
                  and "uninstall=true" in text, text)
            _err, text = await call(client, "ping", {"base": "s10rej"})
            check("remove rejected: alias still routable",
                  _err is None and "pong" in (text or ""), _err or text)

            state["remove"] = "missing"
            await call(client, "register_target", {"url": sb, "base": "s10old",
                                                   "check": False})
            _err, text = await uninstall_and_wait(client, "s10old")
            check("remove missing (old extension): moves to EDT",
                  "Точки /remove нет" in text and "старее 1.3.6" in text, text)
            check("remove missing: record kept",
                  "Запись цели «s10old» СОХРАНЕНА" in text, text)

            await call(client, "register_target", {
                "url": f"http://127.0.0.1:{_dead_port()}/", "base": "s10dead",
                "check": False})
            # Preflight: no endpoint, no EDT link, no stored connection —
            # the removal is refused right in the answer, no job, the
            # record simply stays.
            _err, text = await call(client, "unregister_target",
                                    {"base": "s10dead", "uninstall": True})
            check("dead publication: refused before any job",
                  _err is None and "id задания" not in (text or "")
                  and "Удаление НЕ запущено" in (text or ""), _err or text)
            check("dead publication: publication missing + next step",
                  "публикация не обнаружена" in text
                  and "выполнить публикацию" in text, text)
            check("dead publication: url-registration cause named",
                  "регистрация только по url" in text
                  and "connection" in text, text)
            _err, text = await call(client, "list_targets", {})
            check("dead publication: record kept (still listed)",
                  _err is None and "s10dead" in (text or ""), _err or text)

            state["remove"] = "ok"
            await call(client, "register_target", {"url": sb, "base": "s10plain",
                                                   "check": False})
            _err, text = await call(client, "unregister_target", {"base": "s10plain"})
            check("plain uninstall=false: one line, no chain",
                  _err is None and "Цель «s10plain» удалена." in text
                  and "Удаление расширения" not in text, _err or text)

            for alias in ("s10rej", "s10old", "s10dead"):
                await call(client, "unregister_target", {"base": alias})
            _err, _ = await call(client, "ping", {"base": "s10rej"})
            check("cleanup: stage-10 targets removed",
                  _err and "не зарегистрирована" in _err, _err)
    finally:
        httpd.shutdown()

    return counters["pass"], counters["fail"]


if __name__ == "__main__":
    passed, failed = asyncio.run(run())
    print(f"stage10_check: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
