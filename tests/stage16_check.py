"""Stage 16: Windows authentication targets (auth.type "windows").

Offline: the Negotiate/NTLM legs run against a local HTTP mock with a
scripted SSPI stub factory (no real pyspnego/SSPI anywhere), the /WA+
command seam and the wa-flavored diagnostics are checked on real Target/
store objects with an in-memory keyring, same approach as stage 14.
"""

import contextlib
import http.server
import io
import json
import os
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "src"))

from rsv_data_router import cli, credentials  # noqa: E402
from rsv_data_router import forward, negotiate  # noqa: E402
from rsv_data_router import installer, targets as targets_mod  # noqa: E402
from rsv_data_router import update_designer  # noqa: E402


class FakeKeyring:
    """In-memory stand-in for the keyring module (see stage14)."""

    def __init__(self):
        self.store = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.store:
            raise credentials.KeyringError("entry not found")
        del self.store[(service, username)]


class StubContext:
    """Scripted SSPI context: hands out prepared tokens in order."""

    complete = False

    def __init__(self, tokens):
        self.tokens = list(tokens)

    def step(self, in_token=None):
        if not self.tokens:
            raise AssertionError("unexpected extra SSPI step")
        return self.tokens.pop(0)


class CyclingContext:
    """SSPI stub that never runs dry — for the unfinished-handshake mock,
    where the server keeps challenging no matter what token is sent."""

    complete = False

    def step(self, in_token=None):
        return b"loop-token"


class CompleteContext:
    """SSPI stub whose context is complete after the first step — for the
    restart mock, where the server keeps challenging a finished context
    (kernel-mode auth state lost with the connection)."""

    complete = True

    def step(self, in_token=None):
        return b"done-token"


