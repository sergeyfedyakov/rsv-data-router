"""FastMCP tool registration for rsv-data-router.

The router publishes its own tool list (with zero registered targets there
is no upstream to mirror, and clients cannot re-fetch tools/list on the fly):
upstream wrappers are typed locally, each one gets the router parameters
base/cwd. Service tools manage the targets.json map.

Credentials never travel through the chat: register_target creates entries
with an empty basic template and the response explains how to paste the
Basic key by hand (targets.json hot-reloads on change; JSON has no comments,
so "no authorization" means deleting the auth section or leaving value empty).

Upstream parameter schemas mirror RSVData_Сервер (the 1C extension source);
when RSVData gains a new tool, add a wrapper here.
"""

import asyncio
import os
import re
import threading
import time
from typing import Annotated, Any

from . import edt, forward, ibsrv, installer, jobs, router_help
from pydantic import Field
from .rlog import RouterLog
from .targets import TargetError, TargetsStore, base_root

CHECK_TIMEOUT_S = 20
# Async data jobs (async_mode=true): the agent polls job_status instead of
# holding the call, so the upstream wait can be generous.
ASYNC_TIMEOUT_S = 1800

BASIC_HELP = """Аутентификация — ключ хранится локально, в keyring ОС
(Windows Credential Manager / Secret Service в Linux), и никогда не проходит
через чат. Команды пакета (спросат логин и пароль; перезапуск роутера не нужен):
  rsv-data-router set-secret {alias}      — задать/заменить ключ
  rsv-data-router remove-secret {alias}   — убрать ключ

Запасной вариант без keyring (headless-сервер, CI) — секция auth в {path}:
  "auth": {{ "type": "basic", "value": "<base64 «логин:пароль» или готовая строка Basic ...>" }}
  (base64: [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("логин:пароль")))
Инлайн-значение приоритетнее keyring-записи; пустое value — читать keyring.
Перезапуск роутера не нужен: и keyring, и файл читаются при каждом вызове.

База без авторизации — удалите секцию "auth" (комментариев в JSON нет),
не задавайте keyring-запись: запросы пойдут без Authorization.

Windows-аутентификация базы (0.14.0) — auth_mode="windows" в register_target
(или "auth": {{ "type": "windows" }} в {path}, только для серверных баз):
запросы и конфигуратор идут от учётной записи ОС процесса роутера
(Negotiate/NTLM, конфигуратор с /WA+), ключ set-secret не нужен. Требуется
пакет pip install rsv-data-router[windows-auth] и настройка сервера
(публикация с Windows-аутентификацией, пользователь ИБ с «Аутентификация ОС»,
веб-сервер и сервер 1С на одной машине)."""


def async_job_answer(job):
    """The async_mode answer: the call runs in the background, the agent
    polls job_status for the result (same pattern as the install chains)."""
    return (f"Выполнение запущено ФОНОВЫМ ЗАДАНИЕМ (долгие вызовы не упираются "
            f"в таймаут): id задания {job.id}.\nСтатус и результат: "
            f"job_status(job_id=\"{job.id}\") — опрашивайте через 10–20 с; "
            "этот вызов можно считать завершённым.")


def _normalize_connection_arg(connection):
    """Normalized Srvr/Ref form of the call's connection argument.

    The only shape the record stores (no credentials); a malformed string
    is a caller error with the same wording as the address parsing.
    """
    try:
        return edt.normalize_connection(connection)
    except edt.EdtError as exc:
        raise RuntimeError(f"Строка подключения не годится: {exc}") from exc


def str_arg(description):
    return Annotated[str | None, Field(description=description)]


def int_arg(description):
    return Annotated[int | None, Field(description=description)]


def bool_arg(description):
    return Annotated[bool | None, Field(description=description)]


def str_list_arg(description):
    return Annotated[list[str] | None, Field(description=description)]


def dict_arg(description):
    return Annotated[dict[str, Any] | None, Field(description=description)]


def compact(**kwargs):
    """Drop None values: upstream tools read only the keys they are given."""
    return {key: value for key, value in kwargs.items() if value is not None}


class RouterContext:
    """Shared state for tool handlers: target map, call log, forward settings.

    No-base resolution order: cwd -> EDT workspace/project/infobase -> target
    linked by edt or url (cached per cwd until targets.json changes); then a
    single registered target; otherwise an instructive error. There is no
    "default" flag in the map: with several EDT sessions each has its own
    base, and the route follows the agent's cwd.
    """

    def __init__(self, home_dir, timeout_s=forward.DEFAULT_TIMEOUT_S, log=None,
                 log_dir=None):
        self.home = home_dir
        self.store = TargetsStore(home_dir)
        self.log = log or RouterLog(log_dir or home_dir)
        self.timeout_s = timeout_s
        self._edt_cache = {}  # cwd -> (store_version, alias | None, resolution)
        self.jobs = jobs.JobRegistry()
        self.ibsrv = ibsrv.IbsrvManager(home_dir, self.log)

    def ensure_file_server(self, target):
        """Start the file target's local ibsrv if needed; returns the target.

        Routing to a file IB goes through the ibsrv publication, so a data
        call must have the server up. The start is lazy (first call) and the
        instance then stays up — there is no idle auto-stop. A port taken
        since registration is reallocated and persisted in the record.
        """
        if not target.is_file:
            return target
        try:
            state = self.ibsrv.start(target)
        except ibsrv.IbsrvError:
            # stored port taken meanwhile: reallocate once, then retry (a
            # real failure — no binary, broken base — surfaces from retry)
            target = self.store.set_ib_port(target.alias, ibsrv._free_port())
            state = self.ibsrv.start(target)
        if state.get("port") and state["port"] != target.ib_port:
            target = self.store.set_ib_port(target.alias, state["port"])
        return target

    def start_job(self, alias, kind, factory):
        """Launch a background chain job; returns the Job.

        factory(job) is an async callable that runs the whole chain —
        blocking parts via asyncio.to_thread with the job as the report
        sink — plus any post-processing (e.g. the target-record fate after
        a removal). The registry marks the job done/error; the event loop
        keeps serving other requests meanwhile.
        """
        job = self.jobs.start(alias, kind)
        self.log.write(f"job {job.id} -> {alias}: {kind} запущено")

        async def _run():
            try:
                await factory(job)
                self.jobs.finish(job, jobs.STATUS_DONE)
                self.log.write(f"job {job.id} -> {alias}: {kind} готово")
            except Exception as exc:  # noqa: BLE001 — the job must not die silently
                job.extend([f"Задание прервано ошибкой: {exc}"])
                self.jobs.finish(job, jobs.STATUS_ERROR)
                self.log.write(f"job {job.id} -> {alias}: {kind} ОШИБКА: {exc}")

        asyncio.create_task(_run())
        return job

    def start_data_job(self, name, arguments, base=None, cwd=None):
        """Run an upstream data call as a background job; returns the Job.

        The base resolves FIRST — a bad base refuses right here, no doomed
        job (same fail-fast as the installer preflight). Parallel execute
        jobs on one target are allowed (independent upstream calls tolerate
        each other); an install/uninstall chain still refuses while any job
        runs on the target (JobRegistry.running). The FULL upstream answer
        lands in the job report — job_status returns it when done; a failed
        call appends the classified error block and marks the job errored.
        """
        alias = self.resolve_target(base, cwd).alias
        job = self.jobs.start(alias, jobs.KIND_EXECUTE)
        self.log.write(f"job {job.id} -> {alias}: {jobs.KIND_EXECUTE} запущено")

        def _run():
            try:
                answer = self.route_call(name, arguments, base=base, cwd=cwd,
                                         timeout_s=ASYNC_TIMEOUT_S)
                job.append(answer)
                self.jobs.finish(job, jobs.STATUS_DONE)
                self.log.write(f"job {job.id} -> {alias}: {jobs.KIND_EXECUTE} готово")
            except Exception as exc:  # noqa: BLE001 — the job must not hang running
                # route_call already wrapped transport errors into the
                # classified block (AGENT_NOTE inside) — store it verbatim.
                job.append(str(exc))
                self.jobs.finish(job, jobs.STATUS_ERROR)
                self.log.write(f"job {job.id} -> {alias}: {jobs.KIND_EXECUTE} "
                               f"ОШИБКА: {str(exc)[:160]}")

        threading.Thread(target=_run, daemon=True, name=f"job-{job.id}").start()
        return job

    def _target_by_edt(self, resolution):
        """Match a resolved cwd against the map: edt link, file, then url."""
        for target in self.store.snapshot():
            if target.edt and resolution.get("workspace") == target.edt.get("workspace") \
                    and resolution.get("project") == target.edt.get("project"):
                return target
        if resolution.get("file"):
            wanted = os.path.normcase(os.path.abspath(resolution["file"]))
            for target in self.store.snapshot():
                if target.is_file and target.file_path \
                        and os.path.normcase(os.path.abspath(target.file_path)) == wanted:
                    return target
            return None
        resolved_url = base_root(resolution.get("url") or "")
        for target in self.store.snapshot():
            if target.url == resolved_url:
                return target
        return None

    def _resolve_by_cwd(self, cwd):
        """Cached cwd -> (Target | None, resolution | None); negative results
        cached too, the resolution dict survives cache hits (register_target
        needs it after plain data calls have warmed the cache)."""
        if not cwd:
            return None, None
        version = self.store.version()
        cached = self._edt_cache.get(cwd)
        if cached and cached[0] == version:
            return self.store.get(cached[1]) if cached[1] else None, cached[2]
        try:
            resolution = edt.resolve(cwd)
        except edt.EdtError as exc:
            raise RuntimeError(f"Не удалось определить базу из EDT ({cwd}): {exc}. "
                               "Укажите base или url/строку подключения при регистрации.") from exc
        target = self._target_by_edt(resolution) if resolution else None
        self._edt_cache[cwd] = (version, target.alias if target else None, resolution)
        return target, resolution

    def resolve_target(self, base=None, cwd=None):
        """base alias wins; without base: cwd/EDT route, then the single target."""
        if base:
            target = self.store.get(base)
            if target is None:
                available = ", ".join(self.store.aliases()) or "—"
                raise RuntimeError(
                    f"База «{base}» не зарегистрирована. Доступные: {available}. "
                    "Список — list_targets, регистрация — register_target, "
                    "справка роутера — help_router.")
            return target
        target, _ = self._resolve_by_cwd(cwd)
        if target is not None:
            return target
        targets = self.store.snapshot()
        if len(targets) == 1:
            return targets[0]
        if not targets:
            raise RuntimeError(
                "Нет зарегистрированных целей. Зарегистрируйте базу: register_target "
                '(url="http://host/base" | connection="Srvr=...;Ref=..." '
                '| без параметров из воркспейса EDT + cwd). Справка роутера — help_router.')
        available = ", ".join(t.alias for t in targets)
        raise RuntimeError(
            f"Base не указан, cwd не связан с зарегистрированной базой EDT. "
            f"Доступные: {available}. Укажите base или работайте из каталога проекта EDT. "
            "Справка роутера — help_router.")

    def route_call(self, name, arguments, base=None, cwd=None, timeout_s=None):
        """Forward a tool call to the resolved target; log one line per call.

        timeout_s=None keeps the context default (sync tools); async data
        jobs pass ASYNC_TIMEOUT_S — the agent polls job_status meanwhile.
        """
        timeout_s = self.timeout_s if timeout_s is None else timeout_s
        started = time.monotonic()
        try:
            target = self.resolve_target(base, cwd)
            target = self.ensure_file_server(target)
        except ibsrv.IbsrvError as exc:
            self.log.write(f"{name} -> ?: сервер файловой базы не поднят: {exc}")
            raise RuntimeError(str(exc)) from exc
        except RuntimeError as exc:
            self.log.write(f"{name} -> ?: ошибка: {str(exc)[:160]}")
            raise
        try:
            answer = forward.call_tool(target.mcp_url, name, arguments,
                                       target.auth_headers(), timeout_s,
                                       wa=target.auth_is_windows)
        except forward.TransportError as exc:
            elapsed = (time.monotonic() - started) * 1000
            self.log.write(f"{name} -> {target.alias}: ошибка: "
                           f"{forward.explain_short(exc)} ({elapsed:.0f} мс)")
            # One shared classification block (401/404/409/5xx/network) with
            # the "don't investigate, show the user" instruction inside.
            raise RuntimeError(forward.explain_failure(
                exc, wa=target.auth_is_windows)) from exc
        except RuntimeError as exc:
            elapsed = (time.monotonic() - started) * 1000
            self.log.write(f"{name} -> {target.alias}: ошибка: {str(exc)[:160]} ({elapsed:.0f} мс)")
            raise
        elapsed = (time.monotonic() - started) * 1000
        self.log.write(f"{name} -> {target.alias}: ок ({elapsed:.0f} мс)")
        return answer


