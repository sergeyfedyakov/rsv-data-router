"""Target map (targets.json): alias -> upstream connection settings.

The store is the router's only persistent state. Secrets live in the OS
keyring (one entry per alias, see credentials) — the store hot-reloads the
file when its mtime changes and the keyring is read on every call, so both
`rsv-data-router set-secret` and a hand edit apply without a restart. The
inline "basic" auth section stays as the portable fallback for environments
without a keyring backend (headless Linux, CI); DPAPI blobs are gone — the
keyring replaced them.
"""

import base64
import json
import os
import re
import threading

from . import credentials

CONFIG_NAME = "targets.json"
ALIAS_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
MCP_PATH = "/hs/rsvdata/mcp"
ROOT_PATH = "/hs/rsvdata"

# The only auth types accepted in targets.json since 0.10.0: "basic" with a
# value (portable inline fallback) or an empty value ("consult the keyring"),
# and "none" (explicitly no Authorization). DPAPI types were removed.
KNOWN_AUTH_TYPES = ("basic", "none")


def base_root(url):
    """Any accepted target url -> publication root http://host/base.

    The map stores the root form (B0); urls carrying the service path
    (.../hs/rsvdata/mcp or .../hs/rsvdata) are trimmed at load/register time,
    so old targets.json files keep working without a rewrite.
    """
    clean = str(url).strip().rstrip("/")
    lowered = clean.lower()
    for suffix in (MCP_PATH, ROOT_PATH):
        if lowered.endswith(suffix):
            return clean[: -len(suffix)].rstrip("/")
    return clean


def _looks_like_base64_key(value):
    """True when value is a base64-encoded 'login:password' key.

    Strict decode + utf-8 + the colon separator: a plain 'login:password'
    (what a PowerShell ConvertFrom-SecureString blob often contains) never
    passes, because ':' is not in the base64 alphabet.
    """
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    return ":" in decoded


def basic_header(raw_value):
    """Authorization value from a stored key, tolerant to its shape.

    Accepts a ready 'Basic ...' string, a bare base64 key, or plain
    'login:password' (plain is encoded here). Shape garbage still goes out
    as a key and earns a clean 401 upstream instead of a web-server 400.
    """
    value = str(raw_value).strip()
    if value.lower().startswith("basic "):
        return "Basic " + value[6:].strip()
    if _looks_like_base64_key(value):
        return "Basic " + value
    return "Basic " + base64.b64encode(value.encode("utf-8")).decode("ascii")


class TargetError(Exception):
    """Store/validation error with a message safe to show to the agent."""


class Target:
    """One registered upstream: url + auth + optional EDT link + comment.

    The "default target" concept is gone: with several EDT sessions each has
    its own base, and the no-base route follows the agent's cwd (see
    RouterContext.resolve_target); the map holds no preference of its own.
    Legacy "default" keys in an old targets.json are simply ignored.
    """

    def __init__(self, alias, url, auth=None, comment="", edt=None,
                 platform=None, connection=None):
        self.alias = alias
        self.url = base_root(url)
        self.auth = dict(auth) if auth else {"type": "none"}
        self.comment = comment or ""
        self.edt = dict(edt) if edt else None
        # Operator fallback for the configurator option (version like
        # 8.3.27.2214 or a full 1cv8.exe path): consulted only when the
        # auto resolution (EDT link -> disk scan -> ibases) answers nothing.
        self.platform = str(platform).strip() if platform else None
        # Normalized 'Srvr="host:port";Ref="ref";' — the configurator's
        # connection source when no EDT link is stored. Credentials are
        # never kept here; filled only from unambiguous sources
        # (the connection argument, a cwd resolve, a named ibases entry).
        self.connection = str(connection).strip() if connection else None

    @property
    def mcp_url(self):
        """MCP endpoint: root + the service path. A stored url that already
        ends in /mcp is used as-is (test mocks, exotic publication names)."""
        if self.url.lower().endswith("/mcp"):
            return self.url
        return self.url + MCP_PATH

    @property
    def update_url(self):
        """Self-update endpoint (RSVData 1.3.5+): root + /hs/rsvdata/update;
        a stored url ending in /mcp drops that tail first (test mocks)."""
        return self._service_url("/update")

    @property
    def remove_url(self):
        """Self-removal endpoint (RSVData 1.3.6+), mirror of update_url."""
        return self._service_url("/remove")

    def _service_url(self, tail):
        base = self.url[: -len("/mcp")] if self.url.lower().endswith("/mcp") else self.url
        return base + ROOT_PATH + tail

    @classmethod
    def from_dict(cls, alias, data):
        if not isinstance(data, dict) or not isinstance(data.get("url"), str):
            raise TargetError(f"Цель «{alias}» в {CONFIG_NAME}: ожидается объект с полем url")
        auth = data.get("auth") or {"type": "none"}
        if not isinstance(auth, dict) or "type" not in auth:
            raise TargetError(f"Цель «{alias}»: auth должен быть объектом с полем type")
        if auth["type"] not in KNOWN_AUTH_TYPES:
            raise TargetError(
                f"Цель «{alias}»: auth.type «{auth['type']}» не поддерживается "
                "(DPAPI убран в 0.10.0) — перенесите ключ в keyring командой "
                f"«rsv-data-router set-secret {alias}», в файле оставьте "
                '"basic" с пустым value или удалите секцию auth')
        edt = data.get("edt")
        if edt is not None and not isinstance(edt, dict):
            raise TargetError(f"Цель «{alias}»: edt должен быть объектом {{workspace, project}}")
        platform = data.get("platform")
        if platform is not None and not isinstance(platform, str):
            raise TargetError(f"Цель «{alias}»: platform должен быть строкой "
                              "(версия платформы или путь к 1cv8.exe)")
        connection = data.get("connection")
        if connection is not None and not isinstance(connection, str):
            raise TargetError(f"Цель «{alias}»: connection должен быть строкой "
                              '(вида Srvr="...";Ref="...";)')
        return cls(alias, data["url"], auth, data.get("comment", ""), edt,
                   platform, connection)

    def as_dict(self):
        result = {"url": self.url, "auth": self.auth, "comment": self.comment}
        if self.edt:
            result["edt"] = self.edt
        if self.platform:
            result["platform"] = self.platform
        if self.connection:
            result["connection"] = self.connection
        return result

    def auth_value(self):
        """The stored key for this target, or None.

        Source order: the inline "basic" value from targets.json (portable
        fallback) first, then the OS keyring entry for the alias. An absent
        or unusable keyring counts as "no key".
        """
        if self.auth.get("type") == "basic":
            value = str(self.auth.get("value", "")).strip()
            if value:
                return value
        return credentials.get_value(self.alias)

    def auth_headers(self):
        """Authorization header for forwarding; {} when there is no key.

        The value shape (base64 key, ready 'Basic ...' string, plain
        'login:password') is normalized by basic_header().
        """
        value = self.auth_value()
        return {"Authorization": basic_header(value)} if value else {}

    def auth_kind(self):
        """Safe-to-display auth description (never the value itself):
        'basic (inline)', 'basic (keyring)' or 'нет'."""
        if self.auth.get("type") == "basic" and str(self.auth.get("value", "")).strip():
            return "basic (inline)"
        if credentials.get_value(self.alias):
            return "basic (keyring)"
        return "нет"


