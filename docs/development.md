# Разработка

## Окружение

- Python 3.10+ (разработка и тесты — на 3.13, Windows; CI гоняет те же тесты
  и на Linux).
- Зависимости: [FastMCP](https://gofastmcp.com/) (ставит
  uvicorn/starlette/pydantic/httpx), platformdirs (пользовательские каталоги
  конфигурации/журнала) и keyring (хранилище ключей: Windows Credential
  Manager / Secret Service).

```bat
python -m venv .venv
.venv\Scripts\pip install -e .
```

Исходники — src-layout `src/rsv_data_router/` (версия пакета —
`__init__.__version__`). Запуск без установки: `run.bat` / `run.sh`
(PYTHONPATH=src) или `python -m rsv_data_router`.

## Тесты

Одна команда: поднимает два мока RSVData и роутер на свободных портах
(в т.ч. синтетический воркспейс EDT, фейковый `ibases.v8i`, тестовый
HTTP-сервер со статусами 401/404/409/502 и dummy-бинарник RSVData),
прогоняет все приёмочные проверки (292 на Windows, 286 на прочих ОС —
keyring e2e-блок пропускается там, где нет бэкенда), убирает за собой:

```bat
.venv\Scripts\python tests\smoke.py
```

Отдельные наборы (нужны externally запущенные моки/роутер, см. докстринги;
`stage7` — цепочка установки и предпроверка запуска, `stage8` — источники
версии платформы и подбор конфигуратора по точной сборке (оффлайн),
`stage9` — массовое обновление update_targets,
`stage10` — цепочка удаления unregister_target(uninstall=true),
`stage11` — поиск проекта цели по всем запущенным EDT (оффлайн),
`stage12` — фоновые задания и job_status,
`stage13` — ключ platform, матрица предпроверки и отказ без бинарника
(оффлайн),
`stage14` — команды кредов (set-secret/remove-secret) и цепочка auth
inline → keyring → none (оффлайн, in-memory keyring)):
`tests/stage2_check.py` … `tests/stage14_check.py`; мок — `tests/mock_upstream.py`.

## Сборка и релиз

Сборкой занимается GitHub Actions:

- `ci.yml` — smoke (`tests/smoke.py`) на ubuntu-latest и windows-latest при
  каждом push/PR — тот же прогон, что локально;
- `build.yml` — по тегу `v*` (или вручную, вкладка Actions): sdist + wheel
  (`python -m build`) и однофайловый `rsv-data-router.exe` (PyInstaller,
  ~31 МБ, Python на машине не нужен); по тегу оба артефакта прикладываются
  к GitHub Release.

Локально то же самое:

```bat
pip install build pyinstaller
python -m build
pyinstaller --noconfirm --onefile --name rsv-data-router --paths src --collect-all fastmcp entry.py
```

(`entry.py` — двухстрочный вход `from rsv_data_router.cli import main`,
в workflow генерируется на лету; сборка проверена: `--help` импортирует весь
стек, живой http-старт отвечает на MCP initialize.)

Бинарник запускается так же, как модуль (путь к `.cfe` — куда вы его
положили, см. «Запуск» в [README](../README.md)):

```bat
rsv-data-router.exe --rsvdatabinary путь\к\RSVData.cfe
```

## Формат targets.json

```json
{
  "targets": {
    "ut_demo": {
      "url": "http://srv-1c/ut_demo",
      "auth": { "type": "basic", "value": "" },
      "edt": { "workspace": "D:\\dev\\edt-workspace\\UT", "project": "UT_27" },
      "platform": "8.3.27.2214",
      "connection": "Srvr=\"srv-1c:1541\";Ref=\"ut_demo\";",
      "comment": "УТ 11.5.27.61"
    }
  }
}
```

- `url`: корень публикации базы `http://host/base` — пути сервиса роутер
  достраивает сам (`/hs/rsvdata/mcp` для вызовов, `/hs/rsvdata/update` для
  самообновления, `/hs/rsvdata/remove` для самоудаления). Значения в старом
  формате (с `/hs/rsvdata/mcp`) работают без переписывания файла: лишний
  путь обрезается при чтении.
- `auth`: ключ цели — основной способ `rsv-data-router set-secret <алиас>`
  (keyring ОС), тогда в файле остаётся пустой шаблон
  `{ "type": "basic", "value": "" }`. Запасной путь без keyring — тот же
  `"basic"` с `value`: base64 от «логин:пароль», готовая строка `Basic ...`
  или открытый «логин:пароль» (роутер нормализует любую форму). Инлайн
  приоритетнее keyring; пустое `value` — читать keyring; `"none"` или секция
  без значения и без keyring-записи — запросы без Authorization. Типы
  `"dpapi"`/`"basic-dpapi"` убраны в 0.10.0 (при чтении файла дают ошибку с
  подсказкой перенести ключ в keyring). Секцию `auth` можно удалить целиком.
- Ключ из keyring читается при каждом вызове; сегмент хранилища —
  service `rsv-data-router`, username — алиас цели. Бэкенда нет (headless
  Linux, CI) — keyring считается пустым, работает инлайн-вариант.
- `edt` (необязательно) — связь с проектом 1C:EDT: вызовы без `base` и с
  `cwd` внутри этого проекта идут на эту цель. Признака «цели по умолчанию»
  нет: с несколькими открытыми EDT у каждого своя база, маршрут определяется
  `cwd` агента; без `cwd` и при нескольких целях роутер попросит `base`.
- `platform` (необязательно) — фолбэк для конфигуратор-варианта
  (`register_target`/`unregister_target`, ключ `platform`): номер версии
  платформы (`8.3.27.2214`) или полный путь к `1cv8.exe`. Используется,
  только когда авто-резолв (EDT-связь → скан воркспейсов → `ibases.v8i`)
  не ответил; хранится в записи, чтобы удаление взяло то же значение.
- `connection` (необязательно) — сохранённая строка подключения базы
  `Srvr="host:port";Ref="base";` (без учётных данных; порт кластера
  сохраняется). Заполняется роутером при регистрации с `connection`, по
  `cwd` или по названию базы; из url НЕ выводится (один веб-сервер может
  обслуживать несколько кластеров). Источник подключения для
  конфигуратор-варианта установки/удаления наряду с `edt`-привязкой
  (привязка приоритетна). Повторная регистрация обновляет её, только
  если `connection` передана в вызове.
- Файл можно править руками — изменения подхватываются без перезапуска.

Расположение по умолчанию — каталог конфигурации пользователя
(`%LOCALAPPDATA%\rsv-data-router` в Windows, `~/.config/rsv-data-router` в
Linux); журнал — в каталоге логов рядом. Явный `--home` / env
`RSV_DATA_ROUTER_HOME` складывает оба файла в один каталог.

## Планы развития

- Windows-аутентификация публикаций (Negotiate) — отложена.
