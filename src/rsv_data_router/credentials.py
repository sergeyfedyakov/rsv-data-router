"""OS keyring storage for upstream credentials (the only secret store).

One entry per target alias: service = rsv-data-router, username = alias,
value = a Basic key (base64 of "login:password" or a ready "Basic ..."
string — the same shapes the router accepts inline). The router reads the
entry on every call, so set-secret/remove-secret apply without a restart.

An unusable backend (headless Linux without Secret Service, CI) degrades to
"no secret": calls go out without Authorization instead of breaking the
router. The inline "basic" auth section in targets.json remains as the
portable fallback for such environments.
"""

import base64
import getpass
import sys

import keyring
from keyring.errors import KeyringError

SERVICE = "rsv-data-router"


def get_value(alias):
    """Stored key for the alias, or None (absent, or the backend is unusable)."""
    try:
        value = keyring.get_password(SERVICE, alias)
    except KeyringError:
        return None
    return value.strip() if value and value.strip() else None


def set_value(alias, value):
    keyring.set_password(SERVICE, alias, value)


def delete_value(alias):
    """True when an entry was removed, False when there was none/unusable."""
    try:
        keyring.delete_password(SERVICE, alias)
        return True
    except KeyringError:
        return False


def backend_usable():
    """False when the platform has no working keyring backend (skip e2e)."""
    backend = keyring.get_keyring()
    if "fail" in type(backend).__module__.lower():
        return False
    try:
        keyring.get_password(SERVICE, "__probe__")
    except KeyringError:
        return False
    return True


def encode_key(login, password):
    """The canonical stored shape: base64 of 'login:password'."""
    return base64.b64encode(f"{login}:{password}".encode("utf-8")).decode("ascii")


def prompt_key():
    """(login, password) interactively; with redirected stdin — two lines
    (login, password), so automation can pipe them."""
    if sys.stdin.isatty():
        login = input("Логин: ").strip()
        password = getpass.getpass("Пароль: ")
    else:
        login = sys.stdin.readline().rstrip("\r\n").strip()
        password = sys.stdin.readline().rstrip("\r\n")
    if not login:
        raise SystemExit("Пустой логин — ничего не сохранено.")
    return login, password
