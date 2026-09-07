"""Stage-13 acceptance checks: platform sources + launch preflight.

Offline (no network): the explicit per-target platform key parsing
(version vs 1cv8.exe path), the auto-sources-win-over-key priority, and
the preflight decision matrix that decides BEFORE an install/removal job
starts whether any chain variant can work at all (fail-fast — the
live-case lesson): for installs also the RSVData binary itself (no
--rsvdatabinary file — the refusal lands before the ping, 07.09); a
reachable extension endpoint, an EDT link or a
resolvable platform start the job; otherwise the refusal names the
availability diagnosis and the ask-the-user step, and no job is created.

Uses the home dir given by the smoke launcher; standalone run makes its
own temporary one. forward.call_tool is monkeypatched in-process (the
smoke server is a separate process and is not affected).
"""

import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "src"))

from rsv_data_router import edt  # noqa: E402
from rsv_data_router import forward  # noqa: E402
from rsv_data_router import installer  # noqa: E402
import stage8_check  # noqa: E402
from rsv_data_router import update_designer  # noqa: E402

PING_VERSIONED = "pong — stage13; расширение 1.3.6, хеш s13hash="
PING_OLD = "pong — stage13 (расширение без версии в ответе)"
UUID13 = "b13b13b1-300d-4a13-9a13-000000000013"


class FakeUpstream:
    """Replaces forward.call_tool during the preflight checks.

    mode: versioned (extension answers with a version) | old (pong without
    a version) | down (HTTP 404 transport error).
    """

    def __init__(self):
        self.mode = "versioned"

    def __call__(self, url, name, arguments, headers=None, timeout_s=None):
        if self.mode == "down":
            exc = forward.TransportError("HTTP 404 - not found")
            exc.status = 404
            raise exc
        return PING_VERSIONED if self.mode == "versioned" else PING_OLD


def make_target(**kwargs):
    """A minimal target-like object for the offline chain helpers.

    The default ref is "nobody" on purpose: it matches nothing in the
    ibases fixture, so the auto sources stay silent unless a test points
    the url at the fixture base ("ours") explicitly.
    """
    fields = dict(alias="t13", url="http://127.0.0.1:9/nobody", edt=None,
                  platform=None, connection=None)
    fields.update(kwargs)
    target = SimpleNamespace(**fields)
    target.mcp_url = target.url + "/hs/rsvdata/mcp"
    target.auth_headers = lambda: {}
    target.auth = {"type": "none"}
    target.auth_kind = lambda: "нет"

    def _auth_value(t=target):
        value = str(t.auth.get("value", "")).strip()
        return value if t.auth.get("type") == "basic" and value else None
    target.auth_value = _auth_value
    return target


