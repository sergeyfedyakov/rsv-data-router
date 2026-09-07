"""Primary RSVData installation/removal chains for the service tools.

Install chain (register_target install=True, revised after the live test,
06.09.2026): /update first; any failure there only closes the HTTP path —
the chain moves on to EDT via the MCP:RSV HTTP server (router acts as an
MCP client), then to the Configurator CLI (update_designer logic). The web
publication gates NOTHING here: EDT and the configurator reach the infobase
without it, it is needed only for the final availability check. That check
ends every chain run with a diagnosis (publication and/or auth key missing,
each with its next step). Every step reports in user-ready lines; the chain
never raises — it always returns a report. The configurator's stored key
is OPTIONAL (user decision, 07.09): bases without users take a run without
/N /P, and configurator failures carry the /Out log tail AS IS in the
report (no classification yet — which markers matter is unknown).

Removal chain (unregister_target uninstall=True, 0.7.0) mirrors the install
order with /remove FIRST (user decision): the self-removal endpoint needs
nothing but the publication; EDT (uninstallExtension) and the configurator
(/DeleteCfg -Extension) stay for what /remove cannot serve — extensions
older than 1.3.6, dead publication, rejection. The chain returns whether
the extension is gone: gone (or disabled) — the caller deletes the target
record; not gone — the record stays with the reason and the next step.

Both chains execute in a worker thread (asyncio.to_thread), so blocking
HTTP/subprocess work never stalls the router event loop; the MCP:RSV branch
starts its own loop there with asyncio.run (a worker thread has none).
"""

import asyncio
import json
import os
import subprocess
import urllib.error
import urllib.request

from . import forward
from . import update_designer

PUBLICATION_TIMEOUT_S = 10
UPDATE_TIMEOUT_S = 120
RSV_INSTALL_TIMEOUT_S = 50
RSV_REMOVE_TIMEOUT_S = 50
EXTENSION_NAME = "RSVData"


def root_answers(target):
    """(answers, detail): does anything HTTP answer at the publication root.

    Any status except 404 counts as an existing publication (401/403 are
    the web server guarding it); 404 and network errors leave it unproven
    — either no publication or a bare vdir without a default document.
    detail is a short reason for the negative case.
    """
    request = urllib.request.Request(target.url, method="GET")
    for name, value in target.auth_headers().items():
        request.add_header(name, value)
    try:
        try:
            with urllib.request.urlopen(request, timeout=PUBLICATION_TIMEOUT_S) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
    except (urllib.error.URLError, OSError) as exc:
        return False, f"корень не отвечает ({str(exc)[:120]})"
    if status == 404:
        return False, "корень ответил HTTP 404"
    return True, f"корень отвечает (HTTP {status})"


def selfupdate(cfe_path, target, missing_line=None, short_errors=False,
               chain=False):
    """(kind, lines) from POST <update_url>; kind: done | next | stop.

    404 means the endpoint does not exist yet (extension missing or older
    than 1.3.5). chain=True (the register_target install chain) turns
    EVERY failure into "next": the publication state must not stop the
    chain, EDT and the configurator work without it; the mass update keeps
    the strict stop classification (explain_short when short_errors, so a
    batch report stays compact).
    """
    if not cfe_path or not os.path.isfile(cfe_path):
        return "stop", ["Бинарник RSVData роутеру не выдан (--rsvdatabinary) — "
                        "установка отменена."]
    try:
        with open(cfe_path, "rb") as handle:
            body = handle.read()
        status, raw = forward.post_json(target.update_url, body,
                                        target.auth_headers(), UPDATE_TIMEOUT_S)
    except forward.TransportError as exc:
        if getattr(exc, "status", None) == 404:
            return "next", [missing_line
                            or "Точки /update нет (расширение отсутствует или старее "
                               "1.3.5) — пробую через EDT."]
        if chain:
            return "next", [f"Точка /update недоступна ({forward.explain_short(exc)}) — "
                            "установка пойдёт через EDT/конфигуратор."]
        return "stop", [forward.explain_short(exc) if short_errors
                        else forward.explain_failure(exc)]
    try:
        answer = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        if chain:
            return "next", [f"Точка /update вернула не-JSON ответ (HTTP {status}) — "
                            "установка пойдёт через EDT/конфигуратор."]
        return "stop", [f"Точка /update вернула не-JSON ответ (HTTP {status}): "
                        f"{raw[:200]!r}"]
    result = answer.get("результат")
    if result == "обновлено":
        was = answer.get("было") or {}
        became = answer.get("стало") or {}
        return "done", [f"Расширение обновлено через /update: "
                        f"{was.get('версия', '?')} -> {became.get('версия', '?')} "
                        f"(хеш {became.get('хеш', '?')})."]
    if result == "пропуск":
        return "done", ["Расширение актуально: /update вернул «пропуск» (хеш совпал)."]
    if result == "отклонено":
        problems = "; ".join(answer.get("проблемы") or []) or "без деталей"
        if chain:
            return "next", [f"Точка /update отклонила бинарник: {problems} — "
                            "пробую через EDT/конфигуратор."]
        return "stop", [f"Точка /update отклонила бинарник: {problems}"]
    if chain:
        return "next", ["Точка /update вернула неизвестный ответ — "
                        "пробую через EDT/конфигуратор."]
    return "stop", [f"Точка /update вернула неизвестный ответ: {str(answer)[:200]}"]


