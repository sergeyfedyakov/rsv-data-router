"""One-command smoke run for the whole router build.

Picks free ports, starts two mock upstreams and the router in a throwaway
home dir, runs all stage acceptance checks (2-5) against them, then cleans
everything up. Exit code 0 = everything passed.

Usage (from the project root):
  .venv/Scripts/python tests/smoke.py        (Windows)
  .venv/bin/python tests/smoke.py            (Linux/macOS)
"""

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "src"))

from rsv_data_router import edt  # noqa: E402
import stage2_check  # noqa: E402
import stage3_check  # noqa: E402
import stage4_check  # noqa: E402
import stage5_check  # noqa: E402
import stage6_check  # noqa: E402
import stage7_check  # noqa: E402
import stage8_check  # noqa: E402
import stage9_check  # noqa: E402
import stage10_check  # noqa: E402
import stage11_check  # noqa: E402
import stage12_check  # noqa: E402
import stage13_check  # noqa: E402
import stage14_check  # noqa: E402

SMOKE_HOME = os.path.join(ROOT, ".smoke-home")


def free_ports(count):
    """Ask the OS for free ports (bind :0, read, close; tiny local race is fine)."""
    socks, ports = [], []
    for _ in range(count):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        ports.append(sock.getsockname()[1])
        socks.append(sock)
    for sock in socks:
        sock.close()
    return ports


def wait_port(port, label, timeout_s=20):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    print(f"SMOKE FAIL: {label} (port {port}) не поднялся за {timeout_s} с")
    return False


def main():
    shutil.rmtree(SMOKE_HOME, ignore_errors=True)
    os.makedirs(SMOKE_HOME)
    p1, p2, router_port = free_ports(3)
    procs = []
    try:
        for port in (p1, p2):
            procs.append(subprocess.Popen(
                [sys.executable, os.path.join(ROOT, "tests", "mock_upstream.py"), str(port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        # Fake EDT workspace + ibases.v8i live for the whole server lifetime:
        # the server resolves cwd -> base against this list (stage 4).
        fixtures = stage4_check.build_fixtures(SMOKE_HOME)
        # Stage 4's cleanup removes .test-edt (with the fixture ibases) when
        # it finishes; the server needs the list for the WHOLE run (name
        # lookup, platform fallbacks) — keep a copy outside .test-edt.
        server_ibases = os.path.join(SMOKE_HOME, "server-ibases.v8i")
        shutil.copyfile(fixtures["ibases"], server_ibases)
        # Dummy RSVData binary for stage 6 (help split + /bin route).
        dummy_cfe = os.path.join(SMOKE_HOME, "dummy.cfe")
        with open(dummy_cfe, "wb") as handle:
            handle.write(b"RSVDATA-BINARY-STAGE6-TEST")
        server_env = dict(os.environ, RSV_DATA_ROUTER_IBASES=server_ibases)
        server_env["PYTHONPATH"] = os.pathsep.join(
            [os.path.join(ROOT, "src"),
             server_env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
        # The install/removal chains must stay sandboxed: no real MCP:RSV
        # (it IS live on the dev machine), no real configurator roots and
        # no real EDT workspaces registry — empty fixture instead.
        server_env[edt.PROJECTS_JSON_ENV] = os.path.join(SMOKE_HOME,
                                                         "no-projects.json")
        server_env["RSV_DATA_ROUTER_1CV8_ROOTS"] = os.path.join(
            SMOKE_HOME, "no-roots")
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "rsv_data_router.server",
             "--port", str(router_port), "--home", SMOKE_HOME,
             "--rsvdatabinary", dummy_cfe, "--rsvmcp", ""],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=server_env))

        if not (wait_port(p1, "mock1") and wait_port(p2, "mock2")
                and wait_port(router_port, "router")):
            return 1

        url = f"http://127.0.0.1:{router_port}/mcp"
        all_ok = True
        for module in (stage2_check, stage3_check, stage4_check, stage5_check,
                       stage6_check, stage7_check, stage8_check, stage9_check,
                       stage10_check, stage11_check, stage12_check,
                       stage13_check, stage14_check):
            print(f"--- {module.__name__} ---")
            passed, failed = asyncio.run(module.run(url, SMOKE_HOME, (p1, p2)))
            print(f"{module.__name__}: {passed} passed, {failed} failed")
            all_ok = all_ok and failed == 0
        print("SMOKE:", "OK" if all_ok else "FAILED")
        return 0 if all_ok else 1
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(SMOKE_HOME, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
