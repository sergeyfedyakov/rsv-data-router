"""HTTP transport to RSVData upstream services (stdlib urllib only).

Every upstream failure goes through explain_failure(): one shared block that
classifies the HTTP status (401 auth, 404 publication, 409 extension/server
version mismatch, 5xx web-server) and ends with the instruction for the
calling agent: do NOT investigate on your own (no ping/curl to the base),
just show the message to the user.
"""

import json
import re
import time
import urllib.error
import urllib.request
import uuid

DEFAULT_TIMEOUT_S = 120

# ping answer of RSVData 1.3.5+: "...; расширение 1.3.5, хеш <base64 SHA-1>".
EXTENSION_RE = re.compile(r"расширение\s+([\d.]+),?\s+хеш\s+([A-Za-z0-9+/=]+)")

AGENT_NOTE = ("Не разбирайте ошибку сами: не запускайте ping, curl и прямые обращения "
              "к базе — просто покажьте это сообщение пользователю.")

BODY_LIMIT = 300


class TransportError(Exception):
    """Upstream transport failure.

    status/body are filled for HTTP-level failures (body is the upstream
    answer, shown to the user for 409 because it names the version mismatch).
    The message is shown to the agent and written to logs, so it must never
    contain credentials: upstream URLs are stored without credentials by design.
    """

    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


def _clean_body(raw_body):
    """Upstream answer as one display line; None for HTML pages or emptiness.

    IIS answers 4xx/5xx with HTML pages that carry no useful text, while the
    409 of the RSVData extension is the version-mismatch message the user
    must see verbatim.
    """
    if raw_body is None:
        return None
    text = raw_body.strip()
    if not text or text.startswith("<"):
        return None
    return text if len(text) <= BODY_LIMIT else text[:BODY_LIMIT] + "…"


def explain_failure(exc):
    """Classify an upstream failure into a user-ready text block.

    401 -> auth setup hint; 404 -> publication missing (told honestly);
    409 -> extension/server version mismatch, server text verbatim (the user
    decides what to do); 5xx -> web-server internal error; other -> raw
    description. The block always ends with AGENT_NOTE.
    """
    status = getattr(exc, "status", None)
    body = _clean_body(getattr(exc, "body", None))
    if status == 401:
        lines = [
            "HTTP 401 — база требует авторизацию, либо ключ не принят (неверный логин/пароль).",
            "Настройте ключ авторизации цели (логин/пароль через чат не передаются):",
            "  rsv-data-router set-secret <алиас>   — ключ хранится в keyring ОС;",
            "перезапуск роутера не нужен.",
        ]
    elif status == 404:
        lines = [
            "HTTP 404 — публикация не найдена: по этому адресу нет базы с сервисом RSVData "
            "(опечатка в url/имени публикации, публикация снята или веб-сервер не настроен).",
        ]
    elif status == 409:
        # 409 is about the PLATFORM: the web-server extension module
        # (wsap) vs the 1C server release — not about the RSVData extension.
        lines = [
            "HTTP 409 — несовпадение версий платформы: модуль расширения веб-сервера и "
            "сервер 1С разных релизов (типовая ситуация после обновления одной из сторон).",
        ]
        if body:
            lines.append(f"Текст ответа сервера: {body}")
    elif status is not None and status >= 500:
        lines = [
            f"HTTP {status} — внутренняя ошибка веб-сервера (5xx): публикация отвечает, "
            "но запрос не обслуживается (частая причина — сервер 1С остановлен или недоступен).",
        ]
    elif status is not None:
        lines = [f"HTTP {status} — неожиданный ответ веб-сервера."]
    else:
        lines = [str(exc) or "Цель недоступна (сеть/веб-сервер не отвечает)"]
    # 401 carries only the auth hint (IIS bodies are noise), 409 already
    # showed its body as the version-mismatch text.
    if body and status not in (401, 409):
        lines.append(f"Ответ сервера: {body}")
    lines.append("")
    lines.append(AGENT_NOTE)
    return "\n".join(lines)


def explain_short(exc):
    """One-line classification for lists (list_targets, log lines)."""
    status = getattr(exc, "status", None)
    if status == 401:
        return "HTTP 401 — нужна авторизация или ключ не принят"
    if status == 404:
        return "HTTP 404 — публикация не найдена"
    if status == 409:
        return "HTTP 409 — рассинхрон версий платформы (веб-сервер vs сервер 1С)"
    if status is not None and status >= 500:
        return f"HTTP {status} — внутренняя ошибка веб-сервера"
    if status is not None:
        return f"HTTP {status} — неожиданный ответ"
    return "цель недоступна (сеть/веб-сервер не отвечает)"


def post_json(url, body, headers=None, timeout_s=DEFAULT_TIMEOUT_S):
    """POST raw JSON bytes to the upstream, return (status, body_bytes).

    Raises TransportError on network errors and on non-200 upstream status:
    1C answers JSON-RPC with HTTP 200, so anything else is an infrastructure
    error (401 wrong/no auth, 404 publication missing, 5xx web server).
    """
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json; charset=utf-8")
    request.add_header("Accept", "application/json, text/event-stream")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        try:
            raw_body = exc.read().decode("utf-8", "replace")
        except OSError:
            raw_body = ""
        raise TransportError(f"Цель {url} ответила HTTP {exc.code}",
                             status=exc.code, body=raw_body) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None) or exc
        raise TransportError(f"Цель недоступна: {url} ({reason})") from exc
    except OSError as exc:
        raise TransportError(f"Цель недоступна: {url} ({exc})") from exc


def call_tool(url, name, arguments, headers=None, timeout_s=DEFAULT_TIMEOUT_S):
    """Send a tools/call JSON-RPC to the upstream, return its text content.

    Raises TransportError for network/HTTP failures; RuntimeError for a
    JSON-RPC error envelope (that error is already a meaningful answer, e.g.
    a bad query — it passes through without the AGENT_NOTE decoration).
    A tool-level result (isError=true included) is returned as text — it is
    already the answer for the caller.
    """
    request = {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex[:8],
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    body = json.dumps(request, ensure_ascii=False).encode("utf-8")
    status, raw = post_json(url, body, headers, timeout_s)
    try:
        answer = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise TransportError(f"Цель {url} вернула не-JSON ответ (HTTP {status})",
                             status=status) from exc
    if not isinstance(answer, dict):
        raise TransportError(f"Цель {url}: неожиданный формат ответа", status=status)
    error = answer.get("error")
    if error:
        raise RuntimeError(f"Цель {url}: ошибка JSON-RPC {error.get('code')}: {error.get('message')}")
    result = answer.get("result") or {}
    parts = [item.get("text", "") for item in result.get("content", [])
             if isinstance(item, dict) and item.get("type") == "text"]
    return "\n".join(part for part in parts if part)


def parse_extension_info(text):
    """(version, hash) from a ping answer; None when absent (< RSVData 1.3.5)."""
    match = EXTENSION_RE.search(text or "")
    return (match.group(1), match.group(2)) if match else None


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")
