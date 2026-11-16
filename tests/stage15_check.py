"""Stage-15 acceptance checks: file-IB targets and the ibsrv manager.

Offline checks for the file-target model (targets.json kind=file, File=
connection, edt.file_path) and for the ibsrv lifecycle. The stand-alone
server itself is not available in CI, so start/stop are exercised against a
tiny Python HTTP stub injected into the manager's spawn step: the real
command building, pid/state files, readiness wait and termination are all
the production code paths.
"""

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from rsv_data_router import edt, ibsrv, targets as targets_mod  # noqa: E402

STUB = '''import http.server, socketserver, sys

args = sys.argv[1:]
port, base = 0, ""
for index, item in enumerate(args):
    if item == "--http-port":
        port = int(args[index + 1])
    elif item == "--http-base":
        base = args[index + 1]


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


socketserver.TCPServer(("127.0.0.1", port), Handler).serve_forever()
'''


async def run(url=None, home=None, ports=None, verbose=True):
    """Returns (passed, failed)."""
    home = home or os.path.join(ROOT, ".test-home")
    work = os.path.join(home, ".test-ibsrv")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)

    passed = failed = 0

    def check(label, condition, details=None):
        nonlocal passed, failed
        if condition:
            passed += 1
        else:
            failed += 1
            print(f"FAIL: {label}" + (f" -> {details}" if details is not None else ""))

    # ---- model: File= connections and file targets ----
    check("file_path: File= -> directory",
          edt.file_path('File="C:\\bases\\f";') == "C:\\bases\\f",
          edt.file_path('File="C:\\bases\\f";'))
    check("file_path: server connect -> None",
          edt.file_path('Srvr="h:1541";Ref="r";') is None)
    check("normalize_connection: File kept",
          edt.normalize_connection('File="C:\\bases\\f";') == 'File="C:\\bases\\f";',
          edt.normalize_connection('File="C:\\bases\\f";'))
    try:
        edt.upstream_url({"File": "C:\\bases\\f"})
        check("upstream_url: file IB refused", False, "no exception")
    except edt.EdtError as exc:
        check("upstream_url: file IB refused", "файловая" in str(exc), exc)

    store = targets_mod.TargetsStore(work)
    target, _ = store.register("fbase", None, kind="file",
                               file_path="C:\\bases\\f", ib_port=8314,
                               connection='File="C:\\bases\\f";')
    check("file target: url derived from port+alias",
          target.url == "http://127.0.0.1:8314/fbase", target.url)
    check("file target: is_file", target.is_file is True)
    check("file target: mcp_url", target.mcp_url
          == "http://127.0.0.1:8314/fbase/hs/rsvdata/mcp", target.mcp_url)
    cfg = targets_mod.Target.from_dict("fbase", {
        "kind": "file", "file_path": "C:\\bases\\f", "ib_port": 9001,
        "auth": {"type": "basic", "value": ""}})
    check("from_dict: file round-trip", cfg.is_file and cfg.file_path == "C:\\bases\\f"
          and cfg.url == "http://127.0.0.1:9001/fbase", cfg.url)
    check("as_dict: file keys", cfg.as_dict().get("kind") == "file"
          and cfg.as_dict().get("file_path") == "C:\\bases\\f"
          and cfg.as_dict().get("ib_port") == 9001, cfg.as_dict())
    try:
        targets_mod.Target.from_dict("bad", {"kind": "file"})
        check("from_dict: file without file_path rejected", False, "no error")
    except targets_mod.TargetError as exc:
        check("from_dict: file without file_path rejected", "file_path" in str(exc), exc)
    check("server target unchanged",
          targets_mod.Target.from_dict("s", {"url": "http://h/r"}).url == "http://h/r")

    # ---- ibsrv manager ----
    check("allocate_ports: distinct free ports",
          len(set(ibsrv.allocate_ports(4))) == 4, ibsrv.allocate_ports(4))
    check("publication_url", ibsrv.publication_url("base", 8314)
          == "http://127.0.0.1:8314/base")
    check("_pid_alive: own process", ibsrv._pid_alive(os.getpid()) is True)
    check("_pid_alive: bogus pid", ibsrv._pid_alive(999999) is False)

    stub = os.path.join(work, "ibsrv_stub.py")
    with open(stub, "w", encoding="utf-8") as handle:
        handle.write(STUB)
    os.environ[ibsrv.BINARY_ENV] = stub
    manager = ibsrv.IbsrvManager(work)
    real_port = ibsrv._free_port()

    def fake_spawn(command, alias):
        log_file = open(manager.log_path(alias), "a", encoding="utf-8")
        flags = 0x08000000 if os.name == "nt" else 0
        try:
            proc = subprocess.Popen([sys.executable, stub] + command[1:],
                                    cwd=manager.dir, stdout=log_file,
                                    stderr=subprocess.STDOUT, creationflags=flags)
        finally:
            log_file.close()
        return proc

    manager._spawn = fake_spawn
    file_target = targets_mod.Target("fbase", None, kind="file",
                                     file_path="C:\\bases\\f", ib_port=real_port)
    state = manager.start(file_target)
    check("ibsrv start: running state", state.get("port") == real_port
          and manager.is_running("fbase"), state)
    check("ibsrv start: state persisted", os.path.isfile(manager.state_path("fbase"))
          and os.path.isfile(manager.pid_path("fbase")))
    pub_path = manager.publication_config_path("fbase")
    check("ibsrv start: publication config written", os.path.isfile(pub_path), pub_path)
    with open(pub_path, encoding="utf-8") as handle:
        pub = handle.read()
    check("publication config: base + RSVData service",
          "base: /fbase" in pub and "name: RSVData" in pub
          and "root: rsvdata" in pub and "publish: true" in pub, pub)
    cmd = manager._command("ibsrv.exe", real_port, file_target, 9999, pub_path)
    check("ibsrv command: --config points at the publication file",
          "--config" in cmd and cmd[cmd.index("--config") + 1] == pub_path, cmd)
    check("ibsrv start: adopts a live instance",
          manager.start(file_target).get("pid") == state.get("pid"),
          manager.status("fbase"))
    manager.stop("fbase")
    check("ibsrv stop: not running", manager.is_running("fbase") is False)
    check("ibsrv stop: state cleared",
          not os.path.isfile(manager.state_path("fbase")))
    check("ibsrv stop: publication config removed",
          not os.path.isfile(pub_path), pub_path)
    check("ibsrv status format", manager.status("fbase") == {"alias": "fbase",
                                                             "running": False})

    # ---- ibcmd: file-target install/removal (offline) ----
    from rsv_data_router import installer  # noqa: E402

    check("_ibcmd_base_args: no creds -> no --user",
          installer._ibcmd_base_args(file_target, None, None)
          == ["config", "--db-path=C:\\bases\\f"],
          installer._ibcmd_base_args(file_target, None, None))
    check("_ibcmd_base_args: creds passed",
          installer._ibcmd_base_args(file_target, "Администратор", "")
          == ["config", "--db-path=C:\\bases\\f",
              "--user=Администратор", "--password="],
          installer._ibcmd_base_args(file_target, "Администратор", ""))

    # _run_ibcmd against a REAL process (the interpreter itself): the code
    # path (list args, DEVNULL stdin, output to a log, code check) is the
    # production one.
    log = os.path.join(work, "ibcmd.log")
    installer._run_ibcmd(sys.executable, ["-c", "print('hello-ibcmd')"],
                         log, "успех")
    check("_run_ibcmd: success writes the log",
          "hello-ibcmd" in installer._read_output_tail(log), log)
    try:
        installer._run_ibcmd(sys.executable,
                             ["-c", "import sys; sys.exit(3)"], log, "сбой")
        check("_run_ibcmd: non-zero exit raises", False, "no error")
    except installer.IbcmdError as exc:
        check("_run_ibcmd: non-zero exit raises", exc.returncode == 3, exc.returncode)

    # Orchestration: stop ibsrv -> load -> apply -> start again (no real
    # subprocess: _run_ibcmd is recorded).
    cfe = os.path.join(work, "RSVData.cfe")
    with open(cfe, "wb") as handle:
        handle.write(b"fake-cfe")
    os.environ[installer.IBCMD_BINARY_ENV] = ibcmd_stub = os.path.join(work, "ibcmd.py")
    calls = []
    real_run = installer._run_ibcmd

    def fake_run(binary, args, log_path, what, **_kw):
        calls.append((what, list(args)))

    installer._run_ibcmd = fake_run

    class FakeCtx:
        home = work
        rsvdata_path = cfe
        ibsrv = manager

        def ensure_file_server(self, target):
            return manager.start(target)

    fctx = FakeCtx()
    binary, _version, refusal = installer._ibcmd_for(file_target)
    check("_ibcmd_for: env override short-circuits", binary == ibcmd_stub
          and refusal is None, (binary, refusal))
    go, _ = installer.file_install_preflight(file_target, None)
    check("file_install_preflight: missing cfe refused", go is False)
    go, _ = installer.file_install_preflight(file_target, cfe)
    check("file_install_preflight: cfe + override -> go", go is True)

    # EDT route: an MCP:RSV address plus a stored EDT link is enough to
    # launch without ibcmd (the project may still turn out closed — the
    # chain falls back to ibcmd then, same best-effort as server targets).
    edt_target = targets_mod.Target("fedt", None, kind="file",
                                    file_path="C:\\bases\\f", ib_port=8300,
                                    edt={"workspace": "D:\\ws", "project": "DemoProject"})
    check("_edt_ready: link + address", installer._edt_ready("http://x", edt_target) is True)
    check("_edt_ready: no address", installer._edt_ready("", edt_target) is False)
    check("_edt_ready: no link", installer._edt_ready("http://x", file_target) is False)
    go, _ = installer.file_install_preflight(edt_target, cfe, "http://127.0.0.1:8770/mcp")
    check("file_install_preflight: EDT link + rsvmcp -> go", go is True)
    check("file_removal_preflight: EDT link + rsvmcp -> go",
          installer.file_removal_preflight(edt_target, "http://x")[0] is True)

    manager.start(file_target)
    installer.run_file_install_chain(fctx, file_target)
    check("file install: load then apply",
          [what for what, _args in calls]
          == ["загрузка .cfe", "применение расширения"], calls)
    check("file install: load argv",
          calls[0][1] == ["config", "--db-path=C:\\bases\\f", "load",
                          "--extension=RSVData", "--force",
                          installer.update_designer.ascii_copy(cfe)],
          calls[0][1])
    check("file install: ibsrv restarted", manager.is_running("fbase") is True)

    calls.clear()
    removed, _lines = installer.run_file_removal_chain(fctx, file_target)
    check("file removal: delete called and reported",
          removed is True and [what for what, _a in calls] == ["удаление расширения"],
          (removed, calls))
    check("file removal: delete argv",
          calls[0][1] == ["config", "--db-path=C:\\bases\\f", "extension",
                          "delete", "--name=RSVData"], calls[0][1])
    check("file removal: ibsrv stopped", manager.is_running("fbase") is False)

    # Active extension holding data: the platform refuses the delete, so the
    # chain deactivates it and retries (offline simulation of the live case).
    calls.clear()
    state = {"refused": False}

    def flaky_run(_binary, args, _log_path, what, **_kw):
        calls.append((what, list(args)))
        if what == "удаление расширения" and not state["refused"]:
            state["refused"] = True
            raise installer.IbcmdError("код 1", [
                "Расширение конфигурации 'RSVData' активно и содержит данные. "
                "Удаление расширений с данными допускается только после снятия "
                "признака активности."], 1)

    manager.start(file_target)
    installer._run_ibcmd = flaky_run
    removed, _lines = installer.run_file_removal_chain(fctx, file_target)
    check("file removal (active+data): deactivate then delete",
          removed is True and [what for what, _a in calls]
          == ["удаление расширения", "снятие признака активности",
              "удаление расширения"], calls)
    check("file removal (active+data): update argv",
          calls[1][1] == ["config", "--db-path=C:\\bases\\f", "extension",
                          "update", "--name=RSVData", "--active=no"],
          calls[1][1])
    manager.stop("fbase")

    installer._run_ibcmd = real_run
    os.environ.pop(installer.IBCMD_BINARY_ENV, None)
    os.environ.pop(ibsrv.BINARY_ENV, None)

    shutil.rmtree(work, ignore_errors=True)
    return passed, failed


if __name__ == "__main__":
    ok, bad = __import__("asyncio").run(run())
    print(f"stage15: {ok} passed, {bad} failed")
    sys.exit(1 if bad else 0)
