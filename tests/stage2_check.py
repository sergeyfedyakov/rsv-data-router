"""Stage-2 acceptance checks: target map + service tools + routing.

Standalone (defaults): router on :8785, mocks on :8791/:8792, home .test-home.
Via tests/smoke.py: run(url, home, ports) with dynamically picked ports.
"""

import asyncio
import json
import os
import sys

from fastmcp import Client
from fastmcp.exceptions import ToolError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "src"))

from rsv_data_router import targets as targets_mod  # noqa: E402


def _env_ports(default):
    raw = os.environ.get("SMOKE_PORTS")
    return tuple(int(p) for p in raw.split(",")) if raw else default


async def run(url=None, home=None, ports=None, verbose=True):
    """Returns (passed, failed)."""
    url = url or os.environ.get("SMOKE_URL") or "http://127.0.0.1:8785/mcp"
    home = home or os.environ.get("SMOKE_HOME") or os.path.join(ROOT, ".test-home")
    p1, p2 = ports or _env_ports((8791, 8792))
    cfg_path = os.path.join(home, "targets.json")
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

    def load_cfg():
        with open(cfg_path, encoding="utf-8") as handle:
            return json.load(handle)

    async with Client(url) as client:
        tools = sorted(t.name for t in await client.list_tools())
        check("tools list", tools == sorted([
            "ping", "config", "describe", "get_structure", "query", "execute_query",
            "eventlog", "reveal", "help", "help_router", "get_rsvdata",
            "register_target", "unregister_target", "list_targets",
            "update_targets", "job_status"]), tools)

        err, _ = await call(client, "ping", {})
        check("ping without targets -> instructive error",
              err and "Нет зарегистрированных целей" in err, err)

        err, text = await call(client, "register_target", {
            "url": f"http://127.0.0.1:{p1}/mcp", "base": "mock1", "comment": "первый мок"})
        check("register mock1", not err and "зарегистрирована" in text, err or text)
        check("register: ping ok", text and "Проверка связи: ок" in text, text)
        check("register: basic instructions in response",
              text and "ToBase64String" in text and cfg_path in text, text)

        t1 = load_cfg()["targets"]["mock1"]
        check("targets.json mock1: empty basic template", t1["url"].endswith(f"{p1}/mcp")
              and t1["auth"] == {"type": "basic", "value": ""}
              and t1["comment"] == "первый мок", t1)

        err, text = await call(client, "ping", {})
        check("ping without base -> single registered target",
              not err and f"mock{p1}/ping auth=none" in text, err or text)

        err, text = await call(client, "ping", {"base": "mock1"})
        check("ping base=mock1", not err and f"mock{p1}" in text, err or text)

        err, _ = await call(client, "ping", {"base": "nope"})
        check("ping base=nope -> error with available list",
              err and "не зарегистрирована" in err and "mock1" in err, err)

        err, text = await call(client, "register_target", {
            "url": f"http://127.0.0.1:{p2}/mcp", "base": "mock2"})
        targets = load_cfg()["targets"]
        check("register mock2 second target",
              not err and set(targets) == {"mock1", "mock2"}, targets)

        err, text = await call(client, "ping", {})
        check("ping without base, two targets -> error with list",
              err and "Доступные: mock1, mock2" in err, err)

        # Manual auth edit (simulates the user inserting a Basic key by hand).
        cfg = load_cfg()
        cfg["targets"]["mock1"]["auth"] = {"type": "basic", "value": "dXNlcjpwYXNz"}
        with open(cfg_path, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, ensure_ascii=False, indent=2)
        err, text = await call(client, "ping", {"base": "mock1"})
        check("hot-reload + Authorization forwarded",
              not err and "auth=Basic" in text, err or text)

        err, text = await call(client, "list_targets", {})
        check("list_targets shows auth kind only",
              not err and "auth: basic" in text and "dXNlcjpwYXNz" not in text, err or text)

        # dpapi auth types were removed in 0.10.0 (the OS keyring replaced
        # them): the store rejects them with a migration hint.
        try:
            targets_mod.Target.from_dict(
                "x", {"url": "http://x/", "auth": {"type": "dpapi", "value": "zzz"}})
            check("dpapi auth type rejected", False, "no error")
        except targets_mod.TargetError as exc:
            check("dpapi auth type rejected with migration hint",
                  "set-secret" in str(exc), str(exc))

        # OS-keyring secret (e2e: the router SUBPROCESS reads the real
        # Windows Credential Manager; the test process writes the entry).
        # Skipped where no usable backend (headless Linux, CI).
        if sys.platform == "win32":
            from rsv_data_router import credentials
            if not credentials.backend_usable():
                print("(keyring backend unusable - e2e block skipped)")
            else:
                import base64
                expected_key = credentials.encode_key("user", "pass")
                check("encode_key is base64 of login:password",
                      base64.b64decode(expected_key).decode("utf-8") == "user:pass",
                      expected_key)
                credentials.set_value("kr1", expected_key)
                try:
                    err, text = await call(client, "register_target", {
                        "url": f"http://127.0.0.1:{p1}/mcp", "base": "kr1",
                        "check": False})
                    check("kr1 registered with empty basic template",
                          not err, err or text)
                    err, text = await call(client, "ping", {"base": "kr1"})
                    check("keyring secret -> Authorization forwarded",
                          not err and "auth=Basic" in text, err or text)
                    err, text = await call(client, "list_targets", {})
                    check("list_targets shows keyring kind, never the value",
                          not err and "basic (keyring)" in text
                          and expected_key not in text, err or text)
                finally:
                    credentials.delete_value("kr1")
                err, text = await call(client, "ping", {"base": "kr1"})
                check("removed keyring entry -> no Authorization",
                      not err and "auth=none" in text, err or text)
                err, text = await call(client, "unregister_target", {"base": "kr1"})
                check("kr1 unregistered (cleanup for later stages)",
                      not err, err or text)

        err, text = await call(client, "register_target", {
            "url": f"http://127.0.0.1:{p1}/mcp", "base": "mock1", "comment": "обновлён"})
        cfg = load_cfg()
        check("re-register keeps manual auth",
              cfg["targets"]["mock1"]["auth"]["type"] == "basic"
              and cfg["targets"]["mock1"]["comment"] == "обновлён"
              and "обновлена" in (text or "") and "Авторизация: basic" in (text or ""),
              cfg["targets"]["mock1"])

        # JSON has no comments: "no auth" = delete the section entirely.
        cfg = load_cfg()
        del cfg["targets"]["mock1"]["auth"]
        with open(cfg_path, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, ensure_ascii=False, indent=2)
        err, text = await call(client, "ping", {"base": "mock1"})
        check("deleted auth section -> no Authorization",
              not err and f"mock{p1}/ping auth=none" in text, err or text)

        err, text = await call(client, "unregister_target", {"base": "mock2"})
        cfg = load_cfg()
        check("unregister mock2",
              not err and "удалена" in text and "mock2" not in cfg["targets"], text or err)

        err, text = await call(client, "ping", {})
        check("ping without base -> back to single target",
              not err and f"mock{p1}" in text, err or text)

        err, _ = await call(client, "unregister_target", {"base": "ghost"})
        check("unregister unknown -> error", err and "не зарегистрирована" in err, err)

        err, text = await call(client, "list_targets", {"check": True})
        check("list_targets check pings", not err and "mock1: ок" in text, err or text)

        err, text = await call(client, "unregister_target", {"base": "mock1"})
        check("cleanup: stage-2 targets unregistered",
              not err and not load_cfg()["targets"], text or err)

        leftovers = [f for f in os.listdir(home) if f.endswith(".tmp")]
        check("no tmp leftovers", not leftovers, leftovers)

    return counters["pass"], counters["fail"]


def main():
    passed, failed = asyncio.run(run())
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
