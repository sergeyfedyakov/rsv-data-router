"""Stage-12 acceptance checks: background jobs (install/removal) + job_status.

register_target(install=True) and unregister_target(uninstall=True) always
run as background jobs: the call answers at once with a job id, the chain
report is polled via job_status(job_id). Checked here: the job id appears
in the immediate answer; the poll turns from ВЫПОЛНЯЕТСЯ into the final
report; a slowed-down mock keeps the job ВЫПОЛНЯЕТСЯ with progress; the
job list (no id) names the finished job; an unknown id gets the restart
hint; a second install over a running job is refused with the running id;
re-registration of a known alias keeps the STORED url (a mistyped url in
the call is ignored) and the alias is adopted for the same url root; the
platform fallback key is stored once and survives re-registration.

Needs a router launched with --rsvmcp "" and sandboxed env sources (the
smoke launcher sets both): SMOKE_URL (default http://127.0.0.1:8787/mcp),
home .test-home12.
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

PING_TEXT = "pong — stage12; расширение 1.3.6, хеш s12hash="


def make_handler(state):
    """state["delay"]: seconds to sleep in /update (slow job test)."""

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
            if self.path.endswith("/hs/rsvdata/update"):
                delay = float(state.get("delay") or 0)
                if delay:
                    # Hold the worker thread, not the event loop: other
                    # calls must keep working while the job runs.
                    threading.Event().wait(delay)
                answer = {"результат": "обновлено",
                          "было": {"имя": "RSVData", "версия": "1.3.5"},
                          "стало": {"имя": "RSVData", "версия": "1.3.6",
                                    "хеш": "s12hash="}}
                self._answer(200, json.dumps(answer, ensure_ascii=False).encode("utf-8"))
                return
            if self.path.endswith("/hs/rsvdata/remove"):
                answer = {"результат": "удалено",
                          "было": {"имя": "RSVData", "версия": "1.3.6",
                                   "хеш": "s12hash="},
                          "проблемы": []}
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

    async def poll(client, job_id, timeout_s=30):
        """Poll job_status until the job leaves ВЫПОЛНЯЕТСЯ."""
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            _e, status = await call(client, "job_status", {"job_id": job_id})
            if status and "ВЫПОЛНЯЕТСЯ" not in status:
                return status
            await asyncio.sleep(0.3)
        return f"TIMEOUT waiting for job {job_id}"

    state = {"delay": 0}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    sb = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        async with Client(url) as client:
            # 1. Fast job: id in the answer, poll turns into the report.
            _err, text = await call(client, "register_target", {
                "url": sb, "base": "s12job", "check": True, "install": True})
            match = re.search(r"id задания ([0-9a-f]+)", text or "")
            check("install: immediate answer carries job id",
                  _err is None and match and "ФОНОВЫМ ЗАДАНИЕМ" in text
                  and "job_status" in text, _err or text)
            job_id = match.group(1) if match else ""
            check("install: registration line present, chain NOT inline",
                  _err is None and "зарегистрирована" in text
                  and "Расширение обновлено" not in text, _err or text)
            status = await poll(client, job_id)
            check("install poll: job finished with the full chain report",
                  "ГОТОВО" in status
                  and "Расширение обновлено через /update: 1.3.5 -> 1.3.6" in status
                  and "Проверка связи: ок" in status, status)

            # 2. Slow job: running status shows progress; the event loop
            #    serves other calls meanwhile.
            state["delay"] = 3
            _err, text = await call(client, "register_target", {
                "url": sb, "base": "s12slow", "check": True, "install": True})
            slow_id = (re.search(r"id задания ([0-9a-f]+)", text or "") or [None, ""])[1]
            check("slow job: second install started", _err is None and slow_id, _err or text)
            _e, running = await call(client, "job_status", {"job_id": slow_id})
            check("slow job: poll shows ВЫПОЛНЯЕТСЯ",
                  _e is None and "ВЫПОЛНЯЕТСЯ" in running, _e or running)
            _e, other = await call(client, "list_targets", {})
            check("slow job: event loop stays responsive",
                  _e is None and "s12slow" in (other or ""), _e or other)
            _e, second = await call(client, "register_target", {
                "url": sb, "base": "s12slow", "check": True, "install": True})
            check("slow job: duplicate install refused with running id",
                  _e is None and "уже идёт задание" in second
                  and slow_id in second and "НЕ запущена" in second, _e or second)
            status = await poll(client, slow_id)
            state["delay"] = 0
            check("slow job: finishes with the report",
                  "ГОТОВО" in status and "Расширение обновлено" in status, status)

            # 3. Job list (no id): both jobs listed, newest first.
            _e, listing = await call(client, "job_status", {})
            check("job list: names jobs with status",
                  _e is None and slow_id in listing and job_id in listing
                  and "готово" in listing, _e or listing)

            # 4. Removal as a job: id in the answer, record fate in the poll.
            _err, text = await call(client, "unregister_target",
                                    {"base": "s12job", "uninstall": True})
            rmatch = re.search(r"id задания ([0-9a-f]+)", text or "")
            check("removal: immediate answer carries job id",
                  _err is None and rmatch and "ФОНОВЫМ ЗАДАНИЕМ" in text, _err or text)
            status = await poll(client, rmatch.group(1) if rmatch else "")
            check("removal poll: extension removed, record deleted",
                  "ГОТОВО" in status
                  and "Расширение удалено через /remove" in status
                  and "Запись цели «s12job» удалена из мапы" in status, status)

            # 5. Unknown id: restart hint.
            _e, text = await call(client, "job_status", {"job_id": "deadbeef"})
            check("unknown id: instructive answer",
                  _e is None and "неизвестно" in text
                  and "перезапуск" in text and "повторить операцию" in text,
                  _e or text)

            # 6. Re-registration keeps the STORED url: a stale url in the
            #    call is ignored; the alias is adopted for the same root.
            _e, text = await call(client, "register_target", {
                "url": f"http://127.0.0.1:{_dead_port()}/", "base": "s12slow"})
            check("re-registration: stored url wins over the call",
                  _e is None and sb in (text or "")
                  and "Повторная регистрация" in text, _e or text)
            _e, listing = await call(client, "list_targets", {})
            check("re-registration: no duplicate alias",
                  _e is None and listing.count("s12slow") == 1, _e or listing)

            # 7. Platform fallback key: echoed at registration, kept on
            #    re-registration without the key (uninstall takes it later).
            _e, text = await call(client, "register_target", {
                "url": sb, "base": "s12pf", "check": False,
                "platform": "8.3.27.2214"})
            check("platform: echoed at registration",
                  _e is None and "Платформа (фолбэк" in text
                  and "8.3.27.2214" in text, _e or text)
            _e, text = await call(client, "register_target", {"base": "s12pf"})
            check("platform: kept on re-registration without the key",
                  _e is None and "8.3.27.2214" in text
                  and "Повторная регистрация" in text, _e or text)

            # 8. Register by the base's EXACT name in the list (ibases.v8i
            #    fixture: «Серверная база дымовых тестов», Ref smoke_ref):
            #    url comes from its Connect, the alias is the Ref, and the
            #    Connect is stored as the connection string.
            _e, text = await call(client, "register_target", {
                "base": "Серверная база дымовых тестов", "check": False})
            check("register by exact list name: resolved from ibases",
                  _e is None and "адрес из списка баз" in (text or "")
                  and "smoke_ref" in text
                  and "cluster-host.demo/smoke_ref" in text, _e or text)
            check("register by exact list name: name quoted, alias is Ref",
                  "Серверная база дымовых тестов»" in text
                  and "алиас — Ref" in text, _e or text)
            check("register by exact list name: connection saved from Connect",
                  _e is None
                  and "Строка подключения сохранена (из списка баз)" in text
                  and 'Srvr="Cluster-Host.demo:1541";Ref="smoke_ref";' in text,
                  _e or text)
            _e, text = await call(client, "register_target", {
                "base": "нет такой базы в списке", "check": False})
            check("register by unknown name: instructive error",
                  _e and "точное название базы из списка баз" in _e, _e)

            # 9. The stored connection string: filled by the connection
            #    argument (normalized, credentials dropped), survives
            #    re-registration without it, replaced by a new one.
            _e, text = await call(client, "register_target", {
                "base": "s12conn", "check": False,
                "connection": 'Srvr="Conn-Host:2041";Ref="s12conn";Usr="u";Pwd="p";'})
            check("connection argument: normalized form saved (no creds)",
                  _e is None
                  and "Строка подключения сохранена (из параметра connection)" in text
                  and 'Srvr="Conn-Host:2041";Ref="s12conn";' in text
                  and "Usr" not in text and "Pwd" not in text
                  and "http://conn-host/s12conn" in text, _e or text)
            _e, text = await call(client, "register_target",
                                  {"base": "s12conn", "check": False})
            check("re-registration without connection: stored value kept",
                  _e is None and "Повторная регистрация" in text
                  and "Строка подключения" not in text, _e or text)
            _e, text = await call(client, "register_target", {
                "url": f"http://127.0.0.1:{_dead_port()}/", "base": "s12conn",
                "check": False,
                "connection": 'Srvr="Conn-Host2:2042";Ref="s12conn";'})
            check("re-registration with connection: stored, url still from record",
                  _e is None and "http://conn-host/s12conn" in text
                  and "переданная connection сохранена в записи" in text
                  and 'Srvr="Conn-Host2:2042";Ref="s12conn";' in text, _e or text)

            # Cleanup.
            for alias in ("s12slow", "s12pf", "smoke_ref", "s12conn"):
                await call(client, "unregister_target", {"base": alias})
            _e, _ = await call(client, "ping", {"base": "s12slow"})
            check("cleanup: stage-12 targets removed",
                  _e and "не зарегистрирована" in _e, _e)
    finally:
        httpd.shutdown()

    return counters["pass"], counters["fail"]


if __name__ == "__main__":
    passed, failed = asyncio.run(run())
    print(f"stage12_check: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
