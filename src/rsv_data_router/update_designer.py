"""One-time RSVData extension bootstrap via Configurator batch mode.

For bases whose extension predates the /update self-update endpoint
(RSVData 1.3.3 and older): loads bin/RSVData.cfe through the designer
command line and verifies the result with a direct ping. The key comes from
the target's auth source (OS keyring or the inline basic value) and is
passed to the configurator command line only — it is never printed, logged
or written anywhere. The key
is OPTIONAL: a target without one runs the configurator without /N /P
(bases without users; user decision 07.09), a present-but-unreadable key
refuses («данные авторизации повреждены»).

The configurator must run in the exact platform build of the base —
there is no compatibility between neighboring builds (user decision,
06.09; the previous "newest installed" pick pushed 8.5.1 tooling onto
8.3 bases). The required version comes from the EDT workspace
(.infobase-binding, fallback DefaultVersion in ibases.v8i); when that
exact build is not installed the run refuses instead of substituting.
--designer overrides the whole resolution with an explicit binary.

Load and DB update run as two separate designer invocations: combining
/LoadCfg and /UpdateDBCfg in one call is rejected by the batch parser
("Ошибка в параметрах командной строки", platform 8.5.1).

Usage (from the project root, after "pip install -e ."):
    python -m rsv_data_router.update_designer --target <alias>
    python -m rsv_data_router.update_designer --target <alias> --designer "C:\\...\\bin\\1cv8.exe"

Exit codes: 0 — loaded and applied (ping confirms), 1 — any failure.
"""

import argparse
import base64
import glob
import os
import re
import shutil
import subprocess
import sys
import time

from . import edt, forward, paths
from .targets import TargetsStore, _looks_like_base64_key

EXT_NAME = "RSVData"
DESIGNER_TIMEOUT_S = 900


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="alias in targets.json")
    parser.add_argument("--cfe", default=os.environ.get("RSV_DATA_ROUTER_CFE")
                        or os.path.join("bin", "RSVData.cfe"),
                        help="path to RSVData.cfe (default: bin/RSVData.cfe in the "
                             "current dir, env RSV_DATA_ROUTER_CFE)")
    parser.add_argument("--designer", default=None,
                        help="path to 1cv8.exe (default: the exact platform build "
                             "of the base, resolved from the EDT workspace)")
    parser.add_argument("--log", default=None,
                        help="designer /Out log base (default: .designer-<target>.log "
                             "in the current dir; phases get -load/-update suffixes)")
    parser.add_argument("--wait-seconds", type=int, default=30,
                        help="pause before the verification ping (HTTP session "
                             "pool serves old code up to ~25 s — spike C3)")
    return parser.parse_args()


def _version_key(text):
    """'8.3.27.2214' -> (8, 3, 27, 2214); () when no digits at all."""
    nums = re.findall(r"\d+", str(text or ""))
    return tuple(int(item) for item in nums) if nums else ()


def _version_text(key):
    return ".".join(str(item) for item in key)


ROOTS_ENV = "RSV_DATA_ROUTER_1CV8_ROOTS"


def installed_designers(roots=None):
    """{version_tuple: [1cv8.exe paths]} under the 1cv8 install roots.

    Directory names are the versions (C:\\Program Files\\1cv8\\8.3.27.2214\\bin);
    RSV_DATA_ROUTER_1CV8_ROOTS (os.pathsep-separated) overrides the default
    Program Files roots (tests, exotic layouts).
    """
    if roots is None:
        env_roots = os.environ.get(ROOTS_ENV)
        if env_roots:
            roots = [path for path in env_roots.split(os.pathsep) if path]
    found = {}
    for root in roots or [r"C:\Program Files\1cv8",
                          r"C:\Program Files (x86)\1cv8"]:
        for path in glob.glob(os.path.join(root, "*", "bin", "1cv8.exe")):
            key = _version_key(os.path.basename(
                os.path.dirname(os.path.dirname(path))))
            if len(key) >= 3:
                found.setdefault(key, []).append(path)
    return found


