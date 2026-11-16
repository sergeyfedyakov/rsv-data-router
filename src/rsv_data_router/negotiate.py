"""Windows authentication transport (Negotiate/NTLM) for wa targets.

The legs run over ONE keep-alive http.client connection: IIS performs
Windows authentication in kernel mode (http.sys) and binds it to the TCP
connection («Persistent-Auth: true»), so every leg — and the final POST —
must reuse the same connection. urllib is unusable here: it always sends
`Connection: close`, each leg lands on a fresh connection, the server
restarts the handshake and the client starves at the leg limit (live
diagnosis 16.11, .ai/done/2026-11-16-diagnostika-windows-auth-rsvdata.md).

The SSPI tokens come from the optional `windows-auth` extra: sspilib
directly on Windows (the exact path proven against kernel-mode IIS),
pyspnego as the cross-platform fallback. No credentials are passed
explicitly: Windows authenticates the OS account of the router process,
which the publication maps to an infobase user with OS authentication
(«Аутентификация ОС»). End to end this works single-hop — the web server
and the 1C server cluster on one machine (analysis of 07.09); the router
normally runs on the server itself (--network/--token).

This module must not import forward (forward imports it for wa=True
requests); WaError mirrors TransportError's status/body fields so forward
can translate it losslessly.
"""

import base64
import http.client
from urllib.parse import urlsplit

# Empty challenge + up to four challenge/response round-trips. The working
# sequence is exactly 3 requests (live 16.11); proxies or a context restart
# (the server re-challenges a completed context) may need one-two more.
LEG_LIMIT = 5

INSTALL_HINT = ("модули sspilib/pyspnego не установлены — для целей с "
                "Windows-аутентификацией поставьте пакет с extra: "
                "pip install rsv-data-router[windows-auth]")


class WaError(Exception):
    """Windows-auth transport failure; status/body are filled for HTTP-level
    failures, mirroring forward.TransportError."""

    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


def _client_context(protocol, hostname=None):
    """SSPI/GSSAPI client context under the router process's OS account.

    sspilib is tried first (Windows; proven against kernel-mode IIS with
    the SPN HTTP/<host>), pyspnego second (cross-platform)."""
    try:
        import sspilib
    except ImportError:
        sspilib = None
    if sspilib is not None:
        try:
            cred = sspilib.UserCredential(protocol=protocol, usage="initiate")
            spn = f"HTTP/{hostname}" if hostname else None
            return (sspilib.ClientSecurityContext(cred, spn) if spn
                    else sspilib.ClientSecurityContext(cred))
        except Exception as exc:
            raise WaError(f"SSPI-контекст не создан ({exc})") from exc
    try:
        import spnego
    except ImportError:
        raise WaError(INSTALL_HINT) from None
    kwargs = {"protocol": protocol}
    if hostname:
        kwargs.update(hostname=hostname, service="HTTP")
    try:
        return spnego.client(**kwargs)
    except Exception as exc:
        raise WaError(f"SSPI-контекст не создан ({exc})") from exc


# Test seam: stage16 replaces this with a scripted stub (no real SSPI in CI).
_client_factory = _client_context


def _parse_challenge(headers):
    """(scheme, in_token) from the 401's WWW-Authenticate: the preferred
    scheme (Negotiate beats NTLM) and its decoded challenge; (None, None)
    when the response offers neither. IIS may send SEVERAL separate
    WWW-Authenticate lines (Negotiate and NTLM), so all of them are read
    (get_all), not just the first. A bare scheme name (first leg) yields a
    None token; a malformed challenge yields None and lets the context fail
    loudly on the next step."""
    if headers is None:
        return None, None
    get_all = getattr(headers, "get_all", None)
    values = (get_all("WWW-Authenticate") if get_all is not None
              else None) or []
    if not values:
        value = headers.get("WWW-Authenticate")
        values = [value] if value else []
    for scheme in ("Negotiate", "NTLM"):
        for value in values:
            for part in str(value).split(","):
                part = part.strip()
                lowered = part.lower()
                if lowered == scheme.lower():
                    return scheme, None
                if lowered.startswith(scheme.lower() + " "):
                    token = part[len(scheme) + 1:].strip()
                    try:
                        return scheme, (base64.b64decode(token) if token else None)
                    except (ValueError, UnicodeDecodeError):
                        return scheme, None
    return None, None


def post_json(url, body, headers=None, timeout_s=120):
    """POST raw JSON bytes with Negotiate/NTLM; return (status, body_bytes).

    ALL legs and the final request go over one keep-alive connection
    (kernel-mode auth binds to the TCP connection); every response body is
    read out fully so the connection stays reusable.

    Raises WaError on network errors, non-401 HTTP failures, a challenge
    that offers neither Negotiate nor NTLM, SSPI failures and an unfinished
    handshake after LEG_LIMIT requests.
    """
    parts = urlsplit(url)
    conn_cls = (http.client.HTTPSConnection if parts.scheme == "https"
                else http.client.HTTPConnection)
    conn = conn_cls(parts.hostname, parts.port, timeout=timeout_s)
    context = None
    scheme = None
    auth_value = None
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    try:
        for _ in range(LEG_LIMIT):
            request_headers = {
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json, text/event-stream",
                "Connection": "keep-alive",
            }
            request_headers.update(headers or {})
            if auth_value:
                request_headers["Authorization"] = f"{scheme} {auth_value}"
            conn.request("POST", path, body=body, headers=request_headers)
            response = conn.getresponse()
            raw = response.read()
            if response.status != 401:
                if response.status >= 400:
                    raise WaError(f"Цель {url} ответила HTTP {response.status}",
                                  status=response.status,
                                  body=raw.decode("utf-8", "replace"))
                return response.status, raw
            challenge, in_token = _parse_challenge(response.headers)
            if challenge is None:
                raise WaError(
                    f"Цель {url} ответила 401 без предложения Negotiate/NTLM "
                    "(WWW-Authenticate пуст или предлагает другой механизм; "
                    "возможно, публикация ждёт Basic-авторизацию, а не windows)",
                    status=401, body=raw.decode("utf-8", "replace"))
            scheme = challenge
            if context is not None and getattr(context, "complete", False):
                # The context finished but the server still challenges: the
                # kernel-mode auth state lives on the TCP connection and the
                # connection was lost — restarting the handshake is the only
                # meaningful move (calling step() on a completed context
                # fails with «handle invalid» and masks the real cause).
                context = None
                auth_value = None
            if context is None:
                context = _client_factory(
                    "negotiate" if challenge.lower() == "negotiate" else "ntlm",
                    parts.hostname)
            try:
                out_token = context.step(in_token)
            except Exception as exc2:
                raise WaError(f"Рукопожатие Windows-аутентификации не удалось: "
                              f"ошибка SSPI/NTLM ({exc2})", status=401) from exc2
            if not out_token:
                raise WaError("Рукопожатие Windows-аутентификации не удалось: "
                              "контекст SSPI не выдал токен", status=401)
            auth_value = base64.b64encode(out_token).decode("ascii")
    except (http.client.HTTPException, OSError) as exc:
        raise WaError(f"Цель недоступна: {url} ({exc})") from exc
    finally:
        conn.close()
    raise WaError(
        f"Цель {url}: рукопожатие Negotiate/NTLM не завершено — после "
        f"{LEG_LIMIT} запросов сервер продолжает требовать авторизацию "
        f"(схема {scheme}). Проверьте Windows-аутентификацию публикации и "
        "маппинг учётной записи процесса роутера в пользователя ИБ.",
        status=401)
