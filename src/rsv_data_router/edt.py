"""EDT (1C:Enterprise Development Tools) resolution: cwd -> upstream URL.

Chain (verified against real workspaces):
  cwd -> nearest ancestor with .metadata = workspace;
  first path segment under it = project name; a project without its own
      association (workspace root, plain subfolder, extension project) falls
      back to scanning the workspace for the single project with a server IB
      (the main configuration; several -> EdtError with the list);
  <WS>/.metadata/.plugins/org.eclipse.core.resources/.projects/<project>/
      com._1c.g5.v8.dt.platform.services.core/.default/AssociationData.properties
      -> DefaultInfobase=<uuid> (extension projects have no file of their own);
  %APPDATA%\\1C\\1CEStart\\ibases.v8i (UTF-8 with BOM, INI) -> section with
      ID=<uuid> -> Connect=Srvr="host:port";Ref="ref" or Connect=File="path";
  server IB -> http://<host-without-cluster-port>/<ref>/hs/rsvdata/mcp.
  The base platform version (configurator tooling) comes from the project's
      .infobase-binding (fixed runtime installation), fallback to the
      Version of the ibases.v8i entry — the exact build from the base's
      parameters (infobase_version()).
  For targets WITHOUT an EDT link there are two offline sources that work
      with EDT closed: a scan of every workspace the EDT starter remembers
      (scan_platform_versions over 1cedtstart/projects.json), then the
      Version of the ibases.v8i entry matched by Srvr/Ref alone
      (ibases_version).

Connect strings may embed Usr=/Pwd= — they are dropped here and never reach
logs or errors: router auth lives in targets.json.

File-based IBs are NOT supported (no web publication to point at): use the
existing COM bridge for those until automated publication exists.
"""

import json
import os
import re
import urllib.parse

PROJECTS_REL = os.path.join(".metadata", ".plugins", "org.eclipse.core.resources",
                            ".projects")
PROJECTS_JSON_ENV = "RSV_DATA_ROUTER_EDT_PROJECTS"
ASSOC_REL = os.path.join("com._1c.g5.v8.dt.platform.services.core", ".default",
                         "AssociationData.properties")
BINDING_REL = os.path.join("com._1c.g5.v8.dt.platform.services.core",
                           ".infobase-binding")
URL_SUFFIX = "/hs/rsvdata/mcp"

# "...EnterprisePlatform\=8.3.27.2214\=x86_64" — the fixed runtime
# installation EDT binds the infobase to (backslashes escape ':' and '='
# in the properties value).
_BINDING_VERSION_RE = re.compile(r"EnterprisePlatform(?:\\=|=)(\d+(?:\.\d+)+)")


class EdtError(Exception):
    """Resolution failure with a message safe to show and log (no credentials)."""


def locate(cwd):
    """cwd -> (workspace_dir, project_name or None) or None if no workspace.

    The project is the first path segment of cwd below the workspace dir;
    cwd == workspace itself means "no particular project".
    """
    current = os.path.abspath(cwd)
    while True:
        if os.path.isdir(os.path.join(current, ".metadata")):
            relative = os.path.relpath(os.path.abspath(cwd), current)
            if relative in (".", ""):
                return current, None
            return current, relative.split(os.sep)[0]
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def infobase_uuid(workspace, project, ibases_dir=None):
    """DefaultInfobase uuid of the project, or None (no own association)."""
    if not project:
        return None
    path = os.path.join(workspace, PROJECTS_REL, project, ASSOC_REL)
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return None
    props = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        props[key.strip()] = value.strip()
    uuid = props.get("DefaultInfobase")
    if not uuid:
        listed = [item.strip() for item in props.get("Infobases", "").split(",")]
        listed = [item for item in listed if item]
        uuid = listed[0] if len(listed) == 1 else None
    return uuid or None


def infobase_version(workspace, project, uuid=None):
    """Platform version bound to the project's infobase, or None.

    EDT writes <project>/.infobase-binding when a fixed runtime
    installation is chosen for the base (the exact version+build EDT
    launches this base with); each line is keyed by the infobase uuid.
    A single-line file answers even without a uuid; multi-base files
    require the uuid match. No fixed binding (auto selection) -> None.
    """
    if not project:
        return None
    path = os.path.join(workspace, PROJECTS_REL, project, BINDING_REL)
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return None
    matches = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        found = _BINDING_VERSION_RE.search(value)
        if found:
            matches[key.strip().rsplit("/", 1)[-1]] = found.group(1)
    if uuid and uuid in matches:
        return matches[uuid]
    if not uuid and len(matches) == 1:
        return next(iter(matches.values()))
    return None


def default_ibases_path():
    """ibases.v8i location; RSV_DATA_ROUTER_IBASES overrides (tests/exotics)."""
    override = os.environ.get("RSV_DATA_ROUTER_IBASES")
    if override:
        return override
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(appdata, "1C", "1CEStart", "ibases.v8i")