def single_installed_designer(roots=None):
    """(version_text, path) when EXACTLY ONE platform build is installed.

    A lone installation is what this machine runs its bases with, so it
    doubles as a last-resort answer for bases whose version no source
    names. Anything else (zero or several builds) yields (None, None).
    """
    found = installed_designers(roots)
    if len(found) == 1:
        key = next(iter(found))
        return _version_text(key), _pick(found[key])
    return None, None


def resolve_designer(version, roots=None):
    """(path, version, note) for the configurator binary.

    The EXACT base build wins (neighboring builds are not interchangeable,
    user decision 06.09). When the exact build is absent — or no version
    could be determined at all — but EXACTLY ONE platform is installed,
    that single one is returned with a note (user question, 06.09: the
    only installation is what this machine runs the base with). Refuses
    with SystemExit otherwise: several builds installed and none matches,
    or nothing installed at all.
    """
    required = _version_key(version) if version else None
    if required is not None and len(required) < 2:
        raise SystemExit(f"версия платформы базы не распознана: {version!r}")
    found = installed_designers(roots)
    if not found:
        raise SystemExit("1cv8.exe не найден в C:\\Program Files\\1cv8 — "
                         "установите платформу базы или задайте путь к "
                         "конфигуратору явно (ключ platform у цели, "
                         "--designer в update_designer.py)")
    if required and required in found:
        return _pick(found[required]), _version_text(required), None
    if len(found) == 1:
        single_version, path = single_installed_designer(roots)
        if required:
            note = (f"точная сборка {version} не установлена — использована "
                    f"единственная установленная {single_version}")
        else:
            note = (f"версия платформы базы не определена — использована "
                    f"единственная установленная {single_version}")
        return path, single_version, note
    installed = ", ".join(_version_text(key) for key in sorted(found))
    if required:
        raise SystemExit(f"не установлена точная версия платформы базы "
                         f"{_version_text(required)} (совместимости между "
                         f"соседними сборками нет); установлены: {installed} — "
                         "установите нужную платформу или задайте путь к "
                         "конфигуратору явно (ключ platform у цели, "
                         "--designer в update_designer.py)")
    raise SystemExit(f"версия платформы базы не определена, установлено "
                     f"несколько платформ ({installed}) — запросите у "
                     "пользователя номер версии (например 8.3.27.2214) или "
                     "путь к 1cv8.exe и повторите с ключом platform")


def _pick(paths):
    """x64 'Program Files' wins the tie: sorts after '(x86)'."""
    return sorted(paths)[-1]


def find_designer(version, roots=None):
    """Path of the configurator for the EXACT base build (resolve_designer)."""
    return resolve_designer(version, roots)[0]


def base_platform_version(target, ibases_path=None):
    """The base's platform version from local sources, or None.

    Order (user decision, 06.09): the EDT project linked in targets.json
    (.infobase-binding, then the Version of its ibases entry) -> the
    offline scan of every workspace the EDT starter remembers — it matches
    the project whose association points at the same host+ref and works
    with EDT closed (live-tested: a workspace binding answered where no
    target link existed) -> the Version of the ibases entry
    matched by Srvr/Ref alone (the exact build from the base's own
    parameters, the Version key). The per-target
    platform key is the operator fallback applied by the caller only when
    this returns None — auto resolution wins.
    """
    link = target.edt or {}
    workspace, project = link.get("workspace"), link.get("project")
    if workspace and project:
        uuid = edt.infobase_uuid(workspace, project)
        version = edt.infobase_version(workspace, project, uuid)
        if version:
            return version
        if uuid:
            try:
                entry = edt.read_ibases(
                    ibases_path or edt.default_ibases_path()).get(uuid)
            except edt.EdtError:
                entry = None
            if entry and entry.get("version"):
                return entry["version"]
    key = edt.connect_key_from_url(target.url)
    hit = edt.scan_platform_versions(ibases_path=ibases_path).get(key)
    if hit:
        return hit["version"]
    return edt.ibases_version(key, ibases_path=ibases_path)