def rsv_branch(rsv_mcp, target, cfe_path):
    """(done, lines) for the MCP:RSV attempt; worker-thread entry that runs
    its own event loop."""
    if not rsv_mcp:
        return False, ["Вариант EDT пропущен: адрес MCP:RSV не задан (--rsvmcp)."]
    if not target.edt:
        return False, ["Вариант EDT пропущен: у цели нет EDT-связи "
                       "{workspace, project} в targets.json."]
    return asyncio.run(_rsv_install(rsv_mcp, target, cfe_path))


PLATFORM_REFUSAL = (
    "не определена версия платформы базы: EDT-связь цели, скан воркспейсов "
    "EDT на диске и ibases.v8i ничего не дали. Запросите у пользователя "
    "номер версии платформы (например 8.3.27.2214) или путь к 1cv8.exe и "
    "повторите с ключом platform: register_target(base=..., platform=...) "
    "или unregister_target(base=..., platform=..., uninstall=true).")

URL_REGISTRATION_REFUSAL = (
    "у цели нет строки подключения и EDT-связи — регистрация только по url, "
    "установка расширения на такую базу недоступна. Использовать базу можно; "
    "установку выполнит администратор, удаление — только через точку /remove. "
    "Чтобы установкой управлял роутер, перерегистрируйте цель с connection, "
    "cwd (проект EDT) или точным названием базы — строка подключения будет "
    "сохранена в записи.")


def _explicit_platform(target):
    """(designer_path, version) from the per-target platform key.

    Operator fallback ONLY: auto resolution (update_designer.
    base_platform_version) wins whenever it answers. A value with a path
    separator or an .exe tail is a configurator binary; anything else is a
    platform version.
    """
    value = str(getattr(target, "platform", "") or "").strip()
    if not value:
        return None, None
    if "\\" in value or "/" in value or value.lower().endswith(".exe"):
        return value, None
    return None, value


def _designer_for(target):
    """(designer, version, note, refusal) for the configurator option.

    Auto sources first (EDT link -> disk scan -> ibases Version), the
    per-target platform key last; version=None means resolve_designer may
    fall back to the SINGLE installed platform (with a note) and refuses
    when several are installed. refusal carries the ask-the-user text.
    """
    key_path, key_version = _explicit_platform(target)
    if key_path:
        return key_path, None, None, None
    version = update_designer.base_platform_version(target) or key_version
    try:
        return (*update_designer.resolve_designer(version), None)
    except SystemExit as exc:
        return None, None, None, str(exc)


