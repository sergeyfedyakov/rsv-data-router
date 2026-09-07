"""Stage-6 acceptance checks: help split + RSVData binary distribution.

help (no topic included) must go to the selected base, help_router answers
with the router overview; get_rsvdata reports the --rsvdatabinary path plus the
/bin/rsvdata.cfe download link; GET BIN_PATH serves the file bytes (outside
the /mcp branch, token-protected via the same gate in --network mode).

Standalone (defaults): router on :8787 with --rsvdatabinary pointing at a dummy
file, mocks on :8791/:8792, home .test-home6. Via tests/smoke.py: run(url,
home, ports) — the router must have been started with --rsvdatabinary.
"""

import asyncio
import os
import sys
import urllib.request
import urllib.error
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "src"))

from rsv_data_router import tools  # noqa: E402
from fastmcp import Client  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402


async def run(url=None, home=None, ports=None, verbose=True):
    """Returns (passed, failed)."""
    url = url or os.environ.get("SMOKE_URL") or "http://127.0.0.1:8787/mcp"
    home = home or os.environ.get("SMOKE_HOME") or os.path.join(ROOT, ".test-home6")
    p1, _p2 = ports or (8791, 8792)
    dummy = os.path.join(home, "dummy.cfe")
    if not os.path.exists(dummy):
        with open(dummy, "wb") as handle:
            handle.write(b"RSVDATA-BINARY-STAGE6-TEST")
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

    # -- unit: unset --rsvdatabinary -> instructive report, no crash --
    report = tools.rsvdata_report(SimpleNamespace(rsvdata_path=None, serve_binary=True))
    check("report unset: instructive",
          "--rsvdatabinary" in report and "не выдан" in report, report)

    async with Client(url) as client:
        # Own target (stage 2/3 now clean theirs): help_router shows the
        # target list, and help forwarding needs a live upstream.
        err, text = await call(client, "register_target", {
            "url": f"http://127.0.0.1:{p1}/mcp", "base": "mock1", "check": False})
        check("setup: mock1 registered", not err, err or text)

        found = sorted(t.name for t in await client.list_tools())
        check("new tools registered", "help_router" in found and "get_rsvdata" in found,
              found)

        _err, text = await call(client, "help_router", {})
        check("help_router: overview with tools and targets",
              not _err and "РОУТЕР" in text and "help_router" in text
              and "get_rsvdata" in text and "/bin/rsvdata.cfe" in text
              and "Цели (" in text, _err or text)

        _err, text = await call(client, "get_rsvdata", {})
        check("get_rsvdata: path + link + recipe",
              not _err and dummy in text and "/bin/rsvdata.cfe" in text
              and "importProject" in text and "409" in text, _err or text)

        _err, text = await call(client, "get_rsvdata", {})
        check("get_rsvdata: no binary content in reply",
              "RSVDATA-BINARY-STAGE6-TEST" not in text, text)

        _err, text = await call(client, "help", {"base": "mock1"})
        check("help no topic: forwarded to base, not router text",
              not _err and f"mock{p1}/help" in text and "РОУТЕР" not in text, _err or text)

        _err, text = await call(client, "help", {"base": "mock1", "topic": "query"})
        check("help topic=query: forwarded with topic",
              not _err and f"mock{p1}/help" in text and "topic" in text, _err or text)

        err, _ = await call(client, "ping", {"base": "no-such-target"})
        check("unknown base error mentions help_router",
              err and "не зарегистрирована" in err and "help_router" in err, err)

        # Clean up so later stages / reruns start with a clean map.
        err, _ = await call(client, "unregister_target", {"base": "mock1"})
        check("cleanup: mock1 removed", not err, err)

    # -- http: binary served outside the /mcp branch --
    base = url.rsplit("/mcp", 1)[0]
    with urllib.request.urlopen(f"{base}/bin/rsvdata.cfe", timeout=10) as response:
        body = response.read()
        check("GET /bin/rsvdata.cfe: exact bytes", body == b"RSVDATA-BINARY-STAGE6-TEST", body)
        check("GET /bin/rsvdata.cfe: octet-stream + attachment",
              response.headers.get("Content-Type") == "application/octet-stream"
              and "attachment" in response.headers.get("Content-Disposition", ""),
              dict(response.headers))
    try:
        urllib.request.urlopen(f"{base}/bin/other.cfe", timeout=10)
        check("GET unknown /bin path: 404", False, "no exception")
    except urllib.error.HTTPError as exc:
        check("GET unknown /bin path: 404", exc.code == 404, exc.code)

    return counters["pass"], counters["fail"]


if __name__ == "__main__":
    passed, failed = asyncio.run(run())
    print(f"stage6_check: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