def projects_registry_path():
    """EDT starter registry (1cedtstart/projects.json); env override (tests).

    The launcher remembers every workspace ever opened, so the list works
    with EDT closed — the discovery source for the offline platform scan.
    """
    override = os.environ.get(PROJECTS_JSON_ENV)
    if override:
        return override
    local = (os.environ.get("LOCALAPPDATA")
             or os.path.join(os.path.expanduser("~"), "AppData", "Local"))
    return os.path.join(local, "1C", "1cedtstart", "projects.json")


def list_workspaces(registry_path=None):
    """Workspace dirs from the EDT starter registry (missing entries dropped).

    A workspace counts only when its .metadata dir exists on disk (entries
    of removed workspaces stay in the registry). A missing or unreadable
    registry is not an error: the scan is a best-effort source, an empty
    list just skips it.
    """
    try:
        with open(registry_path or projects_registry_path(),
                  encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    entries = data.get("data") if isinstance(data, dict) else None
    found = []
    for entry in entries if isinstance(entries, list) else ():
        location = entry.get("location") if isinstance(entry, dict) else None
        if location and os.path.isdir(os.path.join(str(location), ".metadata")):
            found.append(os.path.normpath(str(location)))
    return found


def connect_key_from_url(url):
    """(host, ref) of a publication url, shaped like an ibases Connect entry.

    The host drops the port and lowercases (mirrors upstream_url); ref is
    the first path segment of the publication root.
    """
    parts = urllib.parse.urlsplit(str(url).strip())
    segments = [segment for segment in parts.path.split("/") if segment]
    ref = segments[0].lower() if segments else ""
    return (parts.hostname or "").lower(), ref


def _connect_key(connect):
    """(host, ref) of a parsed ibases Connect string, same shape as
    connect_key_from_url() (cluster port dropped, case folded)."""
    host = str(connect.get("Srvr", "")).split(",")[0].split(":")[0].strip().lower()
    return host, str(connect.get("Ref", "")).strip().lower()


def scan_platform_versions(ibases_path=None, registry_path=None):
    """{(host, ref): {"version", "workspace", "project"}} over known workspaces.

    Offline fallback source (works with EDT closed): every workspace the
    EDT starter remembers is scanned for projects whose association points
    at an ibases.v8i entry, and the project's .infobase-binding provides
    the exact platform build. First hit wins (several workspaces may share
    a base); unreadable or unbound projects are silently skipped.
    """
    try:
        bases = read_ibases(ibases_path or default_ibases_path())
    except EdtError:
        return {}
    found = {}
    for workspace in list_workspaces(registry_path):
        for project in list_projects(workspace):
            uuid = infobase_uuid(workspace, project)
            entry = bases.get(uuid) if uuid else None
            if not entry:
                continue
            version = infobase_version(workspace, project, uuid)
            if not version:
                continue
            key = _connect_key(parse_connect(entry["connect"]))
            if key[0] and key[1]:
                found.setdefault(key, {"version": version,
                                       "workspace": workspace,
                                       "project": project})
    return found


def ibases_version(key, ibases_path=None):
    """Version of the ibases.v8i entry matching (host, ref), or None.

    The last standalone source: no EDT, no uuid — entries are matched by
    their own Srvr/Ref. Only entries whose parameters carry the exact
    build (the Version key) answer (e.g. Version=8.3.27.2214).
    """
    if not key[0] or not key[1]:
        return None
    try:
        bases = read_ibases(ibases_path or default_ibases_path())
    except EdtError:
        return None
    for entry in bases.values():
        if _connect_key(parse_connect(entry["connect"])) == key \
                and entry.get("version"):
            return entry["version"]
    return None


def entry_by_name(name, ibases_path=None):
    """ibases.v8i entry by its EXACT section name, or None.

    The list name is what the user calls the base ("Считаем имя точное",
    user decision, 06.09): no normalization, no fuzzy matching — the
    comparison is exact after stripping the surrounding whitespace.
    """
    if not name or not str(name).strip():
        return None
    wanted = str(name).strip()
    try:
        bases = read_ibases(ibases_path or default_ibases_path())
    except EdtError:
        return None
    for entry in bases.values():
        if entry["name"] == wanted:
            return entry
    return None


def parse_ibases(text):
    """ibases.v8i content -> {uuid: {"name", "connect", "version"}} (last wins).

    Hand-rolled INI parse: configparser rejects duplicate section names, which
    a long-lived ibases.v8i easily accumulates. Version is the optional
    Version of the entry — the exact build from the base's own parameters
    (user decision, 06.09: DefaultVersion is NOT used).
    """
    bases, name, props = {}, None, {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            if name is not None and props.get("ID"):
                bases[props["ID"]] = _ibases_entry(name, props)
            name, props = line[1:-1].strip(), {}
        elif "=" in line and name is not None:
            key, _, value = line.partition("=")
            props[key.strip()] = value.strip()
    if name is not None and props.get("ID"):
        bases[props["ID"]] = _ibases_entry(name, props)
    return bases


def _ibases_entry(name, props):
    # Version (user decision, 06.09) is the EXACT build written in the
    # base's own parameters; DefaultVersion is deliberately ignored —
    # it is not guaranteed to be the build this base runs on. The value
    # may be coarse ("8.3", "8.3.27") — the configurator resolver judges.
    return {"name": name, "connect": props.get("Connect", ""),
            "version": props.get("Version") or None}


def parse_connect(connect):
    """'Srvr="host:port";Ref="ref";' -> {key: value} with quotes stripped."""
    result = {}
    for part in str(connect).split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        result[key.strip()] = value
    return result


def normalize_connection(connect):
    """Raw Connect string or parsed dict -> 'Srvr="host:port";Ref="ref";'.

    The form stored in the target record so the configurator can reconnect
    without an EDT link. The cluster port (and a comma-separated server
    list) in Srvr is kept verbatim — one web server may front several
    clusters, so the port cannot be derived from the publication url.
    Everything but Srvr/Ref (Usr/Pwd and other keys) is dropped — no
    credentials in targets.json. File IBs are rejected with the same hint
    as upstream_url.
    """
    parts = parse_connect(connect) if isinstance(connect, str) else dict(connect)
    if parts.get("File"):
        raise EdtError(
            "файловые информационные базы не поддерживаются (нет веб-публикации); "
            "для файлового варианта используйте COM-мост")
    srvr = parts.get("Srvr", "").strip()
    ref = parts.get("Ref", "").strip()
    if not srvr or not ref:
        raise EdtError("строка подключения без Srvr/Ref — сохранить её нельзя")
    return f'Srvr="{srvr}";Ref="{ref}";'


def upstream_url(connect):
    """Connection parts -> rsvdata publication URL; raises EdtError otherwise.

    Cluster port is dropped (the web publication lives on the plain HTTP
    host), Usr/Pwd are ignored. File IBs are rejected with a hint.
    """
    if connect.get("File"):
        raise EdtError(
            "файловые информационные базы не поддерживаются (нет веб-публикации); "
            "для файлового варианта используйте COM-мост")
    srvr, ref = connect.get("Srvr", ""), connect.get("Ref", "")
    if not srvr or not ref:
        raise EdtError("строка подключения без Srvr/Ref — не удалось построить URL")
    host = srvr.split(",")[0].split(":")[0].strip().lower()
    if not host:
        raise EdtError("строка подключения с пустым Srvr — не удалось построить URL")
    return f"http://{host}/{ref}{URL_SUFFIX}"


def list_projects(workspace):
    """Project directory names known to the workspace (.projects entries)."""
    try:
        return sorted(os.listdir(os.path.join(workspace, PROJECTS_REL)))
    except OSError:
        return []


def read_ibases(path):
    """ibases.v8i content parsed by uuid; EdtError when unreadable."""
    try:
        with open(path, encoding="utf-8-sig") as handle:
            return parse_ibases(handle.read())
    except OSError as exc:
        raise EdtError(f"не удалось прочитать список баз EDT ({path}): {exc}") from exc


def resolve(cwd, ibases_path=None):
    """Full chain cwd -> resolution dict or None (nothing EDT-ish found).

    The project is taken from the cwd path when it carries its own
    association. Otherwise (cwd at the workspace root, a plain subfolder or
    an extension project — extensions have no association of their own) the
    workspace is scanned for the main-configuration project: the single one
    whose association resolves to a server IB. Zero candidates -> None,
    several -> EdtError listing them, the only candidate being a file IB ->
    the loud file-IB error.
    """
    located = locate(cwd)
    if not located:
        return None
    workspace, project = located
    path = ibases_path or default_ibases_path()

    def entry_for(project_name):
        """(uuid, ibases entry) of a project with its own association."""
        uuid = infobase_uuid(workspace, project_name)
        return (uuid, read_ibases(path).get(uuid)) if uuid else (None, None)

    if project:
        uuid, entry = entry_for(project)
        if entry:
            connect = parse_connect(entry["connect"])
            return {"workspace": workspace, "project": project, "uuid": uuid,
                    "name": entry["name"], "connect": connect,
                    "url": upstream_url(connect)}

    # No project in cwd or it has no resolvable IB of its own: scan the
    # workspace for the one project we can serve.
    found, file_error = [], None
    bases = read_ibases(path)
    for name in list_projects(workspace):
        uuid = infobase_uuid(workspace, name)
        entry = bases.get(uuid) if uuid else None
        if not entry:
            continue
        connect = parse_connect(entry["connect"])
        try:
            url = upstream_url(connect)
        except EdtError as exc:
            file_error = file_error or exc
            continue
        found.append({"workspace": workspace, "project": name, "uuid": uuid,
                      "name": entry["name"], "connect": connect, "url": url})
    if len(found) == 1:
        found[0]["auto_project"] = True
        return found[0]
    if len(found) > 1:
        names = ", ".join(sorted(r["project"] for r in found))
        raise EdtError(f"в воркспейсе несколько проектов с базами ({names}) — "
                       "укажите cwd нужного проекта")
    if file_error:
        raise file_error
    return None
