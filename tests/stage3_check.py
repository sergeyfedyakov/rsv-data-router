"""Stage-3 acceptance checks: all wrappers, router help, log file.

Standalone (defaults): router on :8785, mocks on :8791/:8792, home .test-home.
Via tests/smoke.py: run(url, home, ports) with dynamically picked ports.
"""

import asyncio
import json
import os
import re
import sys

from fastmcp import Client
from fastmcp.exceptions import ToolError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env_ports(default):
    raw = os.environ.get("SMOKE_PORTS")
    return tuple(int(p) for p in raw.split(",")) if raw else default


def parse_echo(text):
    """Extract args dict from the mock echo 'mock<port>/<name> auth=... args={...}'."""
    match = re.search(r"args=(\{.*\})", text or "", re.DOTALL)
    return json.loads(match.group(1)) if match else None


async def run(url=None, home=None, ports=None, verbose=True):
    """Returns (passed, failed)."""
    url = url or os.environ.get("SMOKE_URL") or "http://127.0.0.1:8785/mcp"
    home = home or os.environ.get("SMOKE_HOME") or os.path.join(ROOT, ".test-home")
    p1, p2 = ports or _env_ports((8791, 8792))
    log_path = os.path.join(home, "router.log")
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

    async with Client(url) as client:
        expected = sorted(["ping", "config", "describe", "get_structure", "query",
                           "execute_query", "eventlog", "reveal", "help", "help_router",
                           "get_rsvdata", "register_target", "unregister_target",
                           "list_targets", "update_targets", "job_status"])
        tools = sorted(t.name for t in await client.list_tools())
        check("16 tools in registry", tools == expected, tools)

        err, _ = await call(client, "register_target", {
            "url": f"http://127.0.0.1:{p1}/mcp", "base": "mock1", "comment": "этап 3"})
        check("register mock1", not err, err)
        err, _ = await call(client, "register_target", {
            "url": f"http://127.0.0.1:{p2}/mcp", "base": "mock2"})
        check("register mock2", not err, err)

        # eventlog schema: from alias must show as "from" in the JSON schema
        tool_map = {t.name: t for t in await client.list_tools()}
        schema = getattr(tool_map["eventlog"], "input_schema", None) \
            or tool_map["eventlog"].inputSchema
        props = schema.get("properties", {})
        check("eventlog schema: from aliased", "from" in props and "from_" not in props,
              list(props))
        check("eventlog schema: base/cwd present",
              "base" in props and "cwd" in props, list(props))

        err, text = await call(client, "config", {"base": "mock1"})
        check("config forwards", not err and f"mock{p1}/config" in text, err or text)

        err, text = await call(client, "describe", {"find": "Контрагент", "base": "mock1"})
        args = parse_echo(text)
        check("describe passes args, drops None",
              not err and args == {"find": "Контрагент"}, err or text)

        err, text = await call(client, "get_structure", {"object": "Справочник.Номенклатура",
                                                         "base": "mock2"})
        args = parse_echo(text)
        check("get_structure routed by base",
              not err and args == {"object": "Справочник.Номенклатура"}
              and f"mock{p2}" in text, err or text)

        err, text = await call(client, "query", {
            "table": "Справочник.Номенклатура", "limit": 5, "base": "mock1",
            "filters": [{"field": "Наименование", "comparison": "contains",
                         "value": "стол"}],
            "order": ["-Наименование"]})
        args = parse_echo(text)
        check("query passes complex args",
              not err and args and args.get("table") == "Справочник.Номенклатура"
              and args.get("limit") == 5 and args.get("order") == ["-Наименование"]
              and args["filters"][0]["comparison"] == "contains", err or text)

        err, text = await call(client, "execute_query", {
            "query": "ВЫБРАТЬ ПЕРВЫЕ 10 Наименование ИЗ Справочник.Номенклатура",
            "parameters": {"Парам": "x"}, "limit": 10, "base": "mock1"})
        args = parse_echo(text)
        check("execute_query passes args",
              not err and args and args.get("parameters") == {"Парам": "x"}, err or text)

        err, text = await call(client, "eventlog", {
            "from": "2026-01-01", "to": "2026-01-31", "levels": ["Error", "Warning"], "base": "mock1",
            "commentContains": "провал", "limit": 50})
        args = parse_echo(text)
        check("eventlog maps from_->from",
              not err and args and args.get("from") == "2026-01-01"
              and args.get("levels") == ["Error", "Warning"]
              and args.get("commentContains") == "провал", err or text)

        err, text = await call(client, "eventlog", {"values": True, "base": "mock1"})
        args = parse_echo(text)
        check("eventlog values mode", not err and args == {"values": True}, err or text)

        err, text = await call(client, "reveal", {"text": "токен [ОРГ-00001]", "base": "mock1"})
        args = parse_echo(text)
        check("reveal passes text",
              not err and args == {"text": "токен [ОРГ-00001]"}, err or text)

        err, text = await call(client, "help", {"base": "mock1"})
        check("help without topic forwards to base",
              not err and f"mock{p1}/help" in text, err or text)

        err, text = await call(client, "help_router", {})
        check("help_router = router help",
              not err and "РОУТЕР MCP:RSV Data Router" in text
              and "help_router" in text and "get_rsvdata" in text
              and "mock1" in text and "mock2" in text,
              err or (text or "")[:200])

        err, text = await call(client, "help", {"topic": "query", "base": "mock1"})
        check("help with topic forwards",
              not err and f"mock{p1}/help" in text
              and (parse_echo(text) or {}).get("topic") == "query", err or text)

        err, text = await call(client, "help", {"topic": "query", "base": "mock2"})
        check("help with topic routed by base", not err and f"mock{p2}/help" in text,
              err or text)

        err, _ = await call(client, "ping", {"base": "nope"})
        check("unknown base error", err and "не зарегистрирована" in err, err)

        # --- log file ---
        await asyncio.sleep(0.3)
        with open(log_path, encoding="utf-8") as handle:
            log_text = handle.read()
        check("log exists with calls",
              "config -> mock1: ок" in log_text and "query -> mock1: ок" in log_text
              and "help -> mock1: ок" in log_text, log_text[-500:])
        check("log has error line", "ping -> ?: ошибка" in log_text, log_text[-300:])
        check("log has register lines", "register_target -> mock1" in log_text, log_text[:300])
        check("log clean of secrets",
              "Authorization" not in log_text and "Basic " not in log_text, log_text)

        # Clean up stage-3 targets so later stages / reruns start clean.
        for alias in ("mock1", "mock2"):
            await call(client, "unregister_target", {"base": alias})

    return counters["pass"], counters["fail"]


def main():
    passed, failed = asyncio.run(run())
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