async def run(url=None, home=None, ports=None, verbose=True):
    """Smoke-loop contract (other stages are async); body is offline sync."""
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

    own_home = home is None
    home = home or tempfile.mkdtemp(prefix="stage13-")
    saved_env = {key: os.environ.get(key) for key in
                 ("RSV_DATA_ROUTER_IBASES", edt.PROJECTS_JSON_ENV,
                  update_designer.ROOTS_ENV)}
    os.environ["RSV_DATA_ROUTER_IBASES"] = os.path.join(home, "ibases.v8i")
    os.environ[edt.PROJECTS_JSON_ENV] = stage8_check.make_registry(home, [])
    # Two fake 1cv8 roots: refusals must not depend on the real machine's
    # installations; the single-install checks switch to one root below.
    multi_roots, _mf = stage8_check.make_designer_roots(
        os.path.join(home, "roots-multi"), x64=("8.3.27.2214", "8.5.1.1423"))
    single_roots, _sf = stage8_check.make_designer_roots(
        os.path.join(home, "roots-single"), x64=("8.5.1.1423",))
    os.environ[update_designer.ROOTS_ENV] = os.pathsep.join(multi_roots)
    fake = FakeUpstream()
    saved_call_tool = installer.forward.call_tool
    installer.forward.call_tool = fake
    # A valid binary for the install-matrix calls; the no-binary
    # precondition has its own dedicated checks below.
    cfe13 = os.path.join(home, "dummy13.cfe")
    with open(cfe13, "wb") as handle:
        handle.write(b"stage13 dummy cfe")

    def _preflight(target, **kwargs):
        return installer.preflight(target, cfe_path=cfe13, **kwargs)
    try:
        # Platform key parsing.
        path, version = installer._explicit_platform(make_target(platform="8.3.27.2214"))
        check("explicit platform: version parsed",
              path is None and version == "8.3.27.2214", f"{path!r} {version!r}")
        path, version = installer._explicit_platform(
            make_target(platform=r"C:\Program Files\1cv8\8.3.27.2214\bin\1cv8.exe"))
        check("explicit platform: windows path parsed",
              path == r"C:\Program Files\1cv8\8.3.27.2214\bin\1cv8.exe"
              and version is None, f"{path!r} {version!r}")
        path, version = installer._explicit_platform(make_target(platform="/opt/1cv8"))
        check("explicit platform: posix path parsed",
              path == "/opt/1cv8" and version is None, f"{path!r} {version!r}")
        check("explicit platform: empty -> nothing",
              installer._explicit_platform(make_target()) == (None, None),
              str(installer._explicit_platform(make_target())))

        # Auto sources win over the operator key.
        stage8_check.make_project(home, "Proj13", UUID13,
                                  binding=[(UUID13, "8.3.27.2214")])
        with open(os.environ["RSV_DATA_ROUTER_IBASES"], "w",
                  encoding="utf-8-sig") as handle:
            handle.write(f"[База тринадцать]\nID={UUID13}\n"
                         'Connect=Srvr="127.0.0.1:1541";Ref="ours";\n'
                         "DefaultVersion=8.3.27.2214\n")
        linked = make_target(edt={"workspace": home, "project": "Proj13"},
                             platform="8.5.9.9999")
        check("priority: auto binding wins over the platform key",
              update_designer.base_platform_version(linked) == "8.3.27.2214",
              update_designer.base_platform_version(linked))
        check("priority: the key itself still readable",
              installer._explicit_platform(linked) == (None, "8.5.9.9999"),
              str(installer._explicit_platform(linked)))

        # Preflight matrix. The endpoint answers with a version — go,
        # everything else is irrelevant.
        fake.mode = "versioned"
        # Precondition #0 for installs (user decision, 07.09): no binary —
        # the refusal comes before the ping and no job is created (same
        # fail-fast as get_rsvdata / update_targets). Removal ignores the
        # binary: with the endpoint answering, removal launches as usual.
        go, lines = installer.preflight(make_target())
        text = "\n".join(lines)
        check("preflight: no binary -> install refused before the ping",
              go is False and "бинарник RSVData" in text
              and "--rsvdatabinary" in text, text)
        go, lines = _preflight(make_target(), remove=True)
        check("preflight removal: no binary still launches (endpoint answers)",
              go is True and lines == [], "\n".join(lines))

        check("preflight: reachable extension -> go",
              _preflight(make_target()) == (True, []),
              str(_preflight(make_target())))

        # Endpoint without a version, no link, no connection anywhere:
        # refusal with diagnosis + the url-registration step (07.09: a
        # url-only target cannot install by design — a live-case shape,
        # before any job would have been created).
        fake.mode = "old"
        go, lines = _preflight(make_target())
        text = "\n".join(lines)
        check("preflight: known-dead chain refused",
              go is False and lines[0].startswith("Установка НЕ запущена"), text)
        check("preflight: refusal carries availability diagnosis",
              "Проверка связи" in text and "версии нет" in text, text)
        check("preflight: refusal names the url-registration cause",
              "регистрация только по url" in text
              and "connection" in text, text)

        # The same shape for removal (Удаление, not Установка).
        go, lines = _preflight(make_target(), remove=True)
        check("preflight removal: refused with its own header",
              go is False and lines[0].startswith("Удаление НЕ запущено"),
              "\n".join(lines))

        # The launch needs a CONNECTION source (EDT link or the stored
        # connection string) plus a platform answer: the key alone no
        # longer unblocks it, the EDT link does (EDT variant), and the
        # key + connection do (configurator variant).
        check("preflight: platform key alone still refuses (no connection)",
              _preflight(make_target(platform="8.3.27.2214"))[0] is False,
              "went without a connection source")
        check("preflight: platform key + connection unblock the launch",
              _preflight(make_target(
                  platform="8.3.27.2214",
                  connection='Srvr="127.0.0.1:1541";Ref="ours";'))[0] is True,
              "refused with the key and connection")
        check("preflight: EDT link unblocks the launch",
              _preflight(make_target(edt={"workspace": home,
                                                   "project": "Proj13"}))[0] is True,
              "refused with the link")

        # Endpoint down + the disk scan answers (the research source): the
        # version alone is not enough any more — the stored connection is
        # what turns the scan hit into a launchable configurator variant.
        fake.mode = "down"
        os.environ[edt.PROJECTS_JSON_ENV] = stage8_check.make_registry(home, [home])
        scanned = make_target(url="http://127.0.0.1:9/ours")
        check("preflight: scan version without connection source refuses",
              _preflight(scanned)[0] is False,
              str(_preflight(scanned)[1][-1]))
        scanned_conn = make_target(url="http://127.0.0.1:9/ours",
                                   connection='Srvr="127.0.0.1:1541";Ref="ours";')
        check("preflight: scan version + connection unblock the launch",
              _preflight(scanned_conn) == (True, []),
              str(_preflight(scanned_conn)))

        # The SINGLE installed platform is a configurator answer even with
        # no version source at all (user question, 06.09) — but again only
        # together with a connection source (07.09).
        os.environ[update_designer.ROOTS_ENV] = os.pathsep.join(single_roots)
        fake.mode = "old"
        check("preflight: single installed platform unblocks the launch",
              _preflight(make_target(
                  connection='Srvr="s:1";Ref="r";'))[0] is True, "refused")
        check("preflight: single installed platform without connection refuses",
              _preflight(make_target())[0] is False, "went")
        designer, used, note, refusal = installer._designer_for(make_target())
        check("single platform: designer substitutes with a note",
              refusal is None and designer and used == "8.5.1.1423"
              and "единственная" in note and "не определена" in note,
              f"{designer} {used} {note} {refusal}")

        # The configurator branch refuses with the same ask-the-user step
        # (no ping involved — "nobody" matches no source, so resolution
        # falls through everywhere; several roots installed).
        os.environ[update_designer.ROOTS_ENV] = os.pathsep.join(multi_roots)
        done, lines = installer.designer_branch(make_target(), "dummy.cfe", home)
        check("designer branch: refusal without any platform source",
              done is False and "Вариант конфигуратора недоступен" in lines[0]
              and "platform" in lines[0], "\n".join(lines))

        # resolve_connection (07.09): the stored connection answers without
        # an EDT link, the link wins when both exist, neither -> the
        # url-registration refusal.
        srvr, ref, name = update_designer.resolve_connection(
            make_target(connection='Srvr="Conn-Host:1541";Ref="ref13";'))
        check("resolve_connection: stored connection answers",
              (srvr, ref, name) == ("Conn-Host:1541", "ref13", "ref13"),
              f"{srvr} {ref} {name}")
        srvr, ref, name = update_designer.resolve_connection(
            make_target(edt={"workspace": home, "project": "Proj13"},
                        connection='Srvr="Conn-Host:1541";Ref="ref13";'))
        check("resolve_connection: EDT link wins over stored connection",
              (srvr, ref, name) == ("127.0.0.1:1541", "ours", "База тринадцать"),
              f"{srvr} {ref} {name}")
        try:
            update_designer.resolve_connection(make_target())
            check("resolve_connection: url-only target refused",
                  False, "no exception")
        except SystemExit as exc:
            check("resolve_connection: url-only target refused",
                  "регистрация по url" in str(exc) and "/remove" in str(exc),
                  str(exc))
        ok, payload = installer._resolve_designer_ctx(make_target(
            platform="8.3.27.2214", connection='Srvr="s:1";Ref="r";'))
        check("removal ctx: no key accepted, login is None with a note",
              ok is True and payload[3] is None and payload[4] is None
              and "не задан" in payload[5], str(payload))

        # Optional credentials (user decision, 07.09): a target without a
        # key still reaches the configurator launch — the command is built
        # without /N /P and the failure (fake 1cv8.exe) carries no refusal;
        # a PRESENT but unreadable key refuses with the set step.
        login, password, note = update_designer.optional_login_password(
            make_target())
        check("optional creds: no key -> login None with a hint note",
              login is None and password is None
              and "без /N /P" in note and "set-secret" in note,
              f"{login!r} {password!r} {note!r}")
        broken = make_target()
        broken.auth = {"type": "basic", "value": "user without separator"}
        try:
            update_designer.optional_login_password(broken)
            check("optional creds: broken key refuses", False, "no exception")
        except SystemExit as exc:
            check("optional creds: broken key refuses",
                  "повреждены" in str(exc) and "set-secret" in str(exc), str(exc))
        cmd = update_designer.build_command(
            "1cv8.exe", "s:1", "r", None, None, "out.log", "x.cfe", "load")
        check("build_command: no key omits /N and /P",
              "/N" not in cmd and "/P" not in cmd, cmd)
        cmd = update_designer.build_command(
            "1cv8.exe", "s:1", "r", "user", "", "out.log", "x.cfe", "load")
        check("build_command: empty password keeps /N with /P \"\"",
              '/N "user"' in cmd and '/P ""' in cmd, cmd)
        done, lines = installer.designer_branch(
            make_target(platform="8.3.27.2214",
                        connection='Srvr="127.0.0.1:1541";Ref="ours";'),
            "dummy.cfe", home)
        text = "\n".join(lines)
        check("designer branch: no-key target proceeds to the launch",
              done is False and any("не задан" in line for line in lines)
              and any("не удалась" in line for line in lines), text)
    finally:
        installer.forward.call_tool = saved_call_tool
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if own_home:
            import shutil
            shutil.rmtree(home, ignore_errors=True)

    return counters["pass"], counters["fail"]


if __name__ == "__main__":
    passed, failed = run_sync()
    print(f"stage13_check: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