class _WaMockHandler(http.server.BaseHTTPRequestHandler):
    """POST handler replaying scripted steps; records every Authorization.

    Speaks HTTP/1.1 with Content-Length on every answer — the transport
    under test keeps ONE connection for the whole handshake, the mock must
    not close it between legs."""

    protocol_version = "HTTP/1.1"

    steps = None       # list of {"status", "headers", "body"}
    fallback = None    # step replayed when the script is exhausted
    seen = None        # recorded requests

    def do_POST(self):
        # HTTP/1.1 keep-alive: the request body MUST be drained, or the
        # leftovers poison the next request line on this connection.
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        type(self).seen.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "content_type": self.headers.get("Content-Type"),
            "connection": self.headers.get("Connection"),
        })
        step = (self.steps.pop(0) if self.steps else self.fallback) or {}
        body = step.get("body", b"")
        self.send_response(step.get("status", 500))
        for name, value in step.get("headers", {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@contextlib.contextmanager
def wa_mock(steps, fallback=None):
    """(base_url, seen) of a one-shot mock server on an ephemeral port."""
    handler = type("BoundMock", (_WaMockHandler,),
                   {"steps": steps, "fallback": fallback,
                    "seen": []})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/base", handler.seen
    finally:
        server.shutdown()
        server.server_close()


def make_target(alias="t16", auth=None, keyring_key=None, url=None):
    """A real Target over a fake keyring (see stage14)."""
    target = targets_mod.Target(alias, url or "http://127.0.0.1:9/x", auth=auth)
    if keyring_key is not None:
        credentials.set_value(alias, keyring_key)
    return target


async def run(url=None, home=None, ports=None, verbose=True):
    """Smoke-loop contract; body is offline sync."""
    return run_sync(url, home, ports, verbose)


def run_sync(url=None, home=None, ports=None, verbose=True):
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

    real_keyring = credentials.keyring
    real_factory = negotiate._client_factory
    credentials.keyring = FakeKeyring()
    try:
        # -- Target model: auth.type "windows" --
        wa = make_target(auth={"type": "windows"},
                         keyring_key="a2V5cmluZzpzZWNyZXQ=")
        check("auth_is_windows", wa.auth_is_windows is True)
        check("auth_headers empty for wa", wa.auth_headers() == {},
              wa.auth_headers())
        check("auth_kind windows", wa.auth_kind() == "windows (SSPI)",
              wa.auth_kind())
        check("auth_value ignores stale keyring", wa.auth_value() is None,
              wa.auth_value())
        check("basic target unaffected",
              make_target(auth={"type": "basic", "value": ""}).auth_is_windows
              is False)

        # -- Store: from_dict / register(auth_mode=...) --
        parsed = targets_mod.Target.from_dict("w16", {
            "url": "http://srv/base", "auth": {"type": "windows"}})
        check("from_dict windows ok", parsed.auth_is_windows)
        try:
            targets_mod.Target.from_dict("wf16", {
                "kind": "file", "file_path": "C:/bases/f",
                "auth": {"type": "windows"}})
            check("from_dict file+windows refused", False)
        except targets_mod.TargetError as exc:
            check("from_dict file+windows refused", "ibsrv" in str(exc), exc)
        with tempfile.TemporaryDirectory() as store_home:
            store = targets_mod.TargetsStore(store_home)
            target, _ = store.register("w16", "http://srv/base",
                                       auth_mode="windows")
            check("register auth_mode=windows", target.auth_is_windows)
            target, _ = store.register("w16", "http://srv/base")
            check("auth_mode=None keeps windows", target.auth_is_windows)
            target, _ = store.register("w16", "http://srv/base",
                                       auth_mode="basic")
            check("auth_mode=basic switches back",
                  target.auth == {"type": "basic", "value": ""}, target.auth)
            try:
                store.register("w16", "http://srv/base", auth_mode="dpapi")
                check("bad auth_mode refused", False)
            except targets_mod.TargetError as exc:
                check("bad auth_mode refused", "auth_mode" in str(exc), exc)
            try:
                store.register("f16", None, auth_mode="windows", kind="file",
                               file_path="C:/bases/f")
                check("register file+windows refused", False)
            except targets_mod.TargetError as exc:
                check("register file+windows refused", "ibsrv" in str(exc), exc)

        # -- negotiate: full two-challenge handshake (Negotiate) --
        protocols = []
        hosts = []

        def scripted_factory(protocol, hostname):
            protocols.append(protocol)
            hosts.append(hostname)
            return StubContext([b"token1", b"token2"])

        negotiate._client_factory = scripted_factory
        steps = [
            {"status": 401, "headers": {"WWW-Authenticate": "Negotiate"}},
            {"status": 401, "headers": {"WWW-Authenticate":
                                        "Negotiate Y2hhbGxlbmdlMg=="}},
            {"status": 200, "body": json.dumps(
                {"jsonrpc": "2.0", "id": "x",
                 "result": {"content": [{"type": "text", "text": "pong"}]}
                 }).encode("utf-8")},
        ]
        with wa_mock(steps) as (base, seen):
            status, raw = negotiate.post_json(base + "/hs/rsvdata/mcp",
                                              b"{}", timeout_s=5)
            check("handshake 200", status == 200 and b"pong" in raw, raw[:80])
            check("three legs", len(seen) == 3, len(seen))
            check("leg1 without Authorization",
                  seen[0]["authorization"] is None, seen[0])
            check("leg2 carries token1",
                  seen[1]["authorization"] == "Negotiate dG9rZW4x",
                  seen[1]["authorization"])
            check("leg3 carries token2",
                  seen[2]["authorization"] == "Negotiate dG9rZW4y",
                  seen[2]["authorization"])
            check("leg1 content-type set",
                  seen[0]["content_type"] == "application/json; charset=utf-8",
                  seen[0]["content_type"])
            check("legs keep the connection alive",
                  all(item["connection"] == "keep-alive" for item in seen),
                  [item["connection"] for item in seen])
        check("protocol negotiated", protocols == ["negotiate"], protocols)
        check("SPN hostname passed", hosts == ["127.0.0.1"], hosts)

        # -- negotiate: bare NTLM scheme, one challenge --
        steps = [
            {"status": 401, "headers": {"WWW-Authenticate": "NTLM"}},
            {"status": 200, "body": b"ok"},
        ]
        with wa_mock(steps) as (base, seen):
            status, raw = negotiate.post_json(base + "/hs/rsvdata/mcp", b"{}",
                                              timeout_s=5)
            check("ntlm single leg 200", status == 200, status)
            check("ntlm protocol chosen", protocols[-1] == "ntlm", protocols)
            check("ntlm header scheme",
                  seen[1]["authorization"].startswith("NTLM "),
                  seen[1]["authorization"])

        # -- negotiate: unfinished handshake -> WaError after LEG_LIMIT --
        negotiate._client_factory = lambda protocol, hostname: CyclingContext()
        with wa_mock([], fallback={"status": 401, "headers": {
                "WWW-Authenticate": "Negotiate"}}) as (base, seen):
            try:
                negotiate.post_json(base + "/hs/rsvdata/mcp", b"{}",
                                    timeout_s=5)
                check("unfinished handshake raises", False)
            except negotiate.WaError as exc:
                check("unfinished handshake raises",
                      exc.status == 401 and "не завершено" in str(exc), exc)
            check("leg limit respected",
                  len(seen) == negotiate.LEG_LIMIT, len(seen))

        # -- negotiate: completed context + another challenge -> restart --
        restart_calls = []

        def complete_factory(protocol, hostname):
            restart_calls.append((protocol, hostname))
            return CompleteContext()

        negotiate._client_factory = complete_factory
        with wa_mock([], fallback={"status": 401, "headers": {
                "WWW-Authenticate": "Negotiate"}}) as (base, seen):
            try:
                negotiate.post_json(base + "/hs/rsvdata/mcp", b"{}",
                                    timeout_s=5)
                check("complete-context restart raises", False)
            except negotiate.WaError as exc:
                check("complete-context restart raises",
                      exc.status == 401 and "не завершено" in str(exc), exc)
            check("complete context restarted every leg",
                  len(restart_calls) == negotiate.LEG_LIMIT, restart_calls)

        # -- negotiate: 401 without Negotiate/NTLM offer --
        with wa_mock([{"status": 401}]) as (base, seen):
            try:
                negotiate.post_json(base + "/hs/rsvdata/mcp", b"{}",
                                    timeout_s=5)
                check("no-challenge 401 raises", False)
            except negotiate.WaError as exc:
                check("no-challenge 401 raises",
                      exc.status == 401 and "Basic" in str(exc), exc)

        # -- negotiate: non-401 HTTP failure keeps status/body --
        with wa_mock([{"status": 404, "body": b"nope"}]) as (base, seen):
            try:
                negotiate.post_json(base + "/hs/rsvdata/mcp", b"{}",
                                    timeout_s=5)
                check("404 passthrough raises", False)
            except negotiate.WaError as exc:
                check("404 passthrough raises",
                      exc.status == 404 and "nope" in (exc.body or ""), exc)

        # -- forward: wa=True wraps WaError into TransportError --
        with wa_mock([{"status": 401}]) as (base, seen):
            try:
                forward.post_json(base + "/hs/rsvdata/mcp", b"{}",
                                  wa=True, timeout_s=5)
                check("forward wa wraps error", False)
            except forward.TransportError as exc:
                check("forward wa wraps error",
                      exc.status == 401 and "Negotiate/NTLM" in str(exc), exc)
            check("forward wa went through negotiate",
                  len(seen) == 1, len(seen))

        # -- forward: wa=False stays plain (401 right away, no legs) --
        with wa_mock([{"status": 401, "headers": {
                "WWW-Authenticate": "Negotiate"}}]) as (base, seen):
            try:
                forward.post_json(base + "/hs/rsvdata/mcp", b"{}",
                                  timeout_s=5)
                check("plain post no negotiate", False)
            except forward.TransportError as exc:
                check("plain post no negotiate", exc.status == 401, exc)
            check("plain post single leg", len(seen) == 1, len(seen))

        # -- negotiate: neither backend installed -> install hint --
        negotiate._client_factory = negotiate._client_context
        saved_modules = {name: sys.modules.pop(name, None)
                         for name in ("sspilib", "spnego")}
        # None in sys.modules makes `import <name>` raise ImportError
        for name in saved_modules:
            sys.modules[name] = None
        try:
            try:
                negotiate._client_context("negotiate")
                check("missing sspilib/pyspnego hint", False)
            except negotiate.WaError as exc:
                check("missing sspilib/pyspnego hint",
                      "windows-auth" in str(exc) and "pip install" in str(exc),
                      exc)
        finally:
            for name, module in saved_modules.items():
                if module is not None:
                    sys.modules[name] = module
                else:
                    sys.modules.pop(name, None)

        # -- build_command: /WA+ seam --
        plain = update_designer.build_command(
            "C:/1cv8/1cv8.exe", "srv:1541", "base", "usr", "pwd",
            "x.log", "x.cfe", "load")
        wa_cmd = update_designer.build_command(
            "C:/1cv8/1cv8.exe", "srv:1541", "base", None, None,
            "x.log", "x.cfe", "load", wa=True)
        wa_cmd_with_login = update_designer.build_command(
            "C:/1cv8/1cv8.exe", "srv:1541", "base", "usr", "pwd",
            "x.log", "x.cfe", "delete", wa=True)
        check("plain command /N /P",
              ' /N "usr" /P "pwd"' in plain and " /WA+" not in plain, plain)
        check("wa command /WA+",
              " /WA+" in wa_cmd and "/N " not in wa_cmd and "/P " not in wa_cmd,
              wa_cmd)
        check("wa wins over stored login",
              " /WA+" in wa_cmd_with_login and "/N " not in wa_cmd_with_login,
              wa_cmd_with_login)
        check("masked_command keeps /WA+",
              update_designer.masked_command(wa_cmd) == wa_cmd, wa_cmd)

        # -- optional_login_password: windows takes the seam directly --
        wa_target = make_target(auth={"type": "windows"},
                                keyring_key="a2V5cmluZzpzZWNyZXQ=")
        login, password, note = update_designer.optional_login_password(
            wa_target)
        check("wa login=None despite keyring",
              login is None and password is None, (login, password))
        check("wa note names /WA+",
              bool(note) and "/WA+" in note and "set-secret" not in note, note)
        basic_no_key = make_target(alias="t16b",
                                   auth={"type": "basic", "value": ""})
        login, password, note = update_designer.optional_login_password(
            basic_no_key)
        check("basic no key keeps old note",
              login is None and note and "set-secret" in note, note)

        # -- diagnostics: wa-flavored 401 blocks --
        err = forward.TransportError("401", status=401)
        wa_block = forward.explain_failure(err, wa=True)
        basic_block = forward.explain_failure(err)
        check("explain_failure wa names Negotiate",
              "Negotiate/NTLM" in wa_block and "set-secret" not in wa_block,
              wa_block)
        check("explain_failure basic keeps set-secret",
              "set-secret" in basic_block, basic_block)
        lines = installer._auth_lines(
            make_target(auth={"type": "windows"}, url="http://127.0.0.1:9/x"),
            "Проверка связи: НЕ ПРОШЛА.")
        joined = "\n".join(lines)
        check("_auth_lines wa checklist",
              "Windows-аутентификацию" in joined and "set-secret" not in joined
              and "одной машине" in joined, joined)

        # -- cli: set-secret refuses a windows target --
        real_home = os.environ.get("RSV_DATA_ROUTER_HOME")
        saved_stdout = sys.stdout
        with tempfile.TemporaryDirectory() as cli_home:
            cli_store = targets_mod.TargetsStore(cli_home)
            cli_store.register("w16", "http://srv/base", auth_mode="windows")
            os.environ["RSV_DATA_ROUTER_HOME"] = cli_home
            try:
                with contextlib.redirect_stdout(io.StringIO()) as captured:
                    code = cli.main(["set-secret", "w16"])
                check("cli set-secret wa refuses", code == 1, code)
                check("cli set-secret wa explains",
                      "Windows-аутентификацию" in captured.getvalue(),
                      captured.getvalue())
                check("cli set-secret wa stored nothing",
                      credentials.get_value("w16") is None)
            finally:
                if real_home is not None:
                    os.environ["RSV_DATA_ROUTER_HOME"] = real_home
                else:
                    os.environ.pop("RSV_DATA_ROUTER_HOME", None)
                sys.stdout = saved_stdout
    finally:
        credentials.keyring = real_keyring
        negotiate._client_factory = real_factory

    return counters["pass"], counters["fail"]


if __name__ == "__main__":
    passed, failed = run_sync(verbose=True)
    print(f"stage16: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