def rsvdata_report(ctx):
    """Text for get_rsvdata: local path, HTTP download link, adoption recipe.

    The URL is shown with the router's own host:port as a placeholder — the
    agent already knows that address from its MCP config, and the server
    cannot reliably know its external name in --network mode.
    """
    path = getattr(ctx, "rsvdata_path", None)
    lines = ["Бинарник RSVData (расширение 1С с HTTP-сервисом, .cfe):"]
    if not path:
        lines.append("  роутеру не выдан — перезапустите его с параметром")
        lines.append("  --rsvdatabinary <путь к .cfe> и повторите get_rsvdata.")
        return "\n".join(lines)
    if os.path.isfile(path):
        size = os.path.getsize(path)
        mtime = time.strftime("%d.%m.%Y %H:%M", time.localtime(os.path.getmtime(path)))
        lines.append(f"  путь: {path} ({size} байт, от {mtime})")
    else:
        lines.append(f"  путь: {path} — ФАЙЛА НЕТ, проверьте параметр --rsvdatabinary.")
    if getattr(ctx, "serve_binary", True):
        lines += [
            "Скачивание по HTTP (тот же host:port, что у роутера; /mcp заменяется на "
            "/bin/rsvdata.cfe):",
            "  curl -o rsvdata.cfe http://<host-роутера>:<порт>/bin/rsvdata.cfe",
            '  (в режиме --network добавьте заголовок: -H "X-Router-Token: <секрет>")',
        ]
    else:
        lines.append("  HTTP-раздача недоступна (транспорт stdio) — копируйте файл по пути.")
    lines += [
        "",
        "Внедрение в проект (через MCP rsv):",
        "  1) edit_metadata operation=importProject filePath=<скачанный .cfe> — "
        "создаст проект-расширение;",
        "  2) sync_database — опубликовать конфигурацию с расширением в базе;",
        "  3) публикация HTTP-сервиса на веб-сервере (vrd) — админская часть, вне MCP.",
        "409 от базы — про версию ПЛАТФОРМЫ (модуль расширения веб-сервера vs сервер 1С):",
        "этот бинарник её не меняет.",
    ]
    return "\n".join(lines)


def _sanitize_alias(name):
    """Alias-safe form of a directory name (letters/digits/._-, max 64)."""
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", str(name or "")).strip("_")[:64]


def _file_target_alias(base, file_dir, existing):
    """Alias + port for a file target record (existing record wins)."""
    if existing and existing.is_file:
        return existing.alias, existing.ib_port
    if not base:
        base = _sanitize_alias(os.path.basename(os.path.normpath(file_dir)))
    if not base:
        raise RuntimeError("Укажите base — алиас файловой базы "
                           "(из имени каталога он не выводится).")
    return base, None


def _file_target_record(ctx, base, file_dir, existing, comment, edt_link, platform):
    """Validate + persist a file target; returns (target, base, updated)."""
    if not os.path.isdir(file_dir):
        raise RuntimeError(f"Файловая база не найдена: {file_dir} (нужен каталог базы).")
    base, port = _file_target_alias(base, file_dir, existing)
    target, updated = ctx.store.register(
        base, None, comment=comment, edt=edt_link, platform=platform,
        connection=edt.normalize_connection({"File": file_dir}),
        kind="file", file_path=file_dir, ib_port=port or ibsrv._free_port())
    ctx.log.write(f"register_target -> {target.alias}: "
                  f"{'обновлена' if updated else 'зарегистрирована'} файловая база {file_dir}")
    return target, base, updated


async def _file_target_check(ctx, target):
    """Availability lines for a file target (starts ibsrv, then pings)."""
    try:
        target = await asyncio.to_thread(ctx.ensure_file_server, target)
    except ibsrv.IbsrvError as exc:
        return [f"Проверка связи пропущена: сервер ibsrv не поднят — {exc}"]
    return list(await asyncio.to_thread(installer.availability_check, target))


def _file_target_report(target, base, file_dir, source, updated):
    """Human report lines for a just-registered file target."""
    lines = [f"Файловая цель «{base}» {'обновлена' if updated else 'зарегистрирована'}: "
             f"{file_dir} (адрес из {source}); локальная публикация — {target.url}."]
    if target.edt:
        lines.append(f"EDT-связь: {target.edt['project']} ({target.edt['workspace']}).")
    if target.platform:
        lines.append(f"Платформа: {target.platform}.")
    lines.append("Файловую базу публикует локальный сервер ibsrv: он поднимается лениво "
                 "(первое обращение или start_server), остановка — stop_server "
                 "или при завершении роутера.")
    return lines


