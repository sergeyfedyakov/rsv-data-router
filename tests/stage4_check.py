"""Stage-4 acceptance checks: EDT integration (cwd -> workspace -> base).

Unit part runs edt.py against a synthetic workspace tree and a fake
ibases.v8i (the server is not involved). Router part exercises
register_target (cwd auto-route / connection string) and no-base routing by
cwd; the smoke runner exports RSV_DATA_ROUTER_IBASES so the server resolves
against the same fake list.

Standalone (defaults): router on :8785, mocks on :8791/:8792, home .test-home.
Via tests/smoke.py: run(url, home, ports) with dynamically picked ports.
"""

import asyncio
import json
import os
import shutil
import sys

from fastmcp import Client
from fastmcp.exceptions import ToolError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "src"))

from rsv_data_router import edt  # noqa: E402

UUID_SRV = "11111111-1111-1111-1111-111111111111"
UUID_FILE = "22222222-2222-2222-2222-222222222222"
UUID_DUP = "33333333-3333-3333-3333-333333333333"
UUID_SRV2 = "44444444-4444-4444-4444-444444444444"
UUID_SRV3 = "55555555-5555-5555-5555-555555555555"


def build_fixtures(home):
    """Synthetic EDT workspaces + fake ibases.v8i inside home.

    Returns {"workspace": ..., "projects": {...}, "ibases": ...}. The fake
    list mirrors the real format, including the UTF-8 BOM. WS1 holds one
    server project (Прикладка), an extension project without its own
    association and a file-IB project; WS2 holds two server projects, so a
    workspace-level cwd must refuse with their list.
    """
    ws = os.path.join(home, ".test-edt", "WS1")
    assoc_dir = os.path.join("com._1c.g5.v8.dt.platform.services.core", ".default")

    def project(workspace_dir, name, uuid_value=None):
        folder = os.path.join(workspace_dir, edt.PROJECTS_REL, name)
        os.makedirs(folder, exist_ok=True)
        if uuid_value:
            assoc = os.path.join(folder, assoc_dir, "AssociationData.properties")
            os.makedirs(os.path.dirname(assoc), exist_ok=True)
            with open(assoc, "w", encoding="utf-8") as handle:
                handle.write("#Infobase association data of the project\n")
                handle.write(f"Infobases={uuid_value}\n")
                handle.write(f"DefaultInfobase={uuid_value}\n")
        # The agent's cwd lives inside the project working tree.
        deep = os.path.join(workspace_dir, name, "src", "Documents")
        os.makedirs(deep, exist_ok=True)
        return deep

    cwds = {
        "app": project(ws, "Прикладка", UUID_SRV),
        "ext": project(ws, "Расширение"),            # no association
        "file": project(ws, "ФайлБаза", UUID_FILE),  # file IB
        "plain": home,                               # outside any EDT
    }
    # Non-project subfolder and the workspace root itself: the main-config
    # project must be found without naming it.
    sub = os.path.join(ws, "Отчёты")
    os.makedirs(sub, exist_ok=True)
    cwds["sub"] = sub
    cwds["ws"] = ws

    # Second workspace: two server-associated projects.
    ws2 = os.path.join(home, ".test-edt", "WS2")
    project(ws2, "Основная1", UUID_SRV2)
    cwds["ws2_app2"] = project(ws2, "Основная2", UUID_SRV3)
    cwds["ws2"] = ws2

    ibases_path = os.path.join(home, ".test-edt", "ibases.v8i")
    content = (
        f'[Серверная база дымовых тестов]\n'
        f'Connect=Srvr="Cluster-Host.demo:1541";Ref="smoke_ref";\n'
        f'ID={UUID_SRV}\n'
        f'OrderInList=1\n'
        f'\n'
        f'[Файловая база]\n'
        f'Connect=File="C:\\bases\\filebase";\n'
        f'ID={UUID_FILE}\n'
        f'OrderInList=2\n'
        f'\n'
        f'[Вторая серверная]\n'
        f'Connect=Srvr="Other-Cluster.demo:1641";Ref="ref_two";\n'
        f'ID={UUID_SRV2}\n'
        f'\n'
        f'[Третья серверная]\n'
        f'Connect=Srvr="Third-Host.demo:1541";Ref="ref_three";\n'
        f'ID={UUID_SRV3}\n'
    )
    with open(ibases_path, "w", encoding="utf-8-sig") as handle:
        handle.write(content)

    return {"workspace": ws, "workspace2": ws2, "cwds": cwds, "ibases": ibases_path}


