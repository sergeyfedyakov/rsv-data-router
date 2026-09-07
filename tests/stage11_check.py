"""Stage-11 acceptance checks: EDT project lookup across running instances.

The router talks to ONE MCP:RSV server, but several EDT instances can be
open side by side; list_workspace_projects answers with the connected
instance's projects at the top level and the neighbors in
otherEdtInstances (no location/isOpen there). installer._find_edt_project
must route the follow-up edit_metadata to the right instance via the
edtWorkspace parameter. These checks are OFFLINE: synthetic payloads,
no servers (same shape as stage 8).

SMOKE_URL/home/ports are accepted for the uniform smoke call signature.
"""

import asyncio
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "src"))

from rsv_data_router import installer  # noqa: E402

CONV_WS = "D:\\dev\\CONV"
BUH_WS = "D:\\dev\\BUH"
OTHER_WS = "D:\\dev\\OTHER"


def _target(workspace, project):
    return SimpleNamespace(edt={"workspace": workspace, "project": project})


def _top(name, workspace, is_open=True):
    return {"name": name, "isOpen": is_open,
            "location": os.path.join(workspace, name)}


def _neighbor(workspace, short, port, names):
    return {"workspace": workspace, "port": port, "edtWorkspace": short,
            "projects": [{"name": n, "projectType": "configuration"}
                         for n in names]}


async def run(url=None, home=None, ports=None, verbose=True):
    """Returns (passed, failed)."""
    counters = {"pass": 0, "fail": 0}

    def check(name, cond, extra=""):
        if cond:
            counters["pass"] += 1
            if verbose:
                print(f"PASS  {name}")
        else:
            counters["fail"] += 1
            print(f"FAIL  {name}: {extra}")

    buh = _target(BUH_WS, "БП")

    # Connected instance: the project sits at the top level, no addressing.
    data = {"projects": [_top("КД", CONV_WS), _top("БП", BUH_WS)],
            "otherEdtInstances": []}
    param, port, miss = installer._find_edt_project(data, buh)
    check("connected instance: no edtWorkspace, no miss",
          param is None and port is None and miss is None, (param, port, miss))

    # Connected instance, project present but closed -> miss, says "closed".
    data = {"projects": [_top("БП", BUH_WS, is_open=False)],
            "otherEdtInstances": []}
    param, port, miss = installer._find_edt_project(data, buh)
    check("connected instance, closed project: miss names it",
          param is None and miss and "закрыт" in miss, (param, miss))

    # Neighbor instance: the live-test case (BUH on port 8771).
    data = {"projects": [_top("КД", CONV_WS)],
            "otherEdtInstances": [_neighbor(BUH_WS, "BUH", 8771, ["БП"])]}
    param, port, miss = installer._find_edt_project(data, buh)
    check("neighbor instance: routes via edtWorkspace short name",
          param == "BUH" and port == 8771 and miss is None, (param, port, miss))

    # Neighbor without the short name: full workspace path is the fallback.
    neighbor = _neighbor(BUH_WS, None, None, ["БП"])
    neighbor.pop("edtWorkspace")
    data = {"projects": [], "otherEdtInstances": [neighbor]}
    param, port, miss = installer._find_edt_project(data, buh)
    check("neighbor instance: full-path fallback",
          param == BUH_WS and miss is None, (param, miss))

    # Name collision: the same name at the top level but in ANOTHER
    # workspace must not shadow the target's project in the neighbor.
    data = {"projects": [_top("БП", OTHER_WS)],
            "otherEdtInstances": [_neighbor(BUH_WS, "BUH", 8771, ["БП"])]}
    param, port, miss = installer._find_edt_project(data, buh)
    check("name collision: target workspace wins in the neighbor",
          param == "BUH" and port == 8771 and miss is None, (param, port, miss))

    # Neighbor workspace matches but the project is not open there.
    data = {"projects": [], "otherEdtInstances": [_neighbor(BUH_WS, "BUH", 8771, ["КД"])]}
    param, port, miss = installer._find_edt_project(data, buh)
    check("neighbor without the project: miss",
          param is None and miss and "ни в одной" in miss, (param, miss))

    # Nothing anywhere (the original live-test symptom).
    data = {"projects": [_top("КД", CONV_WS)],
            "otherEdtInstances": [_neighbor(OTHER_WS, "OTHER", 8772, ["БП"])]}
    param, port, miss = installer._find_edt_project(data, buh)
    check("nothing anywhere: miss names the searched workspace",
          param is None and miss and BUH_WS in miss, (param, miss))

    # Degenerate payload: no keys at all.
    param, port, miss = installer._find_edt_project({}, buh)
    check("empty payload: miss, no exception",
          param is None and miss and "ни в одной" in miss, (param, miss))

    # The routed note lines feed the report when a neighbor was used.
    note = installer._routed_note("BUH", 8771)
    check("routed note: names the instance and port",
          len(note) == 1 and "BUH" in note[0] and "8771" in note[0], note)
    check("routed note: silent for the connected instance",
          installer._routed_note(None, None) == [], "should be empty")

    return counters["pass"], counters["fail"]


if __name__ == "__main__":
    passed, failed = asyncio.run(run())
    print(f"stage11_check: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