def designer_branch(target, cfe_path, home):
    """(done, lines) for the Configurator CLI attempt (update_designer logic).

    The stored key is OPTIONAL (user decision, 07.09): with none the
    configurator runs without /N /P (bases without users) and the report
    says so up front; a present-but-unreadable key refuses the variant.
    Failures carry the /Out log tail AS IS — which markers matter is not
    known yet, no classification."""
    designer, version, note, refusal = _designer_for(target)
    if refusal:
        return False, [f"Вариант конфигуратора недоступен: {refusal}"]
    if designer and not os.path.isfile(designer):
        return False, [f"Вариант конфигуратора недоступен: файл не найден ({designer})."]
    try:
        login, password, auth_note = update_designer.optional_login_password(target)
        srvr, ref, ibase_name = update_designer.resolve_connection(target)
    except (SystemExit, RuntimeError) as exc:
        return False, [f"Вариант конфигуратора недоступен: {exc}"]
    lines = [auth_note] if auth_note else []
    log_base = os.path.join(home, f".designer-{target.alias}")
    try:
        update_designer.run_designer(
            update_designer.build_command(designer, srvr, ref, login, password,
                                          log_base + "-load.log", cfe_path, "load"),
            log_base + "-load.log", "загрузка .cfe")
        update_designer.run_designer(
            update_designer.build_command(designer, srvr, ref, login, password,
                                          log_base + "-update.log", cfe_path, "update"),
            log_base + "-update.log", "обновление конфигурации БД расширения")
    except update_designer.DesignerError as exc:
        lines.append(f"Установка через конфигуратор не удалась: {exc}")
        lines.extend(exc.log_lines)
        return False, lines
    except (SystemExit, subprocess.SubprocessError, OSError) as exc:
        lines.append(f"Установка через конфигуратор не удалась: {exc}")
        return False, lines
    label = version or os.path.basename(designer)
    tail = f" ({note})" if note else ""
    lines.append(f"Расширение установлено через конфигуратор "
                 f"(платформа {label}, база {ibase_name}){tail}.")
    return True, lines


def _auth_missing(target):
    """True when no usable auth value is stored (neither inline nor keyring)."""
    return target.auth_kind() == "нет"


def _auth_lines(target, header):
    """Failure lines for HTTP 401: the publication exists (the web server
    challenged), the auth key is missing or rejected — next step: set-secret."""
    lines = [f"{header}: публикация есть, но база требует авторизацию (HTTP 401)."]
    if _auth_missing(target):
        lines.append("Данные авторизации не заданы. Следующий шаг — добавить ключ "
                     "(логин/пароль через чат не передаются):")
        lines.append(f"  rsv-data-router set-secret {target.alias}")
    else:
        lines.append(f"Ключ авторизации не принят (неверный логин/пароль). "
                     f"Следующий шаг — обновить ключ цели «{target.alias}» "
                     "(rsv-data-router set-secret).")
    lines.append("Ключ хранится в keyring ОС; перезапуск роутера не нужен. "
                 "После добавления ключа повторить проверку: ping (base=<алиас>) "
                 "или list_targets(check=true) — установка расширения при повторе "
                 "не требуется.")
    lines.append("")
    lines.append(forward.AGENT_NOTE)
    return lines


def availability_check(target):
    """Availability report: ping + diagnosis with next steps; never raises.

    Shared by register_target check=true and the tail of the install chain.
    Every failure names its next step: 401 — add/fix the auth key; 404 and
    network errors — the publication is missing or the rsvdata service is
    unpublished (the root probe separates the two), and with an empty auth
    the report carries both messages at once. 409/5xx keep the shared
    classified block (explain_failure).
    """
    try:
        answer = forward.call_tool(target.mcp_url, "ping", {},
                                   target.auth_headers(), 20)
    except forward.TransportError as exc:
        status = getattr(exc, "status", None)
        if status == 401:
            return _auth_lines(target, "Проверка связи: НЕ ПРОШЛА (Запись сохранена).")
        if status == 404 or status is None:
            answers, detail = root_answers(target)
            if answers:
                if status == 404:
                    first = (f"Проверка связи: НЕ ПРОШЛА (Запись сохранена): публикация "
                             f"есть, но сервис rsvdata по адресу {target.mcp_url} не "
                             f"отвечает (HTTP 404).")
                else:
                    first = (f"Проверка связи: НЕ ПРОШЛА (Запись сохранена): публикация "
                             f"есть ({detail}), но сервис rsvdata не отвечает "
                             f"({forward.explain_short(exc)}).")
                lines = [first,
                         "Следующий шаг: опубликовать сервис rsvdata в публикации базы, "
                         "затем повторить проверку: ping (base=<алиас>) или "
                         "list_targets(check=true)."]
            else:
                lines = [f"Проверка связи: НЕ ПРОШЛА (Запись сохранена): публикация не "
                         f"обнаружена ({target.url}: {detail}).",
                         "Следующий шаг: выполнить публикацию базы на веб-сервере с "
                         "сервисом rsvdata, затем повторить проверку: ping "
                         "(base=<алиас>) или list_targets(check=true)."]
                if _auth_missing(target):
                    lines.append("Данные авторизации тоже не заданы — добавить ключ "
                                 "(rsv-data-router set-secret <алиас>).")
            lines.append("")
            lines.append(forward.AGENT_NOTE)
            return lines
        return ["Проверка связи: НЕ ПРОШЛА (Запись сохранена).",
                "", forward.explain_failure(exc)]
    except RuntimeError as exc:
        return [f"Проверка связи: НЕ ПРОШЛА (Запись сохранена) — {exc}."]
    first = answer.splitlines()[0] if answer else "pong"
    lines = [f"Проверка связи: ок — {first}"]
    info = forward.parse_extension_info(answer)
    if info:
        lines.append(f"Расширение RSVData: {info[0]}, хеш {info[1]}.")
    else:
        lines.append("Расширение RSVData: версии нет в ответе (старее 1.3.5 — "
                     "без /update, обновите вручную, см. update_designer.py).")
    return lines