async def run(url=None, home=None, ports=None, verbose=True):
    """Returns (passed, failed)."""
    url = url or os.environ.get("SMOKE_URL") or "http://127.0.0.1:8785/mcp"
    home = home or os.environ.get("SMOKE_HOME") or os.path.join(ROOT, ".test-home")
    p1, p2 = ports or (8791, 8792)
    cfg_path = os.path.join(home, "targets.json")

    counters = {"pass": 0, "fail": 0}

    def check(label, condition, details=None):
        if condition:
            counters["pass"] += 1
            if verbose:
                print(f"PASS  {label}")
        else:
            counters["fail"] += 1
            print(f"FAIL  {label}{' :: ' + str(details) if details else ''}")

    fixtures = build_fixtures(home)

    # ---- unit part: edt.py against the synthetic tree ----

    located = edt.locate(fixtures["cwds"]["app"])
    check("locate deep cwd -> (workspace, project)",
          located == (fixtures["workspace"], "Прикладка"), located)
    check("locate non-EDT dir -> None",
          edt.locate(os.path.join(ROOT, "tests")) is None)

    check("infobase_uuid: associated project",
          edt.infobase_uuid(fixtures["workspace"], "Прикладка") == UUID_SRV)
    check("infobase_uuid: extension project -> None",
          edt.infobase_uuid(fixtures["workspace"], "Расширение") is None)

    with open(fixtures["ibases"], encoding="utf-8-sig") as handle:
        parsed = edt.parse_ibases(handle.read())
    check("parse_ibases: all bases by uuid",
          set(parsed) == {UUID_SRV, UUID_FILE, UUID_SRV2, UUID_SRV3}
          and parsed[UUID_SRV]["name"] == "Серверная база дымовых тестов", parsed)
    dup = edt.parse_ibases("[дубль]\nConnect=Srvr=\"a:1\";Ref=\"x\";\n"
                           f"ID={UUID_DUP}\n[дубль]\nConnect=Srvr=\"b:2\";Ref=\"y\";\n"
                           f"ID={UUID_DUP}\n")
    check("parse_ibases: duplicate sections -> last wins",
          dup[UUID_DUP]["connect"] == 'Srvr="b:2";Ref="y";', dup)

    connect = edt.parse_connect('Srvr="Cluster-Host.demo:1541";Ref="smoke_ref";'
                                'Usr="secret";Pwd="also-secret";')
    check("parse_connect: quotes stripped, keys kept",
          connect.get("Srvr") == "Cluster-Host.demo:1541"
          and connect.get("Ref") == "smoke_ref", connect)
    check("upstream_url: cluster port dropped, host lowered",
          edt.upstream_url(connect) == "http://cluster-host.demo/smoke_ref/hs/rsvdata/mcp",
          edt.upstream_url(connect))
    try:
        edt.upstream_url({"File": "C:\\bases\\filebase"})
        check("upstream_url: file IB -> EdtError", False, "no exception")
    except edt.EdtError as exc:
        check("upstream_url: file IB -> EdtError", "файловые" in str(exc))

    resolved = edt.resolve(fixtures["cwds"]["app"], ibases_path=fixtures["ibases"])
    check("resolve: full chain for the app project",
          resolved and resolved["project"] == "Прикладка"
          and resolved["url"] == "http://cluster-host.demo/smoke_ref/hs/rsvdata/mcp",
          resolved)
    try:
        edt.resolve(fixtures["cwds"]["file"], ibases_path=fixtures["ibases"])
        check("resolve: file IB raises EdtError", False, "no exception")
    except edt.EdtError:
        check("resolve: file IB raises EdtError", True)

    # Workspace root / plain subfolder / extension project: the main-config
    # project (the only one with a servable IB) must be found on its own.
    for key in ("ws", "sub", "ext"):
        resolved = edt.resolve(fixtures["cwds"][key], ibases_path=fixtures["ibases"])
        check(f"resolve: cwd {key} -> main project found automatically",
              resolved and resolved["project"] == "Прикладка"
              and resolved.get("auto_project") is True
              and resolved["url"] == "http://cluster-host.demo/smoke_ref/hs/rsvdata/mcp",
              resolved)
    try:
        edt.resolve(fixtures["cwds"]["ws2"], ibases_path=fixtures["ibases"])
        check("resolve: two server projects -> EdtError with the list",
              False, "no exception")
    except edt.EdtError as exc:
        check("resolve: two server projects -> EdtError with the list",
              "Основная1" in str(exc) and "Основная2" in str(exc), exc)
    resolved = edt.resolve(fixtures["cwds"]["ws2_app2"], ibases_path=fixtures["ibases"])
    check("resolve: explicit project beats the workspace scan",
          resolved and resolved["project"] == "Основная2"
          and "auto_project" not in resolved, resolved)
    check("resolve: non-EDT dir -> None",
          edt.resolve(fixtures["cwds"]["plain"], ibases_path=fixtures["ibases"]) is None)

    # ---- router part: registration and no-base routing by cwd ----

    async def call(client, name, arguments):
        try:
            result = await client.call_tool(name, arguments)
            return None, (result.content[0].text if result.content else "")
        except ToolError as exc:
            return str(exc), None

    def load_cfg():
        with open(cfg_path, encoding="utf-8") as handle:
            return json.load(handle)

    async with Client(url) as client:
        # No url, no connection: the address comes from the EDT chain.
        err, text = await call(client, "register_target", {
            "cwd": fixtures["cwds"]["app"], "check": False})
        check("register by cwd: edt source in reply (root url, B0)",
              not err and "адрес из edt" in text
              and "http://cluster-host.demo/smoke_ref" in text
              and "/hs/rsvdata/mcp" not in text, err or text)
        check("register by cwd: connection saved from ibases Connect",
              not err and "Строка подключения сохранена (по cwd (ibases.v8i))" in text
              and 'Srvr="Cluster-Host.demo:1541";Ref="smoke_ref";' in text,
              err or text)
        cfg = load_cfg()["targets"]
        check("register by cwd: auto alias + edt link in map",
              "smoke_ref" in cfg and cfg["smoke_ref"].get("edt") == {
                  "workspace": fixtures["workspace"], "project": "Прикладка"},
              cfg.get("smoke_ref"))
        check("register by cwd: connection in map",
              cfg.get("smoke_ref", {}).get("connection")
              == 'Srvr="Cluster-Host.demo:1541";Ref="smoke_ref";',
              cfg.get("smoke_ref"))

        # Route the target at the mock via hot-reload (the derived cluster
        # URL is unreachable in the sandbox); the cwd route must still pick
        # the target by its edt link and forward green.
        cfg = load_cfg()
        cfg["targets"]["smoke_ref"]["url"] = f"http://127.0.0.1:{p1}/mcp"
        with open(cfg_path, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, ensure_ascii=False, indent=2)
        err, text = await call(client, "ping", {"cwd": fixtures["cwds"]["app"]})
        check("ping without base, cwd -> edt-linked target",
              not err and f"mock{p1}" in text, err or text)

        err, text = await call(client, "ping", {"cwd": fixtures["cwds"]["ext"]})
        check("cwd of extension project -> auto-routes to the main project",
              not err and f"mock{p1}" in text, err or text)

        err, text = await call(client, "ping", {"cwd": fixtures["cwds"]["file"]})
        check("cwd of file-IB project -> clean 'not supported' error",
              err and "файловые информационные базы не поддерживаются" in err, err)

        # Workspace root: the project is derived, the reply says so.
        err, text = await call(client, "register_target", {
            "cwd": fixtures["cwds"]["ws"], "check": False})
        check("register by workspace root: project found automatically",
              not err and "адрес из edt" in text
              and "Проект определён автоматически" in text, err or text)

        # The password help is part of EVERY register reply, even when the
        # existing key was kept.
        cfg = load_cfg()
        cfg["targets"]["smoke_ref"]["auth"] = {"type": "basic", "value": "dXNlcjpwYXNz"}
        with open(cfg_path, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, ensure_ascii=False, indent=2)
        err, text = await call(client, "register_target", {
            "cwd": fixtures["cwds"]["ws"], "check": False})
        check("re-register keeps manual auth and still shows the key help",
              not err and "сохранена прежняя" in text and "set-secret" in text,
              err or text)

        err, text = await call(client, "register_target", {
            "connection": 'Srvr="Other-Host:2041";Ref="conn_ref";Usr="u";Pwd="p";',
            "check": False})
        check("register by connection string: root url + auto alias (B0)",
              not err and "адрес из connection" in text
              and "http://other-host/conn_ref" in text
              and "/hs/rsvdata/mcp" not in text, err or text)
        check("register by connection: normalized Srvr/Ref saved, creds dropped",
              not err and "Строка подключения сохранена (из параметра connection)" in text
              and 'Srvr="Other-Host:2041";Ref="conn_ref";' in text
              and "Usr" not in text and "Pwd" not in text, err or text)
        check("register by connection: map entry (root url, B0)",
              load_cfg()["targets"].get("conn_ref", {}).get("url")
              == "http://other-host/conn_ref")
        check("register by connection: connection in map",
              load_cfg()["targets"].get("conn_ref", {}).get("connection")
              == 'Srvr="Other-Host:2041";Ref="conn_ref";',
              load_cfg()["targets"].get("conn_ref"))
        err, _ = await call(client, "register_target", {
            "connection": 'Ref="no_server";', "base": "bad_conn", "check": False})
        check("register by malformed connection: instructive error",
              err and "Строка подключения не годится" in err, err)

        # cwd alongside url: the target binds to the EDT project when the
        # project points at the same base (root url match), and stays
        # unbound on a mismatch (new in 0.6.1).
        err, text = await call(client, "register_target", {
            "url": "http://cluster-host.demo/smoke_ref", "base": "cwd_bind",
            "cwd": fixtures["cwds"]["app"], "check": False})
        check("register url+cwd: EDT link bound by cwd",
              not err and "адрес из url" in text
              and "EDT-связь установлена по cwd" in text, err or text)
        check("register url+cwd: map carries edt link",
              load_cfg()["targets"].get("cwd_bind", {}).get("edt") == {
                  "workspace": fixtures["workspace"], "project": "Прикладка"},
              load_cfg()["targets"].get("cwd_bind"))
        check("register url+cwd: connection filled from the bound project",
              load_cfg()["targets"].get("cwd_bind", {}).get("connection")
              == 'Srvr="Cluster-Host.demo:1541";Ref="smoke_ref";',
              load_cfg()["targets"].get("cwd_bind"))

        err, text = await call(client, "register_target", {
            "url": "http://other-host/other_base", "base": "cwd_bind2",
            "cwd": fixtures["cwds"]["app"], "check": False})
        check("register url+cwd with another base: no binding",
              not err and "EDT-связь" not in text
              and load_cfg()["targets"].get("cwd_bind2", {}).get("edt") is None,
              err or text)
        check("register by url only: no connection, install unavailable note",
              not err and "Строка подключения не определена (регистрация по url)" in text
              and "установка расширения на эту базу недоступна" in text
              and load_cfg()["targets"].get("cwd_bind2", {}).get("connection") is None,
              err or text)

        err, _ = await call(client, "register_target", {"cwd": fixtures["cwds"]["plain"]})
        check("register with unresolvable cwd -> asks for url/connection",
              err and "url" in err and "connection" in err, err)

        # A map entry in the pre-B0 full form (.../hs/rsvdata/mcp) must route
        # green too: base_root trims the service tail at load time.
        cfg = load_cfg()
        cfg["targets"]["old_url"] = {"url": f"http://127.0.0.1:{p1}/hs/rsvdata/mcp"}
        with open(cfg_path, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, ensure_ascii=False, indent=2)
        err, text = await call(client, "ping", {"base": "old_url"})
        check("old-format url in map routes green (trim, B3)",
              not err and f"mock{p1}" in text, err or text)

        # Clean up stage-4 targets so later stages / reruns start clean.
        for alias in ("smoke_ref", "conn_ref", "old_url", "cwd_bind", "cwd_bind2"):
            await call(client, "unregister_target", {"base": alias})
        remaining = set(load_cfg()["targets"])
        check("after cleanup: stage-4 targets unregistered",
              not {"smoke_ref", "conn_ref", "old_url", "cwd_bind",
                   "cwd_bind2"} & remaining, remaining)

    shutil.rmtree(os.path.join(home, ".test-edt"), ignore_errors=True)
    return counters["pass"], counters["fail"]


def main():
    passed, failed = asyncio.run(run())
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