def decrypt_login_password(target):
    """(login, password) from the target's stored key (inline value or the
    OS keyring entry); mirrors the shape tolerance of targets.basic_header().
    The result never leaves this function's callers except as
    configurator/ping arguments."""
    value = target.auth_value()
    if not value:
        raise SystemExit(f"Цель «{target.alias}»: ключа нет ни в targets.json, "
                         "ни в keyring (rsv-data-router set-secret "
                         f"{target.alias})")
    if value.lower().startswith("basic "):
        value = base64.b64decode(value[6:].strip()).decode("utf-8")
    elif _looks_like_base64_key(value):
        value = base64.b64decode(value).decode("utf-8")
    login, sep, password = value.partition(":")
    if not sep or not login:
        raise SystemExit(f"Цель «{target.alias}»: ключ не в формате логин:пароль")
    return login, password  # password may be empty — a base user without one


def optional_login_password(target):
    """(login, password, note) for the configurator command line; the key
    is OPTIONAL (user decision, 07.09): bases without users accept a run
    without /N /P (and a future OS-auth key will reuse the same seam), so
    a missing key degrades to login=None plus a hint note instead of
    refusing the variant. A key that is PRESENT but unreadable still
    refuses — the operator meant it to be used: «данные авторизации
    повреждены, установите заново». The result never leaves this
    function's callers except as configurator/ping arguments."""
    if not target.auth_value():
        return None, None, (
            f"Цель «{target.alias}»: ключ авторизации не задан — конфигуратор "
            "запустится без /N /P (для баз с пользователей задайте ключ: "
            f"rsv-data-router set-secret {target.alias}).")
    try:
        login, password = decrypt_login_password(target)
    except (SystemExit, RuntimeError, ValueError) as exc:
        # binascii/UnicodeDecode errors (ValueError) land here too — the
        # run_chain contract (never raise) holds only if nothing escapes.
        raise SystemExit(f"Цель «{target.alias}»: данные авторизации повреждены "
                         f"({exc}); установите ключ заново "
                         f"(rsv-data-router set-secret {target.alias})") from None
    return login, password, None


def resolve_connection(target):
    """(Srvr, Ref, display name) for the configurator command line.

    Unambiguous sources only (user decision, 07.09 — nothing is derived
    from the publication url: one web server can front several clusters):
    the stored EDT link (AssociationData -> ibases entry) first, the
    target's stored connection string second. A target with neither was
    registered by url alone — the configurator option is unavailable for
    it by design.
    """
    link = target.edt or {}
    workspace, project = link.get("workspace"), link.get("project")
    link_problem = None
    if workspace and project:
        uuid_ = edt.infobase_uuid(workspace, project)
        entry = edt.read_ibases(edt.default_ibases_path()).get(uuid_) if uuid_ else None
        if not entry:
            link_problem = (f"база проекта {project} не найдена в "
                            f"{edt.default_ibases_path()}")
        else:
            connect = edt.parse_connect(entry["connect"])
            srvr, ref = connect.get("Srvr"), connect.get("Ref")
            if srvr and ref:
                return srvr, ref, entry["name"]
            link_problem = (f"в строке подключения базы «{entry['name']}» "
                            "нет Srvr/Ref (файловая ИБ?)")
    stored = str(getattr(target, "connection", "") or "").strip()
    if stored:
        connect = edt.parse_connect(stored)
        srvr, ref = connect.get("Srvr"), connect.get("Ref")
        if srvr and ref:
            return srvr, ref, ref
    if link_problem:
        raise SystemExit(f"Цель «{target.alias}»: {link_problem}, "
                         "сохранённой строки подключения нет")
    raise SystemExit(
        f"Цель «{target.alias}»: нет источника строки подключения (нет ни "
        "edt-привязки, ни сохранённой connection — регистрация по url). "
        "Установка расширения на такую базу недоступна: использовать базу "
        "можно, установку выполнит администратор, удаление — только через "
        "точку /remove. Чтобы установкой управлял роутер, перерегистрируйте "
        "цель с connection, cwd или точным названием базы.")