MASS_MISSING_LINE = ("точки /update нет — расширение отсутствует или старее 1.3.5; "
                     "обновление невозможно, первичная установка: "
                     "register_target(install=true).")


PING_TIMEOUT_S = 15


def preflight(target, remove=False, cfe_path=None):
    """(go, refusal_lines) decided BEFORE an install/removal job starts.

    For installs the RSVData binary itself is precondition #0 (user
    decision, 07.09): when --rsvdatabinary did not provide an existing
    file, the refusal comes right here — before the ping, with no doomed
    job (the same fail-fast as get_rsvdata / update_targets). Removal
    never needs the binary (empty-body /remove, EDT uninstallExtension,
    /DeleteCfg), so remove=True skips this check.

    A background job is worth creating only when some chain variant can
    plausibly work: the service answers ping with an extension version
    (the self-update/removal endpoint may exist), or the EDT variant has a
    link to try, or the configurator variant has BOTH resolvable inputs —
    a connection source (EDT link or the stored connection string; nothing
    is derived from the publication url — user decision, 07.09) and a
    platform (auto sources, the per-target key or the single installed
    one). Everything else is known-dead before the launch — the refusal
    carries the availability diagnosis plus the matching next step (the
    url-registration ask when no connection source exists, otherwise the
    platform ask) and no doomed job is created. One ping + local file
    reads; never raises.
    """
    if not remove and not (cfe_path and os.path.isfile(cfe_path)):
        return False, [
            "Установка НЕ запущена (фоновое задание не создавалось): бинарник "
            "RSVData роутеру не выдан (--rsvdatabinary) — установить "
            "расширение нечем.",
            "",
            "Запустите роутер с --rsvdatabinary <путь к .cfe> и повторите "
            "register_target(install=true, check=true).",
        ]
    ping_version = None
    try:
        answer = forward.call_tool(target.mcp_url, "ping", {},
                                   target.auth_headers(), PING_TIMEOUT_S)
        ping_version = (forward.parse_extension_info(answer) or (None,))[0]
    except (forward.TransportError, RuntimeError):
        pass
    if ping_version:
        return True, []
    key_path, key_version = _explicit_platform(target)
    # The SINGLE installed platform doubles as a configurator answer even
    # when no source names the base's version (user question, 06.09).
    _, single_path = update_designer.single_installed_designer()
    platform_known = bool(update_designer.base_platform_version(target)
                          or key_version or key_path or single_path)
    if target.edt or (target.connection and platform_known):
        return True, []
    lines = [f"{'Удаление НЕ запущено' if remove else 'Установка НЕ запущена'} "
             "(фоновое задание не создавалось): заранее известно, что ни один "
             "вариант цепочки не выполнится.", ""]
    lines.extend(availability_check(target))
    lines.append("")
    lines.append(URL_REGISTRATION_REFUSAL if not target.edt and not target.connection
                 else PLATFORM_REFUSAL)
    return False, lines


def run_mass_update(ctx, targets):
    """Sequential self-update report for the update_targets tool.

    Same /update step as the install chain (selfupdate), nothing else:
    no registration and no EDT/configurator branches — those exist for
    PRIMARY installation on bases without the endpoint, silently running
    a configurator across every target would be wrong. Reports, never
    raises; per-target lines + a summary.
    """
    cfe_path = getattr(ctx, "rsvdata_path", None)
    size = os.path.getsize(cfe_path) if cfe_path and os.path.isfile(cfe_path) else 0
    lines = [f"Массовое обновление RSVData: целей {len(targets)}, "
             f"бинарник {cfe_path} ({size} байт)."]
    counts = {"done": 0, "next": 0, "stop": 0}
    for target in targets:
        kind, step = selfupdate(cfe_path, target,
                                missing_line=MASS_MISSING_LINE,
                                short_errors=True)
        counts[kind] += 1
        lines.extend(f"  {target.alias}: {text}" for text in step)
    summary = (f"Итог: обновлено/актуально {counts['done']}, "
               f"нет точки /update {counts['next']}, ошибок {counts['stop']}.")
    lines.append(summary)
    if counts["stop"]:
        lines.append("Блоки ошибок покажите пользователю как есть; диагностику "
                     "самостоятельно не запускать.")
        lines.append(forward.AGENT_NOTE)
    lines.append("Новый код подхватывают новые HTTP-сеансы (пул живёт до ~25 с) — "
                 "сразу после обновления ping может ещё показывать старую версию.")
    return lines


