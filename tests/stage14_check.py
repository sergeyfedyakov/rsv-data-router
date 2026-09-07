"""Stage 14: credential commands (set-secret / remove-secret) and the auth
chain (inline basic -> OS keyring -> none).

Everything runs IN-PROCESS against an in-memory keyring store (the keyring
module is swapped for a fake), so no real Credential Manager entries are
created here; the real-backend e2e (the router subprocess reading the real
keyring) lives in stage2, win32-only.
"""

import base64
import contextlib
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "src"))

from rsv_data_router import cli, credentials  # noqa: E402
from rsv_data_router import targets as targets_mod  # noqa: E402
from rsv_data_router import update_designer  # noqa: E402


class FakeKeyring:
    """In-memory stand-in for the keyring module (missing entries raise
    PasswordDeleteError like the real library, a KeyringError subclass)."""

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


def make_target(alias="t14", auth=None, keyring_key=None):
    """A real Target over a fake keyring: keyring_key emulates a stored
    entry without touching the OS store."""
    target = targets_mod.Target(alias, "http://127.0.0.1:9/x", auth=auth)
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
    fake = FakeKeyring()
    credentials.keyring = fake
    saved_stdin, saved_stdout = sys.stdin, sys.stdout
    try:
        # -- credentials API over the fake backend --
        check("empty store -> get_value None", credentials.get_value("a") is None)
        credentials.set_value("a", credentials.encode_key("user", "pass"))
        check("set/get roundtrip", credentials.get_value("a") ==
              base64.b64encode(b"user:pass").decode("ascii"))
        check("delete existing -> True", credentials.delete_value("a") is True)
        check("delete missing -> False", credentials.delete_value("a") is False)

        # -- cli: set-secret reads login/password from stdin lines --
        sys.stdin = io.StringIO("user\npass\n")
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = cli.main(["set-secret", "t14"])
        finally:
            sys.stdin = saved_stdin
        check("cli set-secret exits 0", code == 0, code)
        check("cli set-secret stored the key",
              fake.store[(credentials.SERVICE, "t14")] ==
              base64.b64encode(b"user:pass").decode("ascii"),
              fake.store.get((credentials.SERVICE, "t14")))
        check("cli set-secret message names the service",
              credentials.SERVICE in out.getvalue(), out.getvalue())

        # -- cli: remove-secret, twice (second run: nothing to remove) --
        sys.stdin = io.StringIO()
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out1:
                code1 = cli.main(["remove-secret", "t14"])
            with contextlib.redirect_stdout(io.StringIO()) as out2:
                code2 = cli.main(["remove-secret", "t14"])
        finally:
            sys.stdin = saved_stdin
        check("cli remove-secret removes and reports",
              code1 == 0 and "удалён" in out1.getvalue()
              and (credentials.SERVICE, "t14") not in fake.store)
        check("cli remove-secret on missing entry is calm",
              code2 == 0 and "нет" in out2.getvalue())

        # -- cli: --version and unknown-safe dispatch to serve --
        with contextlib.redirect_stdout(io.StringIO()) as vout:
            check("cli --version exits 0", cli.main(["--version"]) == 0)
        check("cli --version prints 0.10.0", "0.10.0" in vout.getvalue(),
              vout.getvalue())

        # -- Target auth chain --
        key = credentials.encode_key("kruser", "krpass")
        inline = credentials.encode_key("inl", "secret")
        basic = "Basic " + inline
        t = make_target(auth={"type": "basic", "value": inline}, keyring_key=key)
        check("inline value wins over keyring",
              t.auth_headers() == {"Authorization": basic}, t.auth_headers())
        check("kind: basic (inline)", t.auth_kind() == "basic (inline)")

        t = make_target(auth={"type": "basic", "value": ""}, keyring_key=key)
        check("empty inline falls through to keyring",
              t.auth_headers() == {"Authorization": "Basic " + key},
              t.auth_headers())
        check("kind: basic (keyring)", t.auth_kind() == "basic (keyring)")

        t = make_target(auth=None, keyring_key=key)
        check("no auth section still finds the keyring entry",
              t.auth_headers() == {"Authorization": "Basic " + key},
              t.auth_headers())
        fake.store.clear()

        t = make_target(auth=None)
        check("nothing stored -> no Authorization header", t.auth_headers() == {})
        check("kind: нет", t.auth_kind() == "нет")

        # -- basic_header shapes: all normalize to the same Basic key --
        expected = "Basic " + inline
        check("header from base64 key",
             targets_mod.basic_header(inline) == expected)
        check("header from ready Basic string",
             targets_mod.basic_header(basic) == expected)
        check("header from plain login:password",
             targets_mod.basic_header("inl:secret") == expected)

        # -- update_designer over the keyring --
        t = make_target(keyring_key=key)
        check("decrypt from keyring entry",
              update_designer.decrypt_login_password(t) == ("kruser", "krpass"))
        fake.store.clear()  # the no-key case must not see the entry above
        t = make_target()
        login, password, note = update_designer.optional_login_password(t)
        check("optional creds: no key -> hint with set-secret",
              login is None and password is None
              and "без /N /P" in note and "set-secret" in note,
              f"{login!r} {note!r}")
    finally:
        credentials.keyring = real_keyring
        sys.stdin, sys.stdout = saved_stdin, saved_stdout

    return counters["pass"], counters["fail"]


if __name__ == "__main__":
    passed, failed = run_sync()
    print(f"stage14_check: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