async def _file_install_step(ctx, target, install, check):
    """Launch the install job for a file target: EDT first, ibcmd fallback."""
    if not install:
        return []
    if not check:
        return ["Установка расширения пропущена: check=False — ключ install "
                "игнорируется."]
    clash = ctx.jobs.running(target.alias)
    if clash:
        return [f"Установка НЕ запущена: для цели «{target.alias}» уже идёт "
                f"задание {clash.id} ({clash.kind}, старт {clash.started}) — "
                f"дождитесь его результата job_status(job_id=\"{clash.id}\")."]
    go, refusal = await asyncio.to_thread(
        installer.file_install_preflight, target,
        getattr(ctx, "rsvdata_path", None), getattr(ctx, "rsv_mcp", ""))
    if not go:
        return refusal
    job = ctx.start_job(target.alias, jobs.KIND_INSTALL,
                        lambda j: asyncio.to_thread(
                            installer.run_file_install_chain, ctx, target, j))
    return [f"Установка расширения запущена ФОНОВЫМ ЗАДАНИЕМ (EDT или ibcmd): "
            f"id задания {job.id}.",
            f"Статус и построчный отчёт: job_status(job_id=\"{job.id}\") — "
            "опрашивайте через 20–30 с; этот вызов можно считать завершённым."]


async def _register_file_target(ctx, *, base, file_dir, existing, comment, edt_link,
                                platform, check, install, source):
    """Register or update a file-IB target and optionally check it."""
    target, base, updated = _file_target_record(
        ctx, base, file_dir, existing, comment, edt_link, platform)
    lines = _file_target_report(target, base, file_dir, source, updated)
    if check:
        lines.extend(await _file_target_check(ctx, target))
    else:
        lines.append("Проверка связи пропущена (check=False).")
    lines.extend(await _file_install_step(ctx, target, install, check))
    lines.append("")
    lines.append(BASIC_HELP.format(path=ctx.store.path, alias=base))
    return "\n".join(lines)


def _pick_file_target(ctx, base):
    """Resolve base (or the single file target) to a file-IB Target."""
    if base:
        target = ctx.store.get(base)
        if target is None:
            available = ", ".join(ctx.store.aliases()) or "—"
            raise RuntimeError(f"База «{base}» не зарегистрирована. Доступные: {available}.")
    else:
        files = [item for item in ctx.store.snapshot() if item.is_file]
        if not files:
            raise RuntimeError("Файловых целей нет — зарегистрируйте файловую базу: "
                               'register_target(connection=\'File="<каталог>"\') '
                               "или из проекта EDT (register_target с cwd).")
        if len(files) > 1:
            names = ", ".join(item.alias for item in files)
            raise RuntimeError(f"Несколько файловых целей ({names}) — укажите base.")
        target = files[0]
    if not target.is_file:
        raise RuntimeError(f"Цель «{target.alias}» — серверная база: локальный "
                           "сервер ibsrv ей не нужен.")
    return target


async def _unregister_file_target(ctx, target, base, had_key):
    """Removal job for a file target: EDT (or ibcmd) delete, then drop the record."""
    go, refusal = await asyncio.to_thread(
        installer.file_removal_preflight, target, getattr(ctx, "rsv_mcp", ""))
    if not go:
        ctx.log.write(f"unregister_target -> {base}: удаление не запущено "
                      "(предпроверка ibcmd)")
        return "\n".join(refusal)

    def factory(job):
        async def _run():
            removed, _lines = await asyncio.to_thread(
                installer.run_file_removal_chain, ctx, target, job)
            if removed:
                try:
                    ctx.store.unregister(base)
                except TargetError:
                    pass  # unregistered concurrently — the goal state holds
                job.append(f"Запись цели «{base}» удалена из мапы роутера.")
                if had_key:
                    job.append("Ключ цели удалён из keyring ОС.")
                ctx.log.write(f"job -> {base}: расширение удалено (ibcmd), "
                              "запись удалена")
            else:
                job.append(f"Запись цели «{base}» СОХРАНЕНА (доступ к базе и "
                           "креды — для повторной попытки).")
                ctx.log.write(f"job -> {base}: расширение НЕ удалено, "
                              "запись сохранена")
        return _run()

    job = ctx.start_job(base, jobs.KIND_REMOVAL, factory)
    return (f"Удаление расширения с файловой базы «{base}» запущено ФОНОВЫМ "
            f"ЗАДАНИЕМ (EDT или ibcmd): id задания {job.id}.\n"
            f"Статус и построчный отчёт: job_status(job_id=\"{job.id}\") — "
            "опрашивайте через 20–30 с; по завершении запись цели будет удалена "
            "автоматически вместе с ключом из keyring, либо сохранена с причиной.")