def ascii_copy(cfe_path):
    """Copy the .cfe to an ASCII-only path when needed: designer batch is
    picky about non-ASCII /LoadCfg paths, while /Out tolerates them."""
    if cfe_path.isascii():
        return cfe_path
    tmp = os.path.join(os.environ.get("TEMP", os.getcwd()),
                       os.path.basename(cfe_path))
    shutil.copyfile(cfe_path, tmp)
    return tmp


def build_command(designer, srvr, ref, login, password, log_path, cfe_path,
                  phase):
    """Raw command line with docs-style quoting for one phase ("load",
    "update" or "delete"). The designer parses its own command line the
    way .bat examples show it: the connection string value is quoted,
    inner quotes doubled. login=None (no stored key — user decision,
    07.09) omits /N /P entirely: bases without users accept such runs
    (docs: /P may be dropped when the user has no password), and this is
    the seam a future OS-auth key will build on (/WA+). The result is
    never printed in full: masked_command() hides login/password.
    "delete" (/DeleteCfg -Extension) needs no .cfe: it removes the
    extension straight from the infobase (exit code 1 also means
    "extension absent" — per the batch-mode docs)."""
    if phase == "load":
        batch = f' /LoadCfg "{ascii_copy(cfe_path)}" -Extension "{EXT_NAME}"'
    elif phase == "delete":
        batch = f' /DeleteCfg -Extension "{EXT_NAME}"'
    else:
        batch = f' /UpdateDBCfg -Extension "{EXT_NAME}"'
    auth = f' /N "{login}" /P "{password}"' if login is not None else ""
    return (
        f'"{designer}" DESIGNER'
        " /DisableStartupDialogs /DisableStartupMessages"
        f' /IBConnectionString "Srvr=""{srvr}"";Ref=""{ref}"";"'
        f"{auth}"
        f' /Out "{log_path}"'
        f"{batch}")


def masked_command(command):
    return re.sub(r'(/N ")([^"]*)("|$)', r"\1***\3",
                  re.sub(r'(/P ")([^"]*)("|$)', r"\1***\3", command))


class DesignerError(RuntimeError):
    """Configurator failure carrying diagnostics for the caller's report.

    log_lines holds the /Out log tail AS IS — batch logs are the only
    error text the configurator gives, and which markers matter is not
    yet known (user decision, 07.09), so no classification happens here.
    returncode is None when the process never reported one (timeout).
    """

    def __init__(self, message, log_lines=(), returncode=None):
        super().__init__(message)
        self.log_lines = list(log_lines)
        self.returncode = returncode


