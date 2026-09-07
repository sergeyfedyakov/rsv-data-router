"""Console commands of the rsv-data-router package (the entry point).

No subcommand (or "serve") starts the MCP server — all server options stay
top-level, so existing launch lines keep working. The credential commands
replace the former set_secret.ps1/set_secret.sh scripts:

    rsv-data-router set-secret <алиас>      # login+password -> OS keyring
    rsv-data-router remove-secret <алиас>   # drop the entry
    rsv-data-router --version
"""

import argparse
import sys

from . import __version__, credentials

SECRET_COMMANDS = ("set-secret", "remove-secret")


def cmd_set_secret(argv):
    """Ask for login/password (never through the chat) and store the key."""
    parser = argparse.ArgumentParser(
        prog="rsv-data-router set-secret",
        description="Сохранить ключ цели в keyring ОС (Windows Credential "
                    "Manager / Secret Service). Роутер подхватит его без "
                    "перезапуска.")
    parser.add_argument("alias", help="алиас цели из targets.json")
    args = parser.parse_args(argv)
    login, password = credentials.prompt_key()
    credentials.set_value(args.alias, credentials.encode_key(login, password))
    print(f"Готово: ключ цели «{args.alias}» сохранён в keyring "
          f"(service={credentials.SERVICE}). Перезапуск роутера не нужен.")
    print("Проверка: list_targets (check=true) или любой вызов с base="
          f"{args.alias}.")
    return 0


def cmd_remove_secret(argv):
    parser = argparse.ArgumentParser(
        prog="rsv-data-router remove-secret",
        description="Удалить ключ цели из keyring ОС. Записи targets.json "
                    "это не трогает; unregister_target ключ тоже не удаляет.")
    parser.add_argument("alias", help="алиас цели")
    args = parser.parse_args(argv)
    if credentials.delete_value(args.alias):
        print(f"Готово: ключ цели «{args.alias}» удалён из keyring.")
    else:
        print(f"Ключа цели «{args.alias}» в keyring нет (или бэкенд недоступен).")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv:
        print(f"rsv-data-router {__version__}")
        return 0
    if argv and argv[0] in SECRET_COMMANDS:
        command, rest = argv[0], argv[1:]
        return {"set-secret": cmd_set_secret,
                "remove-secret": cmd_remove_secret}[command](rest)
    from .server import main as serve_main
    return serve_main(argv)


if __name__ == "__main__":
    sys.exit(main())
