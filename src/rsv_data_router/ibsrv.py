"""Local publication of a file infobase via the stand-alone server ibsrv.

A file IB has no web publication of its own: the router starts a per-base
ibsrv instance (the platform's built-in stand-alone server), points it at the
base directory and talks to the resulting local publication over the usual
HTTP transport. One instance per base; the router allocates the ports because
ibsrv does not pick free ones itself and its defaults (1541, 1560:1591)
collide with the 1C cluster. The instance is stopped only explicitly and on
router shutdown — there is no idle auto-stop (consecutive calls must not
restart the server each time).

State lives under <home>/ibsrv: <alias>.json (pid, port, url, binary),
<alias>.pid (the pid alone, for humans), <alias>.log (server output) and
<alias>.publication.yaml (the run-time config passed to ibsrv).

The config file is required: ibsrv does NOT publish HTTP services coming
from EXTENSIONS by default (a bare POST /hs/rsvdata/mcp answers 503 from the
platform), so the RSVData service is listed explicitly and passed with
--config. Services of the main configuration are served without it.
"""

import atexit
import ctypes
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request

from . import update_designer

BINARY_ENV = "RSV_DATA_ROUTER_IBSRV"
READY_TIMEOUT_S = 90
READY_POLL_S = 0.5


class IbsrvError(Exception):
    """ibsrv management failure with a message safe to show to the agent."""


def ibsrv_binary(version=None, roots=None):
    """Path to ibsrv.exe next to the 1cv8.exe of the base's platform build.

    RSV_DATA_ROUTER_IBSRV overrides the lookup (tests, exotic layouts). The
    build is resolved exactly like the configurator (same version, single
    installed as a fallback); a missing ibsrv beside the found 1cv8.exe is an
    error, not a silent downgrade.
    """
    override = os.environ.get(BINARY_ENV)
    if override:
        return override
    try:
        designer = update_designer.find_designer(version, roots)
    except SystemExit as exc:
        raise IbsrvError(f"не удалось найти платформу для файловой базы: {exc}") from exc
    candidate = os.path.join(os.path.dirname(designer), "ibsrv.exe")
    if not os.path.isfile(candidate):
        raise IbsrvError(f"рядом с конфигуратором нет ibsrv.exe: {candidate} — "
                         "нужна полная установка платформы 8.3")
    return candidate


def _free_port():
    """One free TCP port on 127.0.0.1 (bind to 0, read, close)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def allocate_ports(count):
    """Reserve `count` distinct free ports (all sockets held until the end)."""
    socks, ports = [], []
    try:
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            socks.append(sock)
            ports.append(sock.getsockname()[1])
    finally:
        for sock in socks:
            sock.close()
    return ports


def publication_url(alias, port):
    """The base's local publication root on the allocated port."""
    return f"http://127.0.0.1:{int(port)}/{alias}"


def _pid_alive(pid):
    """Is the process alive? (os.kill(pid, 0) would KILL it on Windows.)"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return False
    finally:
        kernel32.CloseHandle(handle)


def _terminate(pid):
    """Terminate a process tree (taskkill on Windows, SIGTERM elsewhere)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, check=False)
        return
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