class TargetsStore:
    """Thread-safe alias -> Target map persisted atomically to targets.json."""

    def __init__(self, home_dir):
        self.path = os.path.join(home_dir, CONFIG_NAME)
        self._lock = threading.RLock()
        self._targets = {}
        self._mtime = None
        self._reload()

    # -- public API (thread-safe) --

    def aliases(self):
        with self._lock:
            self._maybe_reload()
            return list(self._targets)

    def get(self, alias):
        with self._lock:
            self._maybe_reload()
            return self._targets.get(alias)

    def snapshot(self):
        with self._lock:
            self._maybe_reload()
            return list(self._targets.values())

    def register(self, alias, url, comment=None, edt=None, platform=None,
                 connection=None):
        """Create or update a target; returns (target, updated_flag).

        Auth is never passed through the chat: on update the stored auth is
        preserved, on create the target starts with an empty basic template
        (the real key lives in the OS keyring — rsv-data-router set-secret;
        an empty inline value falls through to the keyring, and with no key
        at all requests go without Authorization). The same preservation
        applies to comment, the EDT link, platform and the connection
        string: a None argument keeps the stored value.
        """
        with self._lock:
            self._maybe_reload()
            if not isinstance(alias, str) or not ALIAS_RE.match(alias):
                raise TargetError("Недопустимый алиас «%s»: 1–64 символа, буквы/цифры/._-" % alias)
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                raise TargetError(f"Недопустимый url: {url!r} (ожидается http(s)://...)")
            existing = self._targets.get(alias)
            if comment is None:
                comment = existing.comment if existing else ""
            if edt is None:
                edt = existing.edt if existing else None
            if platform is None:
                platform = existing.platform if existing else None
            if connection is None:
                connection = existing.connection if existing else None
            template = existing.auth if existing else {"type": "basic", "value": ""}
            target = Target(alias, url, auth=template, comment=comment, edt=edt,
                            platform=platform, connection=connection)
            self._targets[alias] = target
            self._save()
            return target, existing is not None

    def unregister(self, alias):
        with self._lock:
            self._maybe_reload()
            target = self._targets.pop(alias, None)
            if target is None:
                available = ", ".join(self._targets) or "—"
                raise TargetError(f"База «{alias}» не зарегистрирована. Доступные: {available}")
            self._save()
            return target

    # -- internals --

    def _maybe_reload(self):
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            if self._mtime is not None:
                self._targets, self._mtime = {}, None
            return
        if mtime != self._mtime:
            self._reload()

    def _reload(self):
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            self._targets, self._mtime = {}, None
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            raise TargetError(f"Не удалось прочитать {self.path}: {exc}") from exc
        raw = data.get("targets") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            raise TargetError(f'{self.path}: ожидается формат {{"targets": {{алиас: {{url, auth}}}}}}')
        self._targets = {alias: Target.from_dict(alias, item) for alias, item in raw.items()}
        self._mtime = mtime

    def version(self):
        """Map generation for cache invalidation (targets.json mtime)."""
        with self._lock:
            return self._mtime

    def _save(self):
        payload = {"targets": {alias: t.as_dict() for alias, t in self._targets.items()}}
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:
            raise TargetError(f"Не удалось записать {self.path}: {exc}") from exc
        try:
            self._mtime = os.path.getmtime(self.path)
        except OSError:
            self._mtime = None