def register_tools(app, ctx):
    # ---- data tools: typed wrappers forwarded to the selected target ----

    @app.tool(name="ping")
    def ping(base: str | None = None, cwd: str | None = None) -> str:
        """Проверка связи с базой через роутер.

        base — алиас цели из list_targets (без него — база из EDT по cwd (проект воркспейса), иначе единственная зарегистрированная цель);
        cwd — рабочий каталог агента: без base база ищется в воркспейсе EDT, где лежит этот каталог.
        """
        return ctx.route_call("ping", {}, base=base, cwd=cwd)

    @app.tool(name="config")
    def config(base: str | None = None, cwd: str | None = None) -> str:
        """Паспорт базы: какая это конфигурация (имя, версия, поставщик, режим
        совместимости), подключённые расширения и бизнес-разделы (подсистемы)
        верхнего уровня. Полезно вызвать первым — понять, с какой базой работаешь.

        base — алиас цели (без него — база из EDT по cwd (проект воркспейса), иначе единственная зарегистрированная цель).
        """
        return ctx.route_call("config", {}, base=base, cwd=cwd)

    @app.tool(name="describe")
    def describe(
        type: str_arg("Категория объектов (Справочник, Документ, РегистрСведений, "
                      "РегистрНакопления, ...). Без других параметров вернёт объекты "
                      "этой категории.") = None,
        filter: str_arg("Только с type: подстрока в имени или синониме (без учёта "
                        "регистра) — сузить список одной категории.") = None,
        find: str_arg("Глобальный поиск: подстрока в имени/синониме сразу по ВСЕМ "
                      "категориям (без type). Вернёт объекты с их типами.") = None,
        subsystem: str_arg("Имя бизнес-раздела (подсистемы): Продажи или путь "
                           "Продажи.ОптовыеПродажи. Вернёт объекты раздела и вложенные "
                           "подсистемы. Список разделов — в config.") = None,
        object: str_arg("Полное имя объекта (Справочник.Номенклатура). Вернёт его "
                        "таблицы. Полную структуру сразу — get_structure.") = None,
        table: str_arg("Полное имя таблицы (Справочник.Номенклатура или "
                       "Документ.РеализацияТоваровУслуг.Товары). Вернёт поля с типами.") = None,
        base: str | None = None,
        cwd: str | None = None,
    ) -> str:
        """Поиск и обзор состава конфигурации, когда точные имена ещё не известны:
        категории объектов, поиск объекта по подстроке (filter или глобально find),
        объекты бизнес-раздела (subsystem), таблицы объекта, поля таблицы.
        Полную структуру одного объекта сразу даёт get_structure.
        Подробности и примеры: help topic=describe. base — алиас цели (без него — база из EDT по cwd (проект воркспейса), иначе единственная зарегистрированная цель).
        """
        return ctx.route_call("describe", compact(type=type, filter=filter, find=find,
                                                  subsystem=subsystem, object=object,
                                                  table=table),
                              base=base, cwd=cwd)

    @app.tool(name="get_structure")
    def get_structure(
        object: str_arg("Полное имя объекта: Справочник.Контрагенты, "
                        "Документ.РеализацияТоваровУслуг, РегистрНакопления.ТоварыНаСкладах. "
                        "Обязательный."),
        base: str | None = None,
        cwd: str | None = None,
    ) -> str:
        """Полная структура ОДНОГО объекта по известному имени за один вызов: все
        таблицы (основная, табличные части, виртуальные таблицы регистров), поля
        с типами, значения перечислений, владельцы — основа для проектирования
        запроса. Найти имя объекта или осмотреть базу — describe.
        Подробности и примеры: help topic=get_structure. base — алиас цели
        (без него — база из EDT по cwd (проект воркспейса), иначе единственная зарегистрированная цель).
        """
        return ctx.route_call("get_structure", {"object": object}, base=base, cwd=cwd)

    @app.tool(name="query")
    def query(
        table: str_arg("Полное имя таблицы: основной (Справочник.Номенклатура), табличной "
                       "части (Документ.РеализацияТоваровУслуг.Товары) или виртуальной "
                       "таблицы регистра (РегистрНакопления.ТоварыНаСкладах.Остатки). "
                       "Обязательный."),
        fields: str_list_arg("Какие поля вернуть. Если не указано — все поля таблицы. "
                             "Точные имена полей даёт get_structure (или describe table=...).") = None,
        filters: Annotated[list[dict[str, Any]] | None, Field(
            description="Отборы данных (соединяются по И). Элемент: "
                        "{field, comparison, value}; comparison: equal, notEqual, greater, "
                        "greaterOrEqual, less, lessOrEqual, contains, notContains, inList, "
                        "notInList, filled, notFilled (по умолчанию equal); value — значение "
                        "сравнения, для inList/notInList — массив значений.")] = None,
        order: str_list_arg("Сортировка: \"Поле\" — по возрастанию, \"-Поле\" — по убыванию.") = None,
        period: str_arg("Момент для виртуальных таблиц регистров (Остатки/СрезПоследних/"
                        "СрезПервых) в формате ГГГГ-ММ-ДД. Без него — текущие остатки.") = None,
        limit: int_arg("Максимум строк (по умолчанию 100, максимум 1000).") = None,
        offset: int_arg("Сколько строк пропустить (постранично). По умолчанию 0.") = None,
        base: str | None = None,
        cwd: str | None = None,
    ) -> str:
        """Чтение данных ОДНОЙ таблицы по описанию (поля, отборы, сортировка,
        период), без языка запросов. Группировки и соединения не поддерживаются —
        для них execute_query.
        Подробности и примеры: help topic=query. base — алиас цели (без него — база из EDT по cwd (проект воркспейса), иначе единственная зарегистрированная цель).
        """
        return ctx.route_call("query", compact(table=table, fields=fields, filters=filters,
                                               order=order, period=period, limit=limit,
                                               offset=offset),
                              base=base, cwd=cwd)

    @app.tool(name="execute_query")
    def execute_query(
        query: str_arg("Текст запроса на языке запросов 1С (только чтение). Пример: "
                       "ВЫБРАТЬ ПЕРВЫЕ 10 Наименование ИЗ Справочник.Номенклатура. "
                       "Обязательный."),
        parameters: dict_arg("Параметры запроса: имя -> значение (для &Имя в тексте). "
                             "Даты — строкой ГГГГ-ММ-ДД. Для списков (В (...)) — массив "
                             "значений.") = None,
        limit: int_arg("Максимум строк (по умолчанию 100, максимум 1000).") = None,
        base: str | None = None,
        cwd: str | None = None,
        async_mode: bool_arg("ФОНОВЫЙ режим для долгих запросов (таймаут 30 минут "
                             "вместо обычного): вызов уходит в задание, ответ — "
                             "в отчёт job_status(job_id=...). По умолчанию false.") = False,
    ) -> str:
        """Выполнение готового запроса на языке запросов 1С — мощный путь:
        группировки и итоги, соединения, разыменование через точку, вложенные
        запросы. Когда декларативного query мало.
        Подробности и примеры: help topic=execute_query. base — алиас цели
        (без него — база из EDT по cwd (проект воркспейса), иначе единственная зарегистрированная цель).
        """
        arguments = compact(query=query, parameters=parameters, limit=limit)
        if async_mode:
            job = ctx.start_data_job("execute_query", arguments, base=base, cwd=cwd)
            return async_job_answer(job)
        return ctx.route_call("execute_query", arguments, base=base, cwd=cwd)

    @app.tool(name="read_binary")
    def read_binary(
        query: str_arg("Запрос на языке запросов 1С (только чтение), возвращающий РОВНО "
                       "одну строку и одну колонку — значение типа ХранилищеЗначения "
                       "(например Настройки варианта отчёта, картинка реквизита). "
                       "Обязательный."),
        parameters: dict_arg("Параметры запроса: имя -> значение (для &Имя в тексте). "
                             "Даты — строкой ГГГГ-ММ-ДД.") = None,
        maxBytes: int_arg("Предел размера блоба в байтах (по умолчанию 102400, максимум "
                          "1048576). Блоб больше предела — отказ: частично двоичные "
                          "данные не отдаются.") = None,
        base: str | None = None,
        cwd: str | None = None,
    ) -> str:
        """Точечное чтение ОДНОГО двоичного значения (реквизит типа ХранилищеЗначения,
        например настройки варианта отчёта): возвращает сырой блоб в base64. Обычные
        query/execute_query такие поля отдают представлением, а не данными.
        Подробности и примеры: help topic=read_binary. base — алиас цели (без него —
        база из EDT по cwd (проект воркспейса), иначе единственная зарегистрированная цель).
        """
        return ctx.route_call("read_binary", compact(query=query, parameters=parameters,
                                                     maxBytes=maxBytes),
                              base=base, cwd=cwd)

    @app.tool(name="evaluate_bsl")
    def evaluate_bsl(
        code: str_arg("Выражение на встроенном языке 1С (Вычислить) в серверном контексте "
                      "базы. Доступна переменная Параметры — массив значений: "
                      "Параметры[0], Параметры[1], ... Обязательный."),
        parameters: Annotated[list[Any] | None, Field(
            description="Массив значений произвольного типа — вход выражения.")] = None,
        base: str | None = None,
        cwd: str | None = None,
        async_mode: bool_arg("ФОНОВЫЙ режим для долгих вычислений (таймаут 30 минут "
                             "вместо обычного): вызов уходит в задание, ответ — "
                             "в отчёт job_status(job_id=...). По умолчанию false.") = False,
    ) -> str:
        """Вычислить выражение на встроенном языке 1С (Вычислить) в серверном контексте
        базы: глобальные функции, метаданные, проверки прав. Вход — массив Параметры;
        ответ — значение выражения в JSON. Только при включённой константе «Включить
        небезопасные методы» (иначе — отказ «Небезопасные методы отключены»); результат
        НЕ проходит анонимизацию.
        Подробности и примеры: help topic=evaluate_bsl. base — алиас цели (без него —
        база из EDT по cwd (проект воркспейса), иначе единственная зарегистрированная цель).
        """
        arguments = compact(code=code, parameters=parameters)
        if async_mode:
            job = ctx.start_data_job("evaluate_bsl", arguments, base=base, cwd=cwd)
            return async_job_answer(job)
        return ctx.route_call("evaluate_bsl", arguments, base=base, cwd=cwd)

    @app.tool(name="execute_bsl")
    def execute_bsl(
        code: str_arg("Код на встроенном языке 1С (Выполнить) в серверном контексте базы. "
                      "Доступны переменные: Параметры — массив значений (вход); "
                      "РезультатВыполнения — присвойте значение в коде, чтобы вернуть его "
                      "в ответе (иначе ответ — «Выполнено»). Обязательный."),
        parameters: Annotated[list[Any] | None, Field(
            description="Массив значений произвольного типа — вход кода.")] = None,
        base: str | None = None,
        cwd: str | None = None,
        async_mode: bool_arg("ФОНОВЫЙ режим для долгого кода (таймаут 30 минут вместо "
                             "обычного): вызов уходит в задание, ответ — в отчёт "
                             "job_status(job_id=...). По умолчанию false.") = False,
    ) -> str:
        """Выполнить код на встроенном языке 1С (Выполнить) в серверном контексте базы.
        Вход — массив Параметры; чтобы вернуть результат, присвойте его переменной
        РезультатВыполнения в коде (без него ответ — «Выполнено»). Только при включённой
        константе «Включить небезопасные методы» (иначе — отказ «Небезопасные методы
        отключены»); НЕ проходит анонимизацию.
        Подробности и примеры: help topic=execute_bsl. base — алиас цели (без него —
        база из EDT по cwd (проект воркспейса), иначе единственная зарегистрированная цель).
        """
        arguments = compact(code=code, parameters=parameters)
        if async_mode:
            job = ctx.start_data_job("execute_bsl", arguments, base=base, cwd=cwd)
            return async_job_answer(job)
        return ctx.route_call("execute_bsl", arguments, base=base, cwd=cwd)

    @app.tool(name="eventlog")
    def eventlog(
        from_: Annotated[str | None, Field(
            alias="from",
            description="Начало периода: ГГГГ-ММ-ДД или ГГГГ-ММ-ДДTчч:мм:сс.")] = None,
        to: str_arg("Конец периода (включительно): ГГГГ-ММ-ДД (весь день) или "
                    "ГГГГ-ММ-ДДTчч:мм:сс.") = None,
        levels: str_list_arg("Уровни: Ошибка/Error, Предупреждение/Warning, "
                             "Информация/Information, Примечание/Note.") = None,
        applications: str_list_arg("Точные идентификаторы приложений (1CV8, BackgroundJob, "
                                   "COMConnection, WebServer...). Допустимые значения: "
                                   "values=true, valueColumns=[application].") = None,
        users: str_list_arg("Имена пользователей информационной базы (точно); фоновые "
                            "задания пишут от имени пользователя, назначенного "
                            "регламентному заданию.") = None,
        computers: str_list_arg("Имена компьютеров (точно).") = None,
        events: str_list_arg("ТОЧНЫЕ системные имена событий: _$PerformError$_ (Ошибка "
                             "выполнения), _$Data$_.Update, _$Session$_.Start и т.п. Список "
                             "допустимых: values=true, valueColumns=[event], "
                             "filter=<подстрока>.") = None,
        metadata: str_arg("FQN объекта метаданных (Документ.ЗаказКлиента) — точный отбор "
                          "по объекту.") = None,
        session: int_arg("Номер сеанса.") = None,
        transaction: str_arg("Идентификатор транзакции.") = None,
        comment: str_arg("ТОЧНОЕ равенство комментария (платформа ищет равенство, НЕ "
                         "подстроку). Для подстроки — commentContains.") = None,
        commentContains: str_arg("Поиск подстроки в комментарии без учёта регистра — "
                                 "серверная пост-фильтрация последних scan записей.") = None,
        scan: int_arg("Сколько последних записей сканировать при commentContains "
                      "(по умолчанию 5000, максимум 20000).") = None,
        dataPresentation: str_arg("Точное представление данных.") = None,
        columns: str_list_arg("Какие колонки вернуть (en/ru): date, level, user, userName, "
                              "computer, application, event, metadata, comment, session, "
                              "transaction и др. По умолчанию компактный набор без data.") = None,
        limit: int_arg("Максимум записей (последние в хронологическом порядке; по умолчанию "
                       "100, максимум 1000).") = None,
        values: bool_arg("Режим получения допустимых значений отборов вместо записей "
                         "журнала.") = None,
        valueColumns: str_list_arg("Для values: наборы application, event, user, computer, "
                                   "metadata, server. По умолчанию application и event.") = None,
        filter: str_arg("Для values: подстрока фильтрации значений (по значению и "
                        "представлению, без учёта регистра).") = None,
        base: str | None = None,
        cwd: str | None = None,
    ) -> str:
        """Журнал регистрации 1С: чтение записей с отборами (период, уровни,
        события, приложения, пользователи, сеансы) и получение допустимых
        значений отборов (values=true). Для событий отбор — ТОЧНОЕ системное имя
        (_$PerformError$_); их список берётся через values, подстрока filter сужает
        на сервере. Подстрока в комментарии — commentContains (поле comment —
        точное равенство).
        Подробности и примеры: help topic=eventlog. base — алиас цели (без него — база из EDT по cwd (проект воркспейса), иначе единственная зарегистрированная цель).
        """
        arguments = compact(**{"from": from_, "to": to, "levels": levels,
                               "applications": applications, "users": users,
                               "computers": computers, "events": events,
                               "metadata": metadata, "session": session,
                               "transaction": transaction, "comment": comment,
                               "commentContains": commentContains, "scan": scan,
                               "dataPresentation": dataPresentation, "columns": columns,
                               "limit": limit, "values": values,
                               "valueColumns": valueColumns, "filter": filter})
        return ctx.route_call("eventlog", arguments, base=base, cwd=cwd)

    @app.tool(name="reveal")
    def reveal(
        text: str_arg("Текст с токенами анонимизации (например [ОРГ-00001]) — обычно "
                      "финальный ответ для пользователя; токены заменяются реальными "
                      "значениями. Обязательный."),
        base: str | None = None,
        cwd: str | None = None,
    ) -> str:
        """Расшифровка анонимизации: заменяет в тексте токены вида [ОРГ-00001]
        реальными значениями. Вызови на финальном ответе пользователю — модель
        работает с обезличенными данными, а человек видит реальные ФИО/контрагентов.
        Подробности и примеры: help topic=reveal. base — алиас цели (без него — база из EDT по cwd (проект воркспейса), иначе единственная зарегистрированная цель).
        """
        return ctx.route_call("reveal", {"text": text}, base=base, cwd=cwd)

    @app.tool(name="help")
    def help_tool(
        topic: str_arg("Тема справки: имя инструмента (query, describe) или обзорная "
                       "тема (workflow, about). Без topic — обзор самой базы.") = None,
        base: str | None = None,
        cwd: str | None = None,
    ) -> str:
        """Справка RSVData из выбранной базы: topic=имя инструмента или тема
        (workflow, about); без topic — обзор базы. Справка самого роутера —
        отдельный инструмент help_router.
        """
        return ctx.route_call("help", compact(topic=topic), base=base, cwd=cwd)

    @app.tool(name="help_router")
    def help_router() -> str:
        """Справка самого роутера: выбор базы (base/cwd), управление целями,
        аутентификация, бинарник RSVData. Справка по данным — help (уходит в
        выбранную базу).
        """
        return router_help.build(ctx)

    @app.tool(name="get_rsvdata")
    def get_rsvdata() -> str:
        """Бинарник расширения RSVData (.cfe): путь и HTTP-адрес для скачивания
        (GET /bin/rsvdata.cfe) + рецепт внедрения в проект через rsv. Путь
        задаётся параметром запуска роутера --rsvdatabinary (инструментами не меняется).
        """
        return rsvdata_report(ctx)

    # ---- service tools: managed by the router itself ----

    @app.tool(name="register_target")
    async def register_target(url: str | None = None, connection: str | None = None,
                              base: str | None = None, comment: str | None = None,
                              cwd: str | None = None, check: bool = True,
                              install: bool = False,
                              platform: str | None = None,
                              auth_mode: str | None = None) -> str:
        """Регистрация цели — базы 1С с опубликованным сервисом RSVData.

        Рекомендуемый порядок: если запущена EDT (доступен MCP:RSV) —
        регистрируйте С УКАЗАНИЕМ cwd (корень воркспейса EDT или любой его
        подкаталог): cwd привязывает цель к проекту EDT, и install=true
        сможет установить расширение через EDT — без публикации и без
        данных авторизации. Без cwd регистрируйте, только когда воркспейса
        нет.

        Повторная регистрация существующей цели: имя (base) берётся из
        записи, а также сохранённые url и EDT-связь — аргумент url вызова
        игнорируется (это защищает от опечатки или устаревшего адреса);
        connection вызова адрес не меняет — только пополняет/обновляет
        сохранённую строку подключения; cwd из вызова используется, только
        если у записи ещё нет EDT-связи. Используйте это, чтобы
        переустановить расширение (install=true) или проверить связь
        (check=true): достаточно register_target(base="алиас"). Новый url
        применяется только к новой цели.

        Способы задания адреса (в порядке приоритета):
        url — адрес публикации базы, http://host/base; пути сервиса роутер
          достраивает сам, полный адрес с /hs/rsvdata/mcp тоже принимается
          (хранится корень http://host/base). ВНИМАНИЕ: регистрация только
          по url оставляет запись без строки подключения — установка
          расширения на такую базу недоступна (база пригодна для работы,
          установку выполняет администратор; удаление — только через точку
          /remove). Если планируете install=true, указывайте connection,
          cwd или точное название базы;
        connection — строка подключения 1С (Srvr="host:port";Ref="base";) —
          для серверной базы URL строится сам, порт кластера отбрасывается
          (в сохранённой строке порт сохраняется); строка хранится в записи
          (нормализованная Srvr/Ref, без учётных данных) и служит
          источником подключения для конфигуратор-варианта;
        без обоих — база определяется из воркспейса EDT по cwd (проект ->
          ассоциированная ИБ -> строка подключения -> URL); cwd может быть
          корнем воркспейса или любым его подкаталогом (в том числе
          проектом-расширением) — проект основной конфигурации роутер
          определит сам; если не вышло, будет ошибка с просьбой указать url
          или connection;
        и наконец, base может быть ТОЧНЫМ названием базы из списка баз
          (ibases.v8i — «считаем имя точное», без нормализации и нечёткого
          поиска): запись с таким именем даёт строку подключения (url
          строится из неё), алиасом становится Ref.
          cwd вместе с url/connection тоже работает: цель привязывается к
          проекту EDT, если проект из cwd указывает на эту же базу.
        base — короткий алиас; обязателен, если его нет — берётся Ref из
        строки подключения.
        comment — пояснение, что это за база;
        check=True — проверить связь ping-ом после регистрации; check=False —
          пропустить проверку. При неудавшейся проверке запись ВСЁ РАВНО
          сохраняется, а ответ назовёт причину и следующий шаг: публикации
          нет — выполнить публикацию; данных авторизации нет — добавить
          ключ (rsv-data-router set-secret); оба сообщения приходят сразу,
          когда не хватает и того и другого.
        install=True — при check=True дополнительно установить/обновить
          расширение RSVData (бинарник --rsvdatabinary) на этой базе, где
          его ещё нет или он старый. Порядок: точка /update -> при ошибке
          установка через EDT через MCP:RSV (нужна EDT-связи цели —
          регистрируйте с cwd) -> конфигуратор (нужны данные авторизации в
          targets.json). Публикация базы для установки НЕ нужна — она
          требуется только для проверки.
          Для ФАЙЛОВОЙ базы (kind=file) цепочка другая: расширение ставит
          ibcmd (рядом с 1cv8.exe платформы базы) в два шага — load .cfe и
          apply; локальный сервер ibsrv на время операции останавливается и
          поднимается заново; креды — из keyring, если база с пользователями
          (без них ibcmd на такой базе не работает).
          Перед запуском задания — предпроверка: если заранее известно,
          что ни один вариант не сработает (нет источника строки
          подключения — регистрация по url; или не определена версия
          платформы), ошибка с диагностикой возвращается СРАЗУ, фоновое
          задание не создаётся.
          Установка идёт ФОНОВЫМ ЗАДАНИЕМ: регистрация выполняется сразу,
          ответ несёт идентификатор задания; статус и отчёт —
          job_status(job_id="ид"). Повторный install=true по цели с идущим
          заданием отклоняется. Цепочка укладывается в один вызов, если
          базе не нужна авторизация, и в два иначе: после добавления ключа
          повторить проверку (ping или list_targets check=true),
          повторная установка не требуется.
          При check=False ключ install игнорируется. Вариант конфигуратора
          может занять пару минут.
        platform — фолбэк для конфигуратор-варианта: номер версии платформы
          базы (например 8.3.27.2214) ИЛИ полный путь к 1cv8.exe.
          Приоритет: EDT-связь цели и скан воркспейсов EDT на диске —
          всегда выше; заданная явная ВЕРСИЯ перекрывает ibases.v8i
          (решение 23.09), явный ПУТЬ побеждает всё. Хранится в записи,
          чтобы uninstall=true позже взял то же значение.
        auth_mode — тип авторизации цели: "basic" (по умолчанию: ключ в
          keyring ОС), "windows" — Windows-аутентификация (запросы и
          конфигуратор от учётной записи ОС процесса роутера, Negotiate/
          NTLM и /WA+; только для серверных баз; требуется
          pip install rsv-data-router[windows-auth] и настройка сервера),
          "none" — явно без авторизации. Без аргумента (None) тип сохраняется
          прежний; сам ключ (keyring) не трогается ни в каком случае.

        Логин/пароль через чат НЕ передаются: запись создаётся с пустым
        шаблоном basic, ключ задаётся командой пакета
        «rsv-data-router set-secret <алиас>» (keyring ОС) —
        инструкция будет в ответе.
        """
        existing = ctx.store.get(base) if base else None
        stored_connection = None
        connection_note = None
        file_dir = edt.file_path(connection) if connection else None
        if file_dir is None and existing is not None and existing.is_file:
            # Re-registration of a file target without repeating the
            # connection: keep it a file target (its directory IS the
            # address), do not fall into the server-url branch.
            file_dir = existing.file_path
        if existing:
            # Re-registration of a known alias: name, url and the EDT link
            # come from the STORED record (protects against a mistyped or
            # stale address in the call); cwd below applies only when the
            # record has no EDT link yet. The connection argument does not
            # change the address — it (re)fills the stored connection
            # string used by the configurator option.
            url = existing.url
            edt_link = dict(existing.edt) if existing.edt else None
            source = "записи в мапе"
            resolution = None
            edt_bound_by_cwd = False
            stored_connection = existing.connection
            if connection:
                stored_connection = _normalize_connection_arg(connection)
                connection_note = "передана в вызове"
        else:
            edt_link, source = None, "url"
            resolution = None
            edt_bound_by_cwd = False
            alias_adopted = False
        named_entry = None
        named_ref = None
        if not url and not connection:
            target_found, resolution = ctx._resolve_by_cwd(cwd)
            if not resolution:
                # The base's EXACT name in the 1cestart list (ibases.v8i)
                # is an address too (user request, 06.09): its Connect
                # gives the publication url, the alias defaults to Ref.
                named_entry = edt.entry_by_name(base) if base else None
                if not named_entry:
                    raise RuntimeError(
                        "Не удалось определить базу из воркспейса EDT по cwd"
                        + (f" ({cwd})" if cwd else " (cwd не передан)")
                        + ". Укажите url (http://host/base), connection "
                          '(строку подключения Srvr="...";Ref="...";) или '
                          "точное название базы из списка баз (параметр base).")
                parts = edt.parse_connect(named_entry["connect"])
                file_dir = edt.file_path(parts)
                if not file_dir:
                    try:
                        url = edt.upstream_url(parts)
                    except edt.EdtError as exc:
                        raise RuntimeError(f"База «{base}» найдена в списке баз, "
                                           f"но строка подключения не годится: "
                                           f"{exc}") from exc
                named_ref = parts.get("Ref")
                source = "списка баз"
                stored_connection = edt.normalize_connection(named_entry["connect"])
                connection_note = "из списка баз"
            else:
                if resolution.get("file"):
                    file_dir = resolution["file"]
                else:
                    url = resolution["url"]
                edt_link = {"workspace": resolution["workspace"],
                            "project": resolution["project"]}
                source = "edt"
                if resolution.get("connect"):
                    stored_connection = edt.normalize_connection(resolution["connect"])
                    connection_note = "по cwd (ibases.v8i)"
        elif connection and not existing:
            stored_connection = _normalize_connection_arg(connection)
            connection_note = "из параметра connection"
            parts = edt.parse_connect(connection)
            file_dir = edt.file_path(parts)
            if not file_dir:
                try:
                    url = edt.upstream_url(parts)
                except edt.EdtError as exc:
                    raise RuntimeError(f"Строка подключения не годится: {exc}") from exc
            source = "connection"
        if file_dir:
            if auth_mode == "windows":
                raise RuntimeError("auth_mode «windows» для файловой базы не "
                                   "поддерживается — ibsrv публикует базу "
                                   "локально; используйте Basic-ключ "
                                   "(rsv-data-router set-secret) или снимите "
                                   "auth_mode.")
            return await _register_file_target(
                ctx, base=base, file_dir=file_dir, existing=existing,
                comment=comment, edt_link=edt_link, platform=platform,
                check=check, install=install, source=source)
        if named_ref:
            # The caller used the list NAME as the address: the alias
            # becomes the Ref (the name itself breaks the alias rules and
            # is clumsy to pass around).
            name_used, base = base, named_ref
        if cwd and not edt_link:
            # Address came from url/connection, but the agent gave a
            # workspace too: bind the target to the EDT project when that
            # project points at the same base (root url match) — install
            # via EDT and no-base routing depend on this link. Best effort:
            # a mismatch or an unresolvable cwd just skips the binding.
            try:
                _, cwd_resolution = ctx._resolve_by_cwd(cwd)
            except RuntimeError:
                cwd_resolution = None
            if cwd_resolution and base_root(cwd_resolution.get("url") or "").lower() \
                    == base_root(url).lower():
                edt_link = {"workspace": cwd_resolution["workspace"],
                            "project": cwd_resolution["project"]}
                edt_bound_by_cwd = True
                if not stored_connection and cwd_resolution.get("connect"):
                    stored_connection = edt.normalize_connection(
                        cwd_resolution["connect"])
                    connection_note = "по cwd (ibases.v8i)"
        if not base:
            # Alias reuse: an existing record with the same url root IS the
            # same base — take its alias instead of deriving a duplicate
            # (Ref) or failing; Ref from connection/resolution is the
            # fallback for a genuinely new base.
            for known in ctx.store.snapshot():
                if base_root(known.url).lower() == base_root(url).lower():
                    base = known.alias
                    existing = known
                    alias_adopted = True
                    break
        if not base:
            if connection:
                base = edt.parse_connect(connection).get("Ref")
            elif source == "edt":
                base = resolution.get("connect", {}).get("Ref")
            if not base:
                raise RuntimeError("Укажите base — короткий алиас базы.")
        target, updated = ctx.store.register(base, url, comment=comment,
                                             edt=edt_link, platform=platform,
                                             connection=stored_connection,
                                             auth_mode=auth_mode)
        ctx.log.write(f"register_target -> {target.alias}: "
                      f"{'обновлена' if updated else 'зарегистрирована'} {target.url}")
        lines = [f"Цель «{base}» {'обновлена' if updated else 'зарегистрирована'}: {target.url}"
                 f" (адрес из {source})"]
        if named_entry:
            lines.append(f"База найдена в списке баз по точному имени "
                         f"«{name_used}»; алиас — Ref из её строки "
                         f"подключения: {base}.")
        if existing:
            note = ("Повторная регистрация: использованы сохранённые имя, "
                    "url и EDT-связь записи (url вызова проигнорирован")
            note += ("; переданная connection сохранена в записи)"
                     if connection else ")")
            lines.append(note)
        elif alias_adopted:
            lines.append("Алиас взят из существующей записи той же базы "
                         "(совпал корень url) — новая цель не создана.")
        if stored_connection and connection_note:
            lines.append(f"Строка подключения сохранена ({connection_note}): "
                         f"{stored_connection} — её использует вариант "
                         "конфигуратора при установке/удалении расширения.")
        elif not stored_connection:
            lines.append("Строка подключения не определена (регистрация по url): "
                         "установка расширения на эту базу недоступна — база "
                         "пригодна для работы, установку выполнит администратор; "
                         "удаление — только через точку /remove.")
        if target.edt:
            lines.append(f"EDT-связь: {target.edt['project']} "
                         f"({target.edt['workspace']}) — вызовы без base из этого "
                         "проекта пойдут на эту цель.")
            if resolution is not None and resolution.get("auto_project"):
                lines.append("Проект определён автоматически: в воркспейсе "
                             "единственный проект с серверной базой.")
            elif edt_bound_by_cwd:
                lines.append("EDT-связь установлена по cwd: проект этого воркспейса "
                             "указывает на ту же базу (install=true сможет ставить "
                             "расширение через EDT).")
        if target.platform:
            lines.append(f"Платформа (фолбэк конфигуратор-варианта): "
                         f"{target.platform} — используется, только если авто-"
                         "резолв (EDT-связь, скан воркспейсов, ibases) молчит.")
        if install and not check:
            lines.append("Установка расширения пропущена: check=False — ключ install "
                         "игнорируется.")
        elif install:
            # Install always runs as a background job: the configurator
            # option takes minutes and a synchronous answer dies on the
            # client timeout. Registration is done, the report — via
            # job_status(job_id).
            clash = ctx.jobs.running(target.alias)
            if clash:
                lines.append(f"Установка НЕ запущена: для цели «{target.alias}» уже "
                             f"идёт задание {clash.id} ({clash.kind}, старт "
                             f"{clash.started}) — дождитесь его результата "
                             f"job_status(job_id=\"{clash.id}\").")
            else:
                # Preflight (blocking HTTP + local files, worker thread):
                # a chain that is known-dead before the launch reports right
                # here — no doomed background job (live-case lesson).
                go, refusal = await asyncio.to_thread(
                    installer.preflight, target, False,
                    getattr(ctx, "rsvdata_path", None))
                if not go:
                    lines.extend(refusal)
                else:
                    job = ctx.start_job(target.alias, jobs.KIND_INSTALL,
                                        lambda j: asyncio.to_thread(
                                            installer.run_chain, ctx, target, j))
                    lines.append(f"Установка расширения запущена ФОНОВЫМ ЗАДАНИЕМ "
                                 f"(вариант конфигуратора может занять пару минут): "
                                 f"id задания {job.id}.")
                    lines.append(f"Статус и построчный отчёт: job_status(job_id=\"{job.id}\") "
                                 "— опрашивайте через 20–30 с; этот вызов можно "
                                 "считать завершённым.")
        elif check:
            # Shared availability report (blocking HTTP): worker thread.
            # Diagnosis + next steps on failure — auth key, publication,
            # or both at once — same as the tail of the install chain.
            lines.extend(await asyncio.to_thread(installer.availability_check, target))
        kind = target.auth_kind()
        if target.auth_is_windows:
            lines.append("Авторизация: windows (SSPI) — запросы и конфигуратор идут "
                         "от учётной записи ОС процесса роутера (Negotiate/NTLM, "
                         "/WA+); ключ set-secret не используется. Если ключ "
                         "авторизации когда-то задавался — не помешает, но можно "
                         "убрать командой remove-secret.")
        elif kind != "нет" and "пусто" not in kind:
            lines.append(f"Авторизация: {kind} (сохранена прежняя — при обновлении "
                         "не затирается; заменить — командой set-secret из справки ниже).")
        else:
            lines.append("Авторизация: не задана (запросы идут без Authorization).")
        lines.append("")
        lines.append(BASIC_HELP.format(path=ctx.store.path, alias=base))
        return "\n".join(lines)

    @app.tool(name="unregister_target")
    async def unregister_target(base: str, uninstall: bool = True,
                                platform: str | None = None) -> str:
        """Удаление цели из мапы роутера.

        ПО УМОЛЧАНИЮ (uninstall=true) перед удалением записи снять с базы
        расширение RSVData — обычный сценарий «свернуть базу»; uninstall=false
        удаляет только запись, базу не трогает.
        Порядок — зеркало установки register_target(install=true),
        но точка /remove первая:
          1) /remove (расширение 1.3.6+): динамическое удаление, монопольный
             режим не нужен; «отключено» (базу держат сеансы, удалить сейчас
             нельзя) — НЕ ошибка: расширение переведено в неактивное и не
             загружается, доудалить можно позже повтором с uninstall=true;
          2) EDT через MCP:RSV (нужна EDT-связь цели и открытый проект);
             на опубликованной базе может не пройти — сеансы держат базу
             (держатели придут поимённо); операция долгая: клиент-серверная
             база — минуты; если EDT ушла в фон, отчёт скажет, запись
             сохранится до повтора;
          3) конфигуратор /DeleteCfg -Extension RSVData (точная сборка
             платформы базы, нужны данные авторизации в targets.json;
             код возврата 1 означает и отсутствие расширения, и ошибку —
             отчёт назовёт эту неоднозначность).
        platform — тот же фолбэк, что у register_target: версия платформы
          или путь к 1cv8.exe, если записи цели он не достался и авто-резолв
          молчит (действует на этот вызов).
        Перед запуском задания — предпроверка: если заранее известно, что
        ни один вариант не сработает (нет источника строки подключения —
        регистрация по url; или не определена версия платформы), ошибка с
        диагностикой возвращается СРАЗУ, фоновое задание не создаётся.
        Удаление идёт ФОНОВЫМ ЗАДАНИЕМ: ответ несёт идентификатор, статус и
        отчёт — job_status(job_id="ид") (опрашивайте через 20–30 с). По
        завершении задания: расширения нет / удалено / отключено — запись
        цели удаляется автоматически; удалить не удалось ни одним способом
        — запись СОХРАНЯЕТСЯ: доступ к базе из роутера и креды для
        повторной попытки не теряются, отчёт назовёт причину и следующий
        шаг. Сразу после «удалено» пул HTTP-сеансов ~25–30 с дорабатывает
        старым кодом: запросы к базе отвечают 500 «Расширение RSVData не
        найдено», затем 404 — это норма, а не ошибка удаления.
        uninstall=false — просто удалить запись, базу не трогать.
        В ЛЮБОМ случае удаление записи из мапы удаляет и ключ цели из keyring
        ОС (если он там был): запись и её секрет живут/умирают вместе. При
        отказе предпроверки или неудачной цепочке запись и ключ СОХРАНЯЮТСЯ —
        креды нужны для повторной попытки.
        """
        target = ctx.store.get(base)
        if target is None:
            available = ", ".join(ctx.store.aliases()) or "—"
            raise RuntimeError(f"База «{base}» не зарегистрирована. Доступные: {available}")
        had_key = target.auth_kind() == "basic (keyring)"
        if not uninstall:
            ctx.store.unregister(base)
            ctx.log.write(f"unregister_target -> {base}: удалена")
            lines = [f"Цель «{base}» удалена."]
            if had_key:
                lines.append("Ключ цели удалён из keyring ОС.")
            return "\n".join(lines)
        if platform and not target.platform:
            target.platform = platform  # hint for this attempt only
        clash = ctx.jobs.running(base)
        if clash:
            return (f"Удаление НЕ запущено: для цели «{base}» уже идёт задание "
                    f"{clash.id} ({clash.kind}, старт {clash.started}) — дождитесь "
                    f"его результата job_status(job_id=\"{clash.id}\").")
        if target.is_file:
            return await _unregister_file_target(ctx, target, base, had_key)
        go, refusal = await asyncio.to_thread(installer.preflight, target,
                                              remove=True)
        if not go:
            ctx.log.write(f"unregister_target -> {base}: удаление не запущено "
                          "(предпроверка: цепочка заведомо не выполнится)")
            return "\n".join(refusal)

        def factory(job):
            async def _run():
                removed, _lines = await asyncio.to_thread(
                    installer.run_removal_chain, ctx, target, job)
                if removed:
                    try:
                        ctx.store.unregister(base)
                    except TargetError:
                        pass  # unregistered concurrently — the goal state holds
                    job.append(f"Запись цели «{base}» удалена из мапы роутера.")
                    if had_key:
                        job.append("Ключ цели удалён из keyring ОС.")
                    ctx.log.write(f"job -> {base}: расширение удалено/отключено, "
                                  "запись удалена")
                else:
                    job.append(f"Запись цели «{base}» СОХРАНЕНА (доступ к базе и "
                               "креды — для повторной попытки).")
                    ctx.log.write(f"job -> {base}: расширение НЕ удалено, "
                                  "запись сохранена")
            return _run()

        job = ctx.start_job(base, jobs.KIND_REMOVAL, factory)
        return (f"Удаление расширения с базы «{base}» запущено ФОНОВЫМ ЗАДАНИЕМ "
                f"(вариант конфигуратора может занять пару минут): id задания {job.id}.\n"
                f"Статус и построчный отчёт: job_status(job_id=\"{job.id}\") — "
                "опрашивайте через 20–30 с; по завершении запись цели будет "
                "удалена автоматически («удалено»/«отключено»/«расширения не "
                "было») вместе с ключом из keyring, либо сохранена с причиной "
                "и следующим шагом.")

    @app.tool(name="list_targets")
    def list_targets(check: bool = False) -> str:
        """Список зарегистрированных целей роутера.

        check=True — дополнительно проверить связь с каждой целью (ping);
        для RSVData 1.3.5+ в строке цели показываются версия и хеш расширения.
        check=False — без сетевых запросов.
        """
        targets = ctx.store.snapshot()
        if not targets:
            ctx.log.write("list_targets: целей нет")
            return ("Целей нет. Зарегистрируйте базу: register_target("
                    'url="http://host/base", base="алиас") '
                    'или connection="Srvr=...;Ref=...;".')
        lines = [f"Цели роутера ({ctx.store.path}):"]
        for target in targets:
            line = f"  {target.alias} — {target.url} — auth: {target.auth_kind()}"
            if target.is_file:
                state = ctx.ibsrv.status(target.alias)
                line += (f" — файловая: {target.file_path} "
                         f"({'сервер работает' if state.get('running') else 'сервер остановлен'})")
            if target.edt:
                line += f" — EDT: {target.edt['project']}"
            if target.comment:
                line += f" — {target.comment}"
            lines.append(line)
        if check:
            lines.append("Проверка связи:")
            failed = 0
            for target in targets:
                try:
                    answer = forward.call_tool(target.mcp_url, "ping", {},
                                               target.auth_headers(), CHECK_TIMEOUT_S,
                                               wa=target.auth_is_windows)
                    info = forward.parse_extension_info(answer)
                    if info:
                        lines.append(f"  {target.alias}: ок — расширение {info[0]}, "
                                     f"хеш {info[1]}")
                    else:
                        lines.append(f"  {target.alias}: ок (расширение без версии — "
                                     "RSVData старее 1.3.5)")
                except forward.TransportError as exc:
                    failed += 1
                    lines.append(f"  {target.alias}: НЕДОСТУПНА — {forward.explain_short(exc)}")
                except RuntimeError as exc:
                    failed += 1
                    lines.append(f"  {target.alias}: НЕДОСТУПНА — {exc}")
            if failed:
                lines.append("")
                lines.append("Подробности — ping по нужной цели; ответ покажите пользователю как есть.")
                lines.append(forward.AGENT_NOTE)
        return "\n".join(lines)

    @app.tool(name="start_server")
    async def start_server(base: str | None = None) -> str:
        """Запуск локального сервера ibsrv файловой базы (по команде).

        Обычно вызывать не нужно: сервер поднимается лениво при первом
        обращении к базе. Остановка — только явная (stop_server).
        base — алиас файловой цели; без него — единственная файловая цель.
        """
        target = _pick_file_target(ctx, base)
        try:
            target = await asyncio.to_thread(ctx.ensure_file_server, target)
        except ibsrv.IbsrvError as exc:
            raise RuntimeError(str(exc)) from exc
        return (f"Сервер файловой базы «{target.alias}» запущен: {target.url}. "
                f"Остановка — stop_server(base=\"{target.alias}\").")

    @app.tool(name="stop_server")
    def stop_server(base: str | None = None) -> str:
        """Явная остановка локального сервера ibsrv файловой базы.

        base — алиас файловой цели; без него — единственная файловая цель.
        Пока сервер не остановлен, файловую базу нельзя открыть
        конфигуратором/EDT/ibcmd — при необходимости остановите её здесь.
        """
        target = _pick_file_target(ctx, base)
        if not ctx.ibsrv.is_running(target.alias):
            return f"Сервер файловой базы «{target.alias}» уже остановлен."
        ctx.ibsrv.stop(target.alias)
        return f"Сервер файловой базы «{target.alias}» остановлен."

    @app.tool(name="server_status")
    def server_status(base: str | None = None) -> str:
        """Состояние локального сервера ibsrv файловой базы.

        base — алиас файловой цели; без него — единственная файловая цель.
        """
        target = _pick_file_target(ctx, base)
        state = ctx.ibsrv.status(target.alias)
        if not state.get("running"):
            return (f"Сервер файловой базы «{target.alias}» не запущен "
                    f"(база: {target.file_path}).")
        return (f"Сервер файловой базы «{target.alias}» работает: pid {state.get('pid')}, "
                f"адрес {state.get('url') or target.url}, "
                f"платформа {target.platform or '—'}.")

    @app.tool(name="job_status")
    def job_status(
        job_id: Annotated[str | None, Field(
            description="Идентификатор задания из ответа register_target(install=true) "
                        "или unregister_target(uninstall=true); без параметра — "
                        "список последних заданий.")] = None,
    ) -> str:
        """Статус фонового задания установки/удаления расширения.

        register_target(install=true) и unregister_target(uninstall=true)
        запускают цепочку фоновым заданием и сразу возвращают его id.
        Опрашивайте job_status(job_id="ид") через 20–30 с, пока задание
        выполняется (в ответе — уже пройденные шаги и время). Готовое
        задание отвечает полным отчётом цепочки: установка/удаление
        выполнены или нет, что делать дальше. Задания живут в памяти
        процесса: после перезапуска роутера они теряются (цепочки можно
        безопасно повторить). Повторный запуск той же операции по цели с
        идущим заданием отклоняется.
        """
        if not job_id:
            return jobs.listing(ctx.jobs.snapshot())
        job = ctx.jobs.get(job_id)
        if job is None:
            return (f"Задание «{job_id}» неизвестно: id взят из ответа "
                    "register_target/unregister_target; задания живут в памяти "
                    "процесса и теряются при перезапуске роутера. Если роутер "
                    "перезапускался — повторить операцию "
                    "(register_target(install=true) / unregister_target("
                    "uninstall=true)); цепочки можно повторять безопасно. "
                    "Список живых заданий: job_status() без параметра.")
        return jobs.describe(job)

    @app.tool(name="update_targets")
    async def update_targets(
        targets: Annotated[str | list[str] | None, Field(
            description="Алиас цели, список алиасов (массивом или через запятую); "
                        "без параметра — все зарегистрированные цели.")] = None,
    ) -> str:
        """Массовое самообновление расширения RSVData на зарегистрированных целях.

        POST <база>/hs/rsvdata/update телом бинарника --rsvdatabinary — тот же
        шаг, что в register_target(install=true), но БЕЗ регистрации и БЕЗ
        вариантов EDT/конфигуратора: они нужны только для первичной установки
        на базах, где точки /update ещё нет. Цели с RSVData 1.3.5+ обновляются
        (ответ: обновлено с было->стало или «пропуск», если хеш совпал); цель
        без точки /update попадает в отчёт с подсказкой про первичную
        установку. Отчёт построчный + итог; ошибки апстрима — готовым
        классифицированным блоком.
        """
        cfe_path = getattr(ctx, "rsvdata_path", None)
        if not cfe_path or not os.path.isfile(cfe_path):
            raise RuntimeError("Бинарник RSVData роутеру не выдан: запустите роутер "
                               "с --rsvdatabinary <путь к .cfe> и повторите.")
        snapshot = ctx.store.snapshot()
        if targets is None:
            chosen = snapshot
        else:
            wanted = ([item.strip() for item in targets.split(",") if item.strip()]
                      if isinstance(targets, str)
                      else [str(item).strip() for item in targets if str(item).strip()])
            known = {target.alias: target for target in snapshot}
            unknown = [alias for alias in wanted if alias not in known]
            if unknown:
                available = ", ".join(known) or "—"
                raise RuntimeError(f"Цели не зарегистрированы: {', '.join(unknown)}. "
                                   f"Доступные: {available}.")
            chosen = [known[alias] for alias in wanted]
        if not chosen:
            raise RuntimeError("Нет зарегистрированных целей — обновлять нечего. "
                               "Регистрация: register_target(url=..., base=...).")
        ctx.log.write(f"update_targets -> {len(chosen)} цел.: "
                      + ", ".join(t.alias for t in chosen))
        # Blocking HTTP: worker thread, the event loop keeps serving.
        return "\n".join(await asyncio.to_thread(installer.run_mass_update,
                                                 ctx, chosen))