def _emit(sink, items):
    """Feed the job sink (if any) with the lines a step just produced."""
    if sink:
        sink.extend(items)


def run_chain(ctx, target, sink=None):
    """Full install chain report for register_target(install=True, check=True).

    /update first; any failure there only closes the HTTP path — EDT and
    then the configurator are tried regardless of the publication state.
    Ends with availability_check, which names what is still missing
    (publication and/or auth) and the next step for each; a failed chain
    points at the re-run. sink (a job, optional) receives every step's
    lines as the chain goes, so a running job_status shows progress; the
    sink ends up holding the full report.
    """
    cfe_path = getattr(ctx, "rsvdata_path", None)
    head = ["Установка расширения RSVData (вариант конфигуратора может "
            "занимать пару минут)…"]
    lines = list(head)
    _emit(sink, head)
    kind, step = selfupdate(cfe_path, target, chain=True)
    lines.extend(step)
    _emit(sink, step)
    if kind == "done":
        tail = availability_check(target)
        lines.extend(tail)
        _emit(sink, tail)
        return lines
    done, step = rsv_branch(getattr(ctx, "rsv_mcp", ""), target, cfe_path)
    lines.extend(step)
    _emit(sink, step)
    if done:
        tail = availability_check(target)
        lines.extend(tail)
        _emit(sink, tail)
        return lines
    done, step = designer_branch(target, cfe_path, ctx.home)
    lines.extend(step)
    _emit(sink, step)
    if done:
        tail = availability_check(target)
        lines.extend(tail)
        _emit(sink, tail)
        return lines
    tail = ["Установка не выполнена ни одним из способов. После устранения "
            "замечаний выше повторить: register_target(install=true, "
            "check=true) или update_targets."]
    tail.extend(availability_check(target))
    lines.extend(tail)
    _emit(sink, tail)
    return lines


# ---- Removal chain (unregister_target uninstall=True, mirror of the install) ----

def selfremove(target):
    """(kind, lines) from POST <remove_url> with an empty body;
    kind: done | next.

    RSVData 1.3.6+ deletes itself; when active sessions hold the base the
    platform refuses and the extension disables itself instead («отключено»)
    — that still ends the chain as done: the extension stops loading, the
    caller deletes the target record, the report names the leftover and how
    to finish the job later. Everything else (no endpoint on extensions
    older than 1.3.6, dead publication, rejection, unknown answer) is
    "next": EDT and the configurator reach the infobase without the web
    publication, so the chain moves on. Right after «удалено» the session
    pool serves old code for ~25–30 s (spike C6) — the report says so, so a
    500/404 probe is not mistaken for a failed removal.
    """
    try:
        status, raw = forward.post_json(target.remove_url, b"",
                                        target.auth_headers(), UPDATE_TIMEOUT_S)
    except forward.TransportError as exc:
        if getattr(exc, "status", None) == 404:
            return "next", ["Точки /remove нет (расширение старее 1.3.6) — "
                            "удаление пойдёт через EDT/конфигуратор."]
        return "next", [f"Точка /remove недоступна ({forward.explain_short(exc)}) — "
                        "удаление пойдёт через EDT/конфигуратор."]
    try:
        answer = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "next", [f"Точка /remove вернула не-JSON ответ (HTTP {status}) — "
                        "удаление пойдёт через EDT/конфигуратор."]
    result = answer.get("результат")
    if result == "удалено":
        was = answer.get("было") or {}
        return "done", [f"Расширение удалено через /remove (была версия "
                        f"{was.get('версия', '?')}).",
                        "Переходное окно ~25–30 с: обращения к /hs/rsvdata/* могут "
                        "отвечать 500 «Расширение RSVData не найдено», затем 404 — "
                        "это норма, а не ошибка удаления."]
    if result == "отключено":
        problems = "; ".join(answer.get("проблемы") or [])
        return "done", ["Удалить расширение сейчас не удалось (базу держат активные "
                        "сеансы) — оно ОТКЛЮЧЕНО и больше не загружается."
                        + (f" Проблемы: {problems}" if problems else ""),
                        "Доудалить позже: повторить unregister_target(uninstall=true) "
                        "на только что зарегистрированной цели или удалить вручную "
                        "(конфигуратор /DeleteCfg -Extension RSVData)."]
    if result == "отклонено":
        problems = "; ".join(answer.get("проблемы") or []) or "без деталей"
        return "next", [f"Точка /remove отклонила удаление: {problems} — "
                        "пробую через EDT/конфигуратор."]
    return "next", ["Точка /remove вернула неизвестный ответ: "
                    f"{str(answer)[:200]} — удаление пойдёт через EDT/конфигуратор."]