class IbsrvManager:
    """Start/stop/status of the per-base ibsrv instances."""

    def __init__(self, home_dir, log=None):
        self.dir = os.path.join(home_dir, "ibsrv")
        os.makedirs(self.dir, exist_ok=True)
        self.log = log
        self._procs = {}
        atexit.register(self.stop_all)

    def _note(self, message):
        if self.log is not None:
            self.log.write(message)

    def state_path(self, alias):
        return os.path.join(self.dir, f"{alias}.json")

    def pid_path(self, alias):
        return os.path.join(self.dir, f"{alias}.pid")

    def log_path(self, alias):
        return os.path.join(self.dir, f"{alias}.log")

    def publication_config_path(self, alias):
        return os.path.join(self.dir, f"{alias}.publication.yaml")

    def _read_state(self, alias):
        try:
            with open(self.state_path(alias), encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def _write_state(self, alias, state):
        with open(self.state_path(alias), "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
        with open(self.pid_path(alias), "w", encoding="utf-8") as handle:
            handle.write(str(state["pid"]))

    def _clear_state(self, alias):
        for path in (self.state_path(alias), self.pid_path(alias),
                     self.publication_config_path(alias)):
            try:
                os.remove(path)
            except OSError:
                pass

    def _write_publication_config(self, alias):
        """Write ibsrv's run-time config listing the extension HTTP service.

        ibsrv does not publish extension services by default, so RSVData is
        declared explicitly (schema: `http` -> list -> `http-services` ->
        `service` -> name/root/publish; verified live 07.10.2026). Returns
        the config path.
        """
        path = self.publication_config_path(alias)
        content = (
            "http:\n"
            f"  - base: /{alias}\n"
            "    http-services:\n"
            "      service:\n"
            "        - name: RSVData\n"
            "          root: rsvdata\n"
            "          publish: true\n"
        )
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path

    def is_running(self, alias):
        """True when a live ibsrv for this alias is recorded."""
        state = self._read_state(alias)
        return bool(state) and _pid_alive(state.get("pid"))

    def status(self, alias):
        """State dict (running/pinned fields) for logs and server_status."""
        state = self._read_state(alias)
        if not state:
            return {"alias": alias, "running": False}
        state = dict(state)
        state["alias"] = alias
        state["running"] = _pid_alive(state.get("pid"))
        return state

    def _command(self, binary, port, target, monitor_port, config_path):
        return [
            binary, "--data", os.path.join(self.dir, f"{target.alias}-data"),
            "--db-path", target.file_path,
            "--http-address", "localhost", "--http-port", str(port),
            "--http-base", f"/{target.alias}", "--name", target.alias,
            "--id", "auto",
            "--monitor-port", str(monitor_port),
            "--config", config_path,
            "--disable-direct", "--disable-ssh",
        ]

    def _spawn(self, command, alias):
        log_file = open(self.log_path(alias), "a", encoding="utf-8")
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(command, cwd=self.dir, stdout=log_file,
                                    stderr=subprocess.STDOUT, creationflags=flags)
        finally:
            log_file.close()
        return proc

    def _wait_ready(self, port, timeout=READY_TIMEOUT_S):
        """Wait until the server answers on the port (any HTTP status is up)."""
        url = f"http://127.0.0.1:{int(port)}/"
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(url, timeout=2)
                return True
            except urllib.error.HTTPError:
                return True
            except (urllib.error.URLError, OSError) as exc:
                last = exc
            time.sleep(READY_POLL_S)
        self._note(f"ibsrv не поднялся за {timeout} с: {last}")
        return False

    def _launch(self, target, binary, port, monitor_port):
        """Spawn the process, record its state; returns the state dict."""
        config_path = self._write_publication_config(target.alias)
        command = self._command(binary, port, target, monitor_port, config_path)
        proc = self._spawn(command, target.alias)
        self._procs[target.alias] = proc
        state = {"pid": proc.pid, "port": port, "monitor_port": monitor_port,
                 "url": publication_url(target.alias, port), "binary": binary,
                 "file_path": target.file_path,
                 "publication_config": config_path}
        self._write_state(target.alias, state)
        return state

    def start(self, target, platform_version=None):
        """Start (or adopt) the base's ibsrv; returns its state dict.

        A live instance is adopted as-is; a failed readiness wait kills the
        process and raises IbsrvError with the log path.
        """
        state = self.status(target.alias)
        if state.get("running"):
            return state
        version = platform_version or target.platform
        if not version:
            # A file base's build is not stored on the record: resolve it from
            # the EDT link (.infobase-binding) so ibsrv uses the exact platform
            # without an explicit platform key (ibases Version is often absent
            # for file bases). None -> the single-installed fallback applies.
            try:
                version = update_designer.base_platform_version(target)
            except Exception:  # noqa: BLE001 — resolve_designer names it then
                version = None
        binary = ibsrv_binary(version)
        port = target.ib_port or _free_port()
        self._note(f"ibsrv {target.alias}: запуск ({binary}, порт {port})")
        state = self._launch(target, binary, port, _free_port())
        if not self._wait_ready(port):
            self.stop(target.alias)
            raise IbsrvError(
                f"ibsrv для «{target.alias}» не поднялся — журнал: "
                f"{self.log_path(target.alias)} (порт {port} мог быть занят)")
        self._note(f"ibsrv {target.alias}: поднят, {state['url']}")
        return state

    def stop(self, alias):
        """Stop the instance (explicit command or router shutdown)."""
        state = self._read_state(alias)
        proc = self._procs.pop(alias, None)
        if proc is not None and proc.poll() is None:
            _terminate(proc.pid)
        elif state and _pid_alive(state.get("pid")):
            _terminate(state["pid"])
        self._clear_state(alias)
        if state:
            self._note(f"ibsrv {alias}: остановлен")

    def stop_all(self):
        """Stop every instance this router started (atexit hook)."""
        try:
            names = [name[:-5] for name in os.listdir(self.dir)
                     if name.endswith(".json")]
        except OSError:
            return
        for alias in names:
            if self.is_running(alias):
                self.stop(alias)
