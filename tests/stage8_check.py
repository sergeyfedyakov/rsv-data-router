"""Stage-8 acceptance checks: configurator platform resolution.

Offline (no network, no real 1C): the base platform version must come
from the EDT workspace (.infobase-binding, fallback DefaultVersion in
ibases.v8i), then — for targets with no EDT link — from the offline scan
of the workspaces the EDT starter remembers (1cedtstart/projects.json,
works with EDT closed; a live-verified source), then from the DefaultVersion
of the ibases entry matched by Srvr/Ref alone; find_designer must return
the EXACT build or refuse — neighboring builds are not interchangeable
(user decision, 06.09; the previous "newest installed" pick pushed 8.5.1
tooling onto 8.3 bases).

Uses the home dir given by the smoke launcher; standalone run makes its
own temporary one. The env sources (ibases, projects registry, 1cv8
roots) are pointed at fixtures for the run and restored after.
"""

import json
import os
import sys
import tempfile
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "src"))

from rsv_data_router import edt  # noqa: E402
from rsv_data_router import update_designer  # noqa: E402

BINDING_LINE = ("com._1c.g5.v8.dt.platform.services.core/infobaseBinding/{uuid}"
                "=com._1c.g5.v8.dt.platform.services.core.resolvableInstallations"
                ".fixed\\:com._1c.g5.v8.dt.platform.services.core.runtimeType"
                ".EnterprisePlatform\\={version}\\=x86_64")
ASSOC_LINES = ("#Infobase association data of the project\n"
               "Infobases={uuid}\n"
               "DefaultInfobase={uuid}\n")


def make_designer_roots(home, x64=(), x86=()):
    """Fake 1cv8 install roots; returns (roots, [created files])."""
    roots, files = [], []
    for subdir, versions in (("PF64", x64), ("PF86", x86)):
        root = os.path.join(home, subdir)
        roots.append(root)
        for version in versions:
            exe = os.path.join(root, version, "bin", "1cv8.exe")
            os.makedirs(os.path.dirname(exe), exist_ok=True)
            with open(exe, "wb"):
                pass
            files.append(exe)
    return roots, files


def make_project(home, name, uuid, binding=None, assoc=True):
    """Synthetic EDT project with association and optional .infobase-binding."""
    base = os.path.join(home, ".metadata", ".plugins",
                        "org.eclipse.core.resources", ".projects", name,
                        "com._1c.g5.v8.dt.platform.services.core")
    if assoc:
        os.makedirs(os.path.join(base, ".default"), exist_ok=True)
        with open(os.path.join(base, ".default", "AssociationData.properties"),
                  "w", encoding="utf-8") as handle:
            handle.write(ASSOC_LINES.format(uuid=uuid))
    if binding:
        with open(os.path.join(base, ".infobase-binding"), "w",
                  encoding="utf-8") as handle:
            handle.write("#Infobase binding data of the project\n")
            for line_uuid, line_version in binding:
                handle.write(BINDING_LINE.format(uuid=line_uuid,
                                                 version=line_version) + "\n")
    return base


def make_ibases(home, uuid, version):
    """Two-entry ibases.v8i: the keyed one carries the exact Version."""
    path = os.path.join(home, "ibases.v8i")
    with open(path, "w", encoding="utf-8-sig") as handle:
        handle.write("[Другая база]\n"
                     "ID=00000000-0000-0000-0000-000000000000\n"
                     'Connect=Srvr="srv:1541";Ref="other";\n')
        handle.write(f"[Наша база]\nID={uuid}\n"
                     'Connect=Srvr="srv:1541";Ref="ours";\n')
        if version:
            handle.write(f"Version={version}\n")
    return path


