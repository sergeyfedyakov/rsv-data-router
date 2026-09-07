# Спайк: POST <база>/hs/rsvdata/update с бинарником расширения.
# Прототип служебного update_targets роутера (этап 9 дорожной карты).
#
# Запуск: python tests/spike_update.py <алиас> <путь-к-cfe>
# Auth берётся из targets.json (как у самого роутера).

import os
import sys
import urllib.error
import urllib.request

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOME, "src"))

from rsv_data_router.targets import ROOT_PATH, TargetsStore  # noqa: E402


def main():
    alias, cfe_path = sys.argv[1], sys.argv[2]
    target = TargetsStore(HOME).get(alias)
    if target is None:
        raise SystemExit(f"Цель «{alias}» не зарегистрирована")

    # target.url — корень публикации (B0); служебная точка — /hs/rsvdata/update.
    url = target.url.rstrip("/") + ROOT_PATH + "/update"

    with open(cfe_path, "rb") as handle:
        data = handle.read()

    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/octet-stream")
    for name, value in target.auth_headers().items():
        request.add_header(name, value)

    print(f"POST {url}  ({len(data)} bytes)")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            print("HTTP", response.status)
            print(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        print("HTTP", error.code)
        print(error.read().decode("utf-8", "replace"))


if __name__ == "__main__":
    main()