def read_log_tail(log_path, lines=40):
    """(lines) of the configurator /Out log, decoded; [] when absent or
    unreadable (utf-8 first, cp1251 fallback — the batch log encoding
    depends on the platform)."""
    try:
        with open(log_path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return []
    for encoding in ("utf-8", "cp1251"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", "replace")
    return [line for line in text.splitlines() if line.strip()][-lines:]


def run_designer(command, log_path, what):
    """One designer invocation; DesignerError carries the /Out tail so
    callers route the diagnostics where they belong (job report / CLI
    stdout) instead of losing them to a bare exit code."""
    print(f"Запуск конфигуратора ({what})…")
    print("Команда:", masked_command(command))
    try:
        proc = subprocess.run(command, capture_output=True,
                              timeout=DESIGNER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise DesignerError(
            f"Конфигуратор ({what}) не уложился в {DESIGNER_TIMEOUT_S} с — "
            "прерван; проверьте журнал и состояние базы вручную",
            read_log_tail(log_path)) from None
    if proc.returncode != 0:
        tail = read_log_tail(log_path)
        print(f"Конфигуратор ({what}) завершился с кодом {proc.returncode}.")
        if tail:
            print("\n".join(tail))
        raise DesignerError(
            f"Конфигуратор ({what}) завершился с кодом {proc.returncode}.",
            tail or [f"(журнал {log_path} пуст или не создан)"],
            proc.returncode)
    print(f"Конфигуратор ({what}): успех (код 0).")


def ping_extension(url, login, password):
    """Direct tools/call ping (same shape as forward.call_tool). With no
    stored key (login=None) it goes without an Authorization header —
    publications open for anonymous answer regardless."""
    headers = None
    if login is not None:
        headers = {"Authorization": "Basic " + base64.b64encode(
            f"{login}:{password}".encode("utf-8")).decode("ascii")}
    return forward.call_tool(url, "ping", {}, headers=headers)


def version_from_ping(text):
    match = re.search(r"расширение\s+([\d.]+),\s+хеш\s+([A-Za-z0-9+/=]+)", text)
    return (match.group(1), match.group(2)) if match else (None, None)


def print_log_tail(log_path, lines=40):
    tail = read_log_tail(log_path, lines)
    print("--- журнал конфигуратора (хвост) ---")
    print("\n".join(tail) if tail else "(пусто или недоступен)")


def main():
    args = parse_args()
    if not os.path.isfile(args.cfe):
        raise SystemExit(f"Не найден файл расширения: {args.cfe}")
    store = TargetsStore(os.environ.get("RSV_DATA_ROUTER_HOME") or paths.default_home())
    target = store.get(args.target)
    if target is None:
        raise SystemExit(f"Цель «{args.target}» не зарегистрирована. "
                         f"Доступные: {', '.join(store.aliases())}")

    print(f"Цель: {target.alias} ({target.url}); ключ: {target.auth_kind()}")
    login, password, auth_note = optional_login_password(target)
    if auth_note:
        print(auth_note)
    srvr, ref, ibase_name = resolve_connection(target)
    if args.designer:
        designer = args.designer
    else:
        version = base_platform_version(target)
        if not version:
            raise SystemExit(
                "не удалось определить версию платформы базы: у проекта нет "
                ".infobase-binding, в записи ibases.v8i нет Version — "
                "укажите --designer")
        designer, used, note = resolve_designer(version)
        print(f"Платформа базы: {used}" + (f" ({note})" if note else ""))
    log_base = args.log or f".designer-{args.target}"
    size = os.path.getsize(args.cfe)
    print(f"База: {ibase_name}; Srvr={srvr}; Ref={ref}")
    print(f"Конфигуратор: {designer}")
    print(f"Расширение: {args.cfe} ({size} байт)")

    try:
        text = ping_extension(target.url, login, password)
    except (forward.TransportError, RuntimeError) as exc:
        raise SystemExit(f"Предварительный ping не прошёл, обновление отменено: {exc}")
    was = version_from_ping(text)
    print(f"Состояние до: {was[0] or '(версия не сообщается) ' + text.strip()[:120]}")

    run_designer(build_command(designer, srvr, ref, login, password,
                               log_base + "-load.log", os.path.abspath(args.cfe),
                               "load"),
                 log_base + "-load.log", "загрузка .cfe")
    run_designer(build_command(designer, srvr, ref, login, password,
                               log_base + "-update.log", os.path.abspath(args.cfe),
                               "update"),
                 log_base + "-update.log", "обновление конфигурации БД расширения")

    print(f"Пауза {args.wait_seconds} с (пул HTTP-сеансов дорабатывает старым кодом)…")
    time.sleep(args.wait_seconds)
    try:
        text = ping_extension(target.url, login, password)
    except (forward.TransportError, RuntimeError) as exc:
        print_log_tail(log_base + "-update.log")
        raise SystemExit(f"Загрузка прошла, но контрольный ping не удался: {exc}")
    became = version_from_ping(text)
    print(f"Состояние после: {became[0] or '(версия не сообщается) ' + text.strip()[:120]}")
    if became[0] is None:
        print_log_tail(log_base + "-update.log")
        raise SystemExit("Конфигуратор отчитался об успехе, но ping не показывает "
                         "версию расширения — вероятно, обновление ушло в фон "
                         "или сеанс ещё старый; повторите ping позже.")
    print(f"Готово: расширение {EXT_NAME} {was[0] or '?'} -> {became[0]}, "
          f"хеш {became[1]}")


if __name__ == "__main__":
    main()
