# Спайк: POST <база>/hs/rsvdata/remove — самоудаление расширения (этап 10 дорожной карты).
#
# Запуск: python tests/spike_remove.py <алиас>
# Auth берётся из targets.json (как у самого роутера).

import os
import sys
import urllib.error
import urllib.request

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOME, "src"))

from rsv_data_router.targets import ROOT_PATH, TargetsStore  # noqa: E402


def main():
    alias = sys.argv[1]
    target = TargetsStore(HOME).get(alias)
    if target is None:
        raise SystemExit(f"Цель «{alias}» не зарегистрирована")

    # target.url — корень публикации; служебная точка живёт под /hs/rsvdata/remove
    # (зеркально Target.update_url, свойство remove_url появится в роутере 0.7.0).
    url = target.url.rstrip("/") + ROOT_PATH + "/remove"

    request = urllib.request.Request(url, data=b"", method="POST")
    for name, value in target.auth_headers().items():
        request.add_header(name, value)

    print(f"POST {url}")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            print("HTTP", response.status)
            print(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        print("HTTP", error.code)
        print(error.read().decode("utf-8", "replace"))


if __name__ == "__main__":
    main()