def _format_holders(holders):
    """Held-by list from the platform as one display line (defensive: the
    plugin ships dicts with user/application fields, but any shape prints)."""
    names = []
    for item in holders or []:
        if isinstance(item, dict):
            parts = [str(item[key]) for key in ("user", "userName", "application", "name")
                     if item.get(key)]
            names.append("/".join(parts) or json.dumps(item, ensure_ascii=False))
        else:
            names.append(str(item))
    return "; ".join(names) or "неизвестные сеансы"


def _find_edt_project(data, target):
    """(edt_workspace, port, miss_line): where the target's project lives.

    Matches the target's {workspace, project} against the FULL
    list_workspace_projects answer. Several EDT instances run side by side,
    each with its own MCP:RSV port; the connected instance lists its own
    projects at the top level and the neighbors in otherEdtInstances
    (entries there carry no location/isOpen — a running instance lists only
    its open projects). The connected workspace answers (None, None, None) —
    no addressing needed; a neighbor answers its edtWorkspace (short name,
    full-path fallback) and port for the edtWorkspace parameter that routes
    the follow-up call there. Nothing anywhere — a miss line for the report.
    """
    workspace = os.path.normcase(os.path.normpath(target.edt["workspace"]))
    name = target.edt["project"]
    for item in data.get("projects") or []:
        if item.get("name") != name:
            continue
        parent = os.path.dirname(os.path.normpath(item.get("location", "")))
        if os.path.normcase(parent) != workspace:
            continue  # same name elsewhere: the target workspace may sit in
            # another instance, keep looking
        if item.get("isOpen", False):
            return None, None, None
        return None, None, (f"проект {name} есть в воркспейсе "
                            f"{target.edt['workspace']}, но закрыт в EDT — "
                            "откройте его и повторите.")
    for instance in data.get("otherEdtInstances") or []:
        if os.path.normcase(os.path.normpath(instance.get("workspace", ""))) != workspace:
            continue
        if any(p.get("name") == name for p in instance.get("projects") or []):
            return (instance.get("edtWorkspace") or instance.get("workspace"),
                    instance.get("port"), None)
    return None, None, (f"проект {name} не открыт ни в одной запущенной EDT "
                        f"(ищем воркспейс {target.edt['workspace']}).")


def _routed_note(edt_workspace, port):
    """Report line for a project found in a neighboring EDT instance."""
    if not edt_workspace:
        return []
    where = str(edt_workspace) + (f", порт {port}" if port else "")
    return [f"Проект найден в другой запущенной EDT ({where}) — вызов "
            "адресован ей параметром edtWorkspace."]


async def _rsv_install(rsv_mcp, target, cfe_path):
    """MCP client to the MCP:RSV server inside EDT: is the target's project
    open (in any running EDT instance), and installExtension into its bound
    infobase. Returns (done, lines)."""
    from fastmcp import Client

    try:
        async with Client(rsv_mcp) as client:
            listing = await client.call_tool("list_workspace_projects", {})
            data = json.loads(listing.content[0].text)
            edt_workspace, port, miss = _find_edt_project(data, target)
            if miss:
                return False, [f"Вариант EDT пропущен: {miss}"]
            arguments = {
                "operation": "installExtension",
                "name": EXTENSION_NAME,
                "projectName": target.edt["project"],
                "cfePath": os.path.abspath(cfe_path),
                "timeoutSeconds": RSV_INSTALL_TIMEOUT_S,
            }
            routed = _routed_note(edt_workspace, port)
            if edt_workspace:
                arguments["edtWorkspace"] = edt_workspace
            result = await client.call_tool("edit_metadata", arguments)
            answer = result.content[0].text if result.content else ""
            try:
                payload = json.loads(answer)
                ok = payload.get("success", True)
            except ValueError:
                ok = True
            if not ok:
                return False, routed + ["Вариант EDT не удался:", f"  {answer[:300]}",
                                        "— пробую конфигуратор."]
            return True, routed + ["Расширение установлено через EDT (MCP:RSV)."]
    except Exception as exc:  # noqa: BLE001 — report, never raise
        return False, [f"Вариант EDT недоступен ({exc}) — пробую конфигуратор."]


