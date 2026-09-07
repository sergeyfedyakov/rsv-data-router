# rsv-data-router

**MCP-роутер для RSVData: одна точка входа ИИ-агента к данным нескольких баз 1С.**

Расширение [RSVData](https://github.com/sergeyfedyakov/mcp-rsv-data) публикует в каждой базе 1С
HTTP-сервис MCP (чтение данных и структура метаданных — `query`, `get_structure`, `eventlog`
и др.). Роутер стоит между ИИ-клиентом (ZCode, Claude Code, Cursor, …) и этими базами:

- в клиенте настраивается **одна запись** вместо по записи на базу;
- мапа «алиас → база» хранится в `targets.json` и управляется прямо из диалога
  (`register_target` / `unregister_target` / `list_targets`);
- каждый инструмент данных получает параметры **`base`** (алиас цели) и **`cwd`**
  (рабочий каталог агента): без `base` база определяется из воркспейса EDT, где
  лежит `cwd`, иначе берётся единственная зарегистрированная цель;
- логин/пароль **не передаются через чат**: ключ хранится в keyring ОС
  (Windows Credential Manager / Secret Service) и подхватывается на лету;
- установка/обновление/удаление расширения на базах — фоновыми цепочками
  (точка `/update` → EDT → конфигуратор) прямо из диалога;
- журнал всех вызовов — `router.log` (инструмент, цель, статус, длительность).

```
ИИ-клиент (одна запись: rsvdata → http://127.0.0.1:8780/mcp)
  └─ роутер (FastMCP: stateless HTTP + stdio)
       ├─ 9 инструментов данных — форвард в выбранную базу (+ base/cwd)
       ├─ 7 служебных — register_target / unregister_target / list_targets /
       │                update_targets / job_status / get_rsvdata / help_router
       └─ targets.json: алиас → {url, auth, edt?, platform?, connection?, comment}
```

## Требования и установка

- Python 3.10+ (разработка и тесты — на 3.13, Windows).
- Зависимости: [FastMCP](https://gofastmcp.com/) (ставит uvicorn/starlette/pydantic/httpx),
  platformdirs (пользовательские каталоги конфигурации/журнала) и
  keyring (хранилище ключей: Windows Credential Manager / Secret Service).

```bat
python -m venv .venv
.venv\Scripts\pip install -e .
```

Готовый бинарник без Python — собирается по тегу `v*`, см. «Сборка и релиз»
в [docs/development.md](docs/development.md).

## Запуск

```bat
run.bat                                   :: Windows, http://127.0.0.1:8780/mcp
./run.sh                                  :: Linux/macOS/Git Bash
rsv-data-router --help                    :: все опции (или: python -m rsv_data_router)
```

`run.bat` и `run.sh` передают `--rsvdatabinary <каталог>\bin\RSVData.cfe`,
только если файл существует: положите туда бинарник расширения (репозиторий
и релизы проекта [RSVData](https://github.com/sergeyfedyakov/mcp-rsv-data)), и
`get_rsvdata` + `GET /bin/rsvdata.cfe` заработают сразу. Сам .cfe в составе
этого репозитория не распространяется; что доступно без бинарника, а что нет —
таблица в [docs/tools-router.md](docs/tools-router.md).

| Опция | Назначение |
|---|---|
| `--transport http\|stdio` | транспорт (по умолчанию http; stdio — локальные эксперименты) |
| `--host`, `--port` | адрес/порт http (по умолчанию `127.0.0.1:8780`) |
| `--network` | слушать во всей сети (`0.0.0.0`); обязателен `--token` |
| `--token СЕКРЕТ` | клиенты обязаны передавать заголовок `X-Router-Token` |
| `--home КАТАЛОГ` | рантайм: и `targets.json`, и `router.log` в одном каталоге; по умолчанию — `targets.json` в каталоге конфигурации пользователя (`%LOCALAPPDATA%\rsv-data-router` в Windows, `~/.config/rsv-data-router` в Linux), журнал — в каталоге логов; env `RSV_DATA_ROUTER_HOME` |
| `--rsvdatabinary ПУТЬ` | бинарник расширения RSVData (.cfe): инструмент `get_rsvdata` отдаёт его агентам, http-транспорт раздаёт по `GET /bin/rsvdata.cfe` |
| `--rsvmcp URL` | адрес MCP:RSV в EDT (`http://127.0.0.1:8770/mcp`) — через него `register_target(install=true)` ставит расширение, пока открыт проект этой базы; пустая строка — отключить вариант EDT. Если запущено несколько EDT, проект цели ищется и в соседних экземплярах (`otherEdtInstances`), вызов адресуется им параметром `edtWorkspace` |

env-переменные (обычно не нужны; так тесты подменяют источники):
`RSV_DATA_ROUTER_HOME`, `RSV_DATA_ROUTER_IBASES` (свой `ibases.v8i`),
`RSV_DATA_ROUTER_EDT_PROJECTS` (свой реестр воркспейсов `1cedtstart/projects.json`),
`RSV_DATA_ROUTER_1CV8_ROOTS` (свои корни установок 1cv8, через `;` на Windows).

Пример с доступом из сети:

```bat
rsv-data-router --network --token ДЛИННЫЙ-СЕКРЕТ
```

## Подключение ИИ-клиента

Блок для ZCode / Claude Code (`mcpServers`):

```json
{
  "mcpServers": {
    "rsvdata": {
      "type": "http",
      "url": "http://127.0.0.1:8780/mcp"
    }
  }
}
```

При запуске с `--network` добавьте заголовок:

```json
"headers": { "X-Router-Token": "ДЛИННЫЙ-СЕКРЕТ" }
```

## Быстрый старт: весь сценарий через агента

Роутер сделан для агентского режима, поэтому и установка описывается
обычными словами — шаги ниже достаточно проговорить агенту.

1. **Установить и запустить.** «Установи и запусти программу из репозитория
   [sergeyfedyakov/rsv-data-router](https://github.com/sergeyfedyakov/rsv-data-router)» —
   агент склонирует репозиторий, поставит пакет (`pip install -e .`) и
   запустит роутер (см. «Требования и установка» и «Запуск» выше).
2. **Подключить MCP-сервер.** «Подключи MCP-сервер rsvdata в текущем
   воркспейсе по инструкции» — блок `mcpServers` из раздела «Подключение
   ИИ-клиента» выше; после правки конфига пересоздайте MCP-сессию.
3. **Добавить базу и поставить расширение.** «Добавь базу <имя> в роутер и
   установи расширение» — предполагается запущенная EDT с открытым проектом
   базы: работая в каталоге воркспейса, агент сам зарегистрирует цель по cwd
   (для этого варианта параметры можно не указывать вовсе — cwd подставляется
   из текущего каталога агента) и запустит установку расширения
   (`install=true`); прогресс — `job_status`.

Ключ базы, если она требует авторизации, агент попросит и сохранит в keyring
командой `set-secret` — в чат он не попадёт. Подробное описание всех команд —
в [docs/tools-router.md](docs/tools-router.md).

## Документация

- [docs/tools-router.md](docs/tools-router.md) — служебные команды роутера:
  регистрация целей, установка/удаление расширения, поведение без бинарника;
- [docs/tools-rsvdata.md](docs/tools-rsvdata.md) — инструменты данных
  RSVData: состав и маршрутизация base/cwd (подробная справка — в репо
  RSVData);
- [docs/development.md](docs/development.md) — окружение, тесты, сборка
  (wheel/exe, CI), формат `targets.json`.

## Лицензия

MIT — [LICENSE](LICENSE). Расширение RSVData публикуется отдельным проектом
([mcp-rsv-data](https://github.com/sergeyfedyakov/mcp-rsv-data), тоже MIT).