def make_registry(home, locations):
    """1cedtstart-style projects.json listing workspace dirs."""
    os.makedirs(home, exist_ok=True)
    path = os.path.join(home, "projects.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"version": "1.1",
                   "data": [{"location": loc} for loc in locations]}, handle)
    return path


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
    home = home or tempfile.mkdtemp(prefix="stage8-")
    saved_env = {key: os.environ.get(key) for key in
                 ("RSV_DATA_ROUTER_IBASES", edt.PROJECTS_JSON_ENV,
                  update_designer.ROOTS_ENV)}
    empty_registry = make_registry(os.path.join(home, "reg-empty"), [])
    os.environ["RSV_DATA_ROUTER_IBASES"] = os.path.join(home, "ibases.v8i")
    os.environ[edt.PROJECTS_JSON_ENV] = empty_registry
    os.environ.pop(update_designer.ROOTS_ENV, None)
    try:
        roots, _files = make_designer_roots(
            home, x64=("8.3.27.2214", "8.5.1.1423"), x86=("8.5.1.1302",))

        exact = update_designer.find_designer("8.3.27.2214", roots)
        check("find_designer: exact build",
              exact == os.path.join(home, "PF64", "8.3.27.2214", "bin", "1cv8.exe"),
              exact)
        exact86 = update_designer.find_designer("8.5.1.1302", roots)
        check("find_designer: x86-only version found",
              "PF86" in exact86 and exact86.endswith("1cv8.exe"), exact86)

        os.environ[update_designer.ROOTS_ENV] = os.pathsep.join(roots)
        env_found = update_designer.find_designer("8.5.1.1423")
        os.environ.pop(update_designer.ROOTS_ENV, None)
        check("find_designer: ROOTS_ENV override respected",
              env_found == os.path.join(home, "PF64", "8.5.1.1423", "bin", "1cv8.exe"),
              env_found)

        def refused(version):
            try:
                return None, update_designer.find_designer(version, roots)
            except SystemExit as exc:
                return str(exc), None

        text, path = refused("8.3.27.1999")
        check("find_designer: neighboring build refused",
              path is None and "точная версия" in text
              and "совместимости" in text and "8.3.27.2214" in text, text)
        text, path = refused("8.4.1.1111")
        check("find_designer: absent branch refused with installed list",
              path is None and "8.5.1.1302" in text and "8.5.1.1423" in text,
              text)
        text, path = refused("мусор")
        check("find_designer: unparsable version refused",
              path is None and "не распознана" in text, text)

        empty_roots, _empty = make_designer_roots(os.path.join(home, "empty"))
        try:
            update_designer.find_designer("8.3.27.2214", empty_roots)
            text, path = None, "no error (!)"
        except SystemExit as exc:
            text, path = str(exc), None
        check("find_designer: nothing installed refused",
              path is None and "не найден" in text, text)

        uuid = "35841e4a-0fd7-457c-8785-75f65e27458d"
        make_project(home, "Proj1", uuid,
                     binding=[(uuid, "8.3.27.2214")])
        target = SimpleNamespace(edt={"workspace": home, "project": "Proj1"},
                                 url="http://srv:1561/ours")
        check("base_platform_version: from .infobase-binding",
              update_designer.base_platform_version(target) == "8.3.27.2214",
              update_designer.base_platform_version(target))

        make_project(home, "Proj2", uuid,
                     binding=[(uuid, "8.3.27.2214"),
                              ("00000000-0000-0000-0000-000000000001", "8.5.1.1423")])
        target2 = SimpleNamespace(edt={"workspace": home, "project": "Proj2"},
                                  url="http://srv:1561/ours")
        check("base_platform_version: multi-base binding needs uuid",
              update_designer.base_platform_version(target2) == "8.3.27.2214",
              update_designer.base_platform_version(target2))

        make_project(home, "Proj3", uuid)
        ibases = make_ibases(home, uuid, "8.3.27.2214")
        # url points at the entry WITHOUT Version: only the uuid
        # fallback of the linked project can answer here.
        target3 = SimpleNamespace(edt={"workspace": home, "project": "Proj3"},
                                  url="http://srv:1561/other")
        check("base_platform_version: fallback to the entry Version",
              update_designer.base_platform_version(target3, ibases) == "8.3.27.2214",
              update_designer.base_platform_version(target3, ibases))

        # Offline scan (the research source, works with EDT closed): the
        # registry names the workspace, the project bound to the ibases
        # entry "ours" carries the binding — no EDT link on the target.
        os.environ[edt.PROJECTS_JSON_ENV] = make_registry(
            home, [home, os.path.join(home, "ghost")])
        found = edt.list_workspaces()
        check("list_workspaces: existing kept, missing dropped",
              found == [os.path.normpath(home)], str(found))
        scan = edt.scan_platform_versions()
        check("scan_platform_versions: host+ref matched",
              scan.get(("srv", "ours"), {}).get("version") == "8.3.27.2214"
              and scan.get(("srv", "ours"), {}).get("workspace") == home,
              str(scan))
        target4 = SimpleNamespace(edt=None, url="http://SRV:1561/ours")
        check("base_platform_version: disk scan answers without EDT link",
              update_designer.base_platform_version(target4, ibases) == "8.3.27.2214",
              update_designer.base_platform_version(target4, ibases))

        # Standalone ibases fallback: empty registry (nothing bound on
        # disk), the entry matched by its own Srvr/Ref.
        os.environ[edt.PROJECTS_JSON_ENV] = empty_registry
        target5 = SimpleNamespace(edt=None, url="http://srv:1561/ours")
        check("base_platform_version: standalone ibases Version",
              update_designer.base_platform_version(target5, ibases) == "8.3.27.2214",
              update_designer.base_platform_version(target5, ibases))

        check("base_platform_version: no sources -> None",
              update_designer.base_platform_version(
                  SimpleNamespace(edt=None, url="http://srv:1561/unknown")) is None)

        parsed = edt.parse_ibases(open(ibases, encoding="utf-8-sig").read())
        check("parse_ibases: Version key parsed (DefaultVersion ignored)",
              parsed[uuid]["version"] == "8.3.27.2214"
              and parsed["00000000-0000-0000-0000-000000000000"]["version"] is None,
              str(parsed[uuid]))
        by_name = edt.entry_by_name("Наша база", ibases)
        check("entry_by_name: exact section name",
              by_name and by_name["connect"] == 'Srvr="srv:1541";Ref="ours";'
              and edt.entry_by_name("наша БАЗА", ibases) is None
              and edt.entry_by_name("нет такой", ibases) is None,
              str(by_name))

        # Single installed platform: the exact build wins when present;
        # otherwise the ONLY installation substitutes, with a note.
        single_roots, _single_files = make_designer_roots(
            os.path.join(home, "single"), x64=("8.5.1.1423",))
        path, used, note = update_designer.resolve_designer("8.5.1.1423",
                                                            single_roots)
        check("resolve_designer: exact build still wins",
              used == "8.5.1.1423" and not note, f"{used} {note}")
        path, used, note = update_designer.resolve_designer("8.3.27.2214",
                                                            single_roots)
        check("resolve_designer: mismatch + single installed substitutes",
              used == "8.5.1.1423" and path.endswith("1cv8.exe")
              and "единственная" in note and "8.3.27.2214" in note,
              f"{used} {note}")
        path, used, note = update_designer.resolve_designer(None, single_roots)
        check("resolve_designer: no version + single installed substitutes",
              used == "8.5.1.1423" and "не определена" in note, f"{used} {note}")

        def resolve_refused(version, roots_):
            try:
                return None, update_designer.resolve_designer(version, roots_)
            except SystemExit as exc:
                return str(exc), None

        text, result = resolve_refused(None, roots)
        check("resolve_designer: no version + several installed refuses",
              result is None and "запросите у пользователя" in text
              and "platform" in text, text)
    finally:
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
    print(f"stage8_check: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
