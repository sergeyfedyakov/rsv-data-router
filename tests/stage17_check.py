"""Stage-17 acceptance checks: async data jobs (async_mode) + job_status.

execute_query / evaluate_bsl / execute_bsl with async_mode=true run as
background jobs ("выполнение"): the call answers at once with a job id,
the FULL upstream answer lands in the job report, polled via
job_status(job_id). Checked here: the id in the immediate answer; the poll
turns into ГОТОВО with the upstream answer; parallel execute jobs on one
target are both allowed and both finish; the job list (no id) names the
kind; a dead target yields ОШИБКА with the classified error block; a bad
base refuses right away (no doomed job); an install over a RUNNING execute
job is refused with its id; the sync path (async_mode omitted) stays
inline.

Needs a router launched with --rsvmcp "" and sandboxed env sources (the
smoke launcher sets both): SMOKE_URL (default http://127.0.0.1:8787/mcp).
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

UPSTREAM_TEXT = "pong — stage17; расширение 1.3.9, хеш s17hash="


def make_handler(state):
    """state["delay"]: seconds to sleep before the /mcp answer (slow job)."""

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
                delay = float(state.get("delay") or 0)
                if delay:
                    # Hold the data-job worker thread, not the event loop.
                    threading.Event().wait(delay)
                self._answer(200, json.dumps({
                    "jsonrpc": "2.0", "id": 1,
                    "result": {"content": [{"type": "text", "text": UPSTREAM_TEXT}]},
                }).encode("utf-8"))
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
            _e, text = await call(client, "register_target",
                                  {"url": sb, "base": "exec17", "check": False})
            check("register exec17", _e is None and "зарегистрирована" in text,
                  _e or text)

            # 1. Fast async call: id in the answer, full upstream answer in
            #    the finished job report; the call itself did NOT wait.
            _e, text = await call(client, "execute_query", {
                "query": "ВЫБРАТЬ ПЕРВЫЕ 1 Наименование ИЗ Справочник.Номенклатура",
                "async_mode": True})
            match = re.search(r"id задания ([0-9a-f]+)", text or "")
            check("async query: immediate answer carries job id",
                  _e is None and match and "ФОНОВЫМ ЗАДАНИЕМ" in text
                  and "job_status" in text, _e or text)
            job_id = match.group(1) if match else ""
            check("async query: query text NOT executed inline",
                  _e is None and UPSTREAM_TEXT not in text, _e or text)
            status = await poll(client, job_id)
            check("async query poll: ГОТОВО with the upstream answer",
                  "ГОТОВО" in status and UPSTREAM_TEXT in status, status)
            check("async query poll: kind named",
                  "выполнение цели «exec17»" in status, status)

            # 2. Sync path (async_mode omitted) stays inline.
            _e, text = await call(client, "execute_query", {
                "query": "ВЫБРАТЬ ПЕРВЫЕ 1 Код ИЗ Справочник.Номенклатура"})
            check("sync query: inline answer unchanged",
                  _e is None and UPSTREAM_TEXT in text, _e or text)

            # 3. Parallel async calls on one target: both allowed, both done.
            _e1, t1 = await call(client, "evaluate_bsl",
                                 {"code": "ТекущаяДата()", "async_mode": True})
            _e2, t2 = await call(client, "execute_bsl",
                                 {"code": "РезультатВыполнения = 1;", "async_mode": True})
            id1 = (re.search(r"id задания ([0-9a-f]+)", t1 or "") or [None, ""])[1]
            id2 = (re.search(r"id задания ([0-9a-f]+)", t2 or "") or [None, ""])[1]
            check("parallel: both jobs accepted",
                  _e1 is None and _e2 is None and id1 and id2 and id1 != id2,
                  (_e1, _e2, t1, t2))
            s1 = await poll(client, id1)
            s2 = await poll(client, id2)
            check("parallel: both finished with the answer",
                  "ГОТОВО" in s1 and UPSTREAM_TEXT in s1
                  and "ГОТОВО" in s2 and UPSTREAM_TEXT in s2, (s1, s2))

            # 4. Job list (no id): the execute kind is named.
            _e, listing = await call(client, "job_status", {})
            check("job list: names выполнение jobs",
                  _e is None and job_id in listing and "выполнение" in listing,
                  _e or listing)

            # 5. Dead target: the job finishes ОШИБКА with the classified
            #    block (AGENT_NOTE inside), not a hung running state.
            _e, text = await call(client, "register_target", {
                "url": f"http://127.0.0.1:{_dead_port()}/", "base": "dead17",
                "check": False})
            check("register dead17", _e is None, _e or text)
            _e, text = await call(client, "execute_query", {
                "query": "ВЫБРАТЬ 1", "base": "dead17", "async_mode": True})
            dead_id = (re.search(r"id задания ([0-9a-f]+)", text or "") or [None, ""])[1]
            check("dead target: job id returned", dead_id, _e or text)
            status = await poll(client, dead_id)
            check("dead target: ОШИБКА with classified block",
                  "ОШИБКА" in status
                  and "покажьте это сообщение пользователю" in status, status)

            # 6. Bad base: refused right away, no doomed job.
            _e, text = await call(client, "execute_query", {
                "query": "ВЫБРАТЬ 1", "base": "нет такой цели",
                "async_mode": True})
            check("bad base: inline refusal, no job id",
                  _e and "id задания" not in (text or ""), (_e, text))

            # 7. Install over a RUNNING execute job: refused with its id.
            #    base is explicit — with two registered targets the no-base
            #    route refuses right away (that fail-fast is checked in #6).
            state["delay"] = 3
            _e, text = await call(client, "execute_query", {
                "query": "ВЫБРАТЬ 2", "base": "exec17", "async_mode": True})
            slow_id = (re.search(r"id задания ([0-9a-f]+)", text or "") or [None, ""])[1]
            _e, text = await call(client, "register_target", {
                "url": sb, "base": "exec17", "check": True, "install": True})
            check("install over running execute: refused with its id",
                  _e is None and "уже идёт задание" in (text or "")
                  and slow_id in (text or ""), _e or text)
            status = await poll(client, slow_id)
            check("slow execute still finishes",
                  "ГОТОВО" in status and UPSTREAM_TEXT in status, status)
            state["delay"] = 0

            # Cleanup.
            for alias in ("exec17", "dead17"):
                await call(client, "unregister_target",
                           {"base": alias, "uninstall": False})
            _e, _ = await call(client, "ping", {"base": "exec17"})
            check("cleanup: stage-17 targets removed",
                  _e and "не зарегистрирована" in _e, _e)
    finally:
        httpd.shutdown()

    return counters["pass"], counters["fail"]


if __name__ == "__main__":
    passed, failed = asyncio.run(run())
    print(f"stage17_check: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