async def _rsv_remove(rsv_mcp, target):
    """MCP client to the MCP:RSV server inside EDT: is the target's project
    open (in any running EDT instance), and uninstallExtension from its
    bound infobase. Returns (kind, lines), kind: done | next | pending.
    pending means EDT started the removal in the background (client-server
    bases take minutes): the chain stops and the record stays until a rerun
    picks the result up."""
    from fastmcp import Client

    try:
        async with Client(rsv_mcp) as client:
            listing = await client.call_tool("list_workspace_projects", {})
            data = json.loads(listing.content[0].text)
            edt_workspace, port, miss = _find_edt_project(data, target)
            if miss:
                return "next", [f"Вариант EDT пропущен: {miss}"]
            arguments = {
                "operation": "uninstallExtension",
                "name": EXTENSION_NAME,
                "projectName": target.edt["project"],
                "timeoutSeconds": RSV_REMOVE_TIMEOUT_S,
            }
            routed = _routed_note(edt_workspace, port)
            if edt_workspace:
                arguments["edtWorkspace"] = edt_workspace
            result = await client.call_tool("edit_metadata", arguments)
            answer = result.content[0].text if result.content else ""
            try:
                payload = json.loads(answer)
            except ValueError:
                payload = None
            if isinstance(payload, dict) \
                    and str(payload.get("status", "")).lower() in ("pending", "inprogress"):
                return "pending", routed + [f"EDT начала удаление, оно ещё идёт в фоне "
                                            f"(клиент-серверная база может удаляться "
                                            f"минуты): {answer[:200]}"]
            ok = payload.get("success", True) if isinstance(payload, dict) else True
            if not ok:
                lines = routed + ["Вариант EDT не удался:", f"  {answer[:300]}",
                                  "— пробую конфигуратор."]
                holders = payload.get("infobaseHeldBy") if isinstance(payload, dict) else None
                if holders:
                    lines.append(f"  Базу держат сеансы: {_format_holders(holders)} — "
                                 "удалению нужна исключительная блокировка; завершите "
                                 "их или повторите позже.")
                return "next", lines
            if isinstance(payload, dict) and payload.get("alreadyAbsent"):
                return "done", routed + ["Расширения RSVData на базе не было "
                                         "(EDT: alreadyAbsent) — цель достигнута."]
            return "done", routed + ["Расширение удалено через EDT (MCP:RSV)."]
    except Exception as exc:  # noqa: BLE001 — report, never raise
        return "next", [f"Вариант EDT недоступен ({exc}) — пробую конфигуратор."]


def rsv_remove_branch(rsv_mcp, target):
    """(kind, lines) for the MCP:RSV removal attempt; worker-thread entry
    that runs its own event loop."""
    if not rsv_mcp:
        return "next", ["Вариант EDT пропущен: адрес MCP:RSV не задан (--rsvmcp)."]
    if not target.edt:
        return "next", ["Вариант EDT пропущен: у цели нет EDT-связи "
                        "{workspace, project} в targets.json."]
    return asyncio.run(_rsv_remove(rsv_mcp, target))


def _resolve_designer_ctx(target):
    """(ok, payload) for the configurator option, resolved UP FRONT (before
    /remove): ok=True carries (path, version, note, login, password,
    auth_note, srvr, ref, name); ok=False carries the reason. Platform:
    auto sources first, the per-target platform key last, the SINGLE
    installed platform as the final substitute (with a note), refusal
    names the ask-the-user step. The auth key is OPTIONAL (user decision,
    07.09): login=None + a note when none is stored, refusal when a
    present key is unreadable. Resolution touches only local files and
    the OS keyring — nothing here needs the service, but the record (and
    with it the stored credentials) goes away at the end of the chain, so
    everything is read from the in-memory target before the destructive
    step."""
    designer, version, note, refusal = _designer_for(target)
    if refusal:
        return False, refusal
    if designer and not os.path.isfile(designer):
        return False, f"файл не найден ({designer})."
    try:
        login, password, auth_note = update_designer.optional_login_password(target)
        srvr, ref, ibase_name = update_designer.resolve_connection(target)
    except (SystemExit, RuntimeError) as exc:
        return False, str(exc)
    return True, (designer, version, note, login, password, auth_note,
                  srvr, ref, ibase_name)


def designer_delete_branch(target, home, resolved):
    """(done, lines) for the Configurator CLI removal (resolved comes from
    _resolve_designer_ctx, pre-computed before the destructive step).
    Failures carry the /Out log tail AS IS (user decision, 07.09)."""
    ok, payload = resolved
    if not ok:
        return False, [f"Вариант конфигуратора недоступен: {payload}"]
    (path, version, note, login, password, auth_note,
     srvr, ref, ibase_name) = payload
    lines = [auth_note] if auth_note else []
    log_path = os.path.join(home, f".designer-{target.alias}-delete.log")
    try:
        update_designer.run_designer(
            update_designer.build_command(path, srvr, ref, login, password,
                                          log_path, None, "delete"),
            log_path, "удаление расширения")
    except update_designer.DesignerError as exc:
        lines.append(f"Удаление через конфигуратор не удалось: {exc}")
        lines.extend(exc.log_lines)
        if exc.returncode == 1:
            # The batch docs: /DeleteCfg -Extension returns 1 both for a real
            # failure and for "extension absent" — the goal state may already
            # hold; name the ambiguity instead of guessing.
            lines.append("Код возврата 1 по документации означает и отсутствие "
                         "расширения (цель фактически достигнута), и реальную "
                         "ошибку; хвост журнала — выше. Фактическое состояние "
                         "базы можно проверить, зарегистрировав цель заново "
                         "(register_target check=true) или спросив пользователя.")
        return False, lines
    except (SystemExit, subprocess.SubprocessError, OSError) as exc:
        lines.append(f"Удаление через конфигуратор не удалось: {exc}")
        return False, lines
    label = version or os.path.basename(path)
    tail = f" ({note})" if note else ""
    lines.append(f"Расширение удалено через конфигуратор "
                 f"(платформа {label}, база {ibase_name}){tail}.")
    return True, lines


def run_removal_chain(ctx, target, sink=None):
    """Full removal chain report for unregister_target(uninstall=True).

    /remove first (user decision), then EDT, then the configurator. Returns
    (removed, lines): removed=True — the extension is deleted or disabled,
    the caller deletes the target record; removed=False — the record stays
    (access to the base and the stored credentials survive for a retry) and
    the report names the reason and the next step. Reports, never raises.
    sink (a job, optional) receives every step's lines as the chain goes;
    the sink ends up holding the full report.
    """
    head = ["Удаление расширения RSVData (вариант конфигуратора может занять "
            "пару минут)…"]
    lines = list(head)
    _emit(sink, head)
    resolved = _resolve_designer_ctx(target)
    kind, step = selfremove(target)
    lines.extend(step)
    _emit(sink, step)
    if kind == "done":
        return True, lines
    kind, step = rsv_remove_branch(getattr(ctx, "rsv_mcp", ""), target)
    lines.extend(step)
    _emit(sink, step)
    if kind == "done":
        return True, lines
    if kind == "pending":
        step = ["Удаление выполняется в EDT в фоне. Запись цели СОХРАНЕНА; "
                "когда EDT закончит (list_targets(check=true): цель станет "
                "недоступной, когда расширение уйдёт), повторить "
                "unregister_target(base=..., uninstall=true): EDT дочитает "
                "результат (уже удалённое расширение вернёт success + "
                "alreadyAbsent), и запись уберётся."]
        lines.extend(step)
        _emit(sink, step)
        return False, lines
    done, step = designer_delete_branch(target, ctx.home, resolved)
    lines.extend(step)
    _emit(sink, step)
    if done:
        return True, lines
    step = ["Удаление не выполнено ни одним из способов. После устранения "
            "замечаний выше повторить: unregister_target(base=..., "
            "uninstall=true); если расширение удалено вручную — достаточно "
            "простого unregister_target(base=...)."]
    lines.extend(step)
    _emit(sink, step)
    return False, lines
