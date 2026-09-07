"""rsv-data-router entry point: CLI arguments + FastMCP application.

Transports: http (stateless POST /mcp, mirrors the RSVData upstream service)
and stdio (bonus of FastMCP, handy for local experiments). Stage 1: a single
--upstream target and one passthrough tool; stage 2 replaces the single
upstream with the targets.json map.
"""

import argparse
import os
import sys

import uvicorn
from fastmcp import FastMCP

from . import __version__, paths, tools
from .forward import now_iso

SERVER_NAME = "MCP:RSV Data Router"
SERVER_VERSION = __version__
DEFAULT_PORT = 8780
ENDPOINT_PATH = "/mcp"
BIN_PATH = "/bin/rsvdata.cfe"
ROUTER_TOKEN_HEADER = b"x-router-token"


def log(message):
    print(f"{now_iso()} {message}", file=sys.stderr, flush=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="rsv-data-router",
        description="MCP-роутер RSVData: одна точка входа на несколько баз 1С.",
    )
    parser.add_argument("--transport", choices=("http", "stdio"), default="http",
                        help="транспорт сервера (по умолчанию http)")
    parser.add_argument("--host", default=None,
                        help="адрес прослушивания (по умолчанию 127.0.0.1; при --network — 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"порт http-транспорта (по умолчанию {DEFAULT_PORT})")
    parser.add_argument("--network", action="store_true",
                        help="слушать во всей сети, а не только localhost (обязателен --token)")
    parser.add_argument("--token", default=None,
                        help="секрет: клиенты обязаны передавать заголовок X-Router-Token "
                             "(http-транспорт)")
    parser.add_argument("--home", default=None,
                        help="каталог рантайма: и targets.json, и журнал в одном каталоге "
                             "(по умолчанию targets.json — в каталоге конфигурации "
                             "пользователя, журнал — в каталоге логов; env RSV_DATA_ROUTER_HOME)")
    parser.add_argument("--rsvdatabinary", default=None, metavar="PATH",
                        help="путь к бинарнику расширения RSVData (.cfe): get_rsvdata отдаёт "
                             f"его агентам, http-транспорт раздаёт по GET {BIN_PATH}")
    parser.add_argument("--rsvmcp", default="http://127.0.0.1:8770/mcp", metavar="URL",
                        help="адрес MCP:RSV (HTTP-сервер в EDT) — через него register_target "
                             "(install=true) ставит расширение, пока EDT открыт; пустая "
                             "строка отключает вариант EDT")
    args = parser.parse_args(argv)

    if args.host is None:
        args.host = "0.0.0.0" if args.network else "127.0.0.1"
    if args.network and not args.token:
        parser.error("--network открывает роутер в сеть: обязателен --token")
    # Explicit home (flag or env) keeps everything in one directory; the
    # default splits it: targets.json in the user config dir, log in the
    # user log dir (platformdirs).
    if args.home is None:
        args.home = os.environ.get("RSV_DATA_ROUTER_HOME")
    if args.home is None:
        args.home = paths.default_home()
        args.log_dir = paths.default_log_dir()
    else:
        args.log_dir = args.home
    return args


class TokenGate:
    """Pure ASGI wrapper: rejects http requests without the router token.

    Non-http scopes (lifespan etc.) pass through untouched.
    """

    def __init__(self, app, token):
        self.app = app
        self.token = token.encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            if headers.get(ROUTER_TOKEN_HEADER) != self.token:
                body = b"missing or wrong X-Router-Token"
                await send({"type": "http.response.start", "status": 401, "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii"))]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


class BinaryRoute:
    """Pure ASGI dispatcher: GET BIN_PATH serves the RSVData binary, everything
    else goes to the MCP app. Mounted inside TokenGate, so the download is
    token-protected in --network mode too.
    """

    def __init__(self, app, rsvdata_path):
        self.app = app
        self.rsvdata_path = rsvdata_path

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == BIN_PATH:
            await self.serve(scope, receive, send)
            return
        await self.app(scope, receive, send)

    async def serve(self, scope, receive, send):
        async def respond(status, body, content_type=b"text/plain; charset=utf-8",
                          extra_headers=()):
            await send({"type": "http.response.start", "status": status, "headers": [
                (b"content-type", content_type),
                (b"content-length", str(len(body)).encode("ascii")),
                *extra_headers]})
            await send({"type": "http.response.body", "body": body})

        if scope.get("method") != "GET":
            await respond(405, "Только GET".encode("utf-8"))
            return
        if not self.rsvdata_path:
            await respond(404, "Бинарник RSVData роутеру не выдан — перезапустите его "
                               "с параметром --rsvdatabinary <путь к .cfe>".encode("utf-8"))
            return
        try:
            with open(self.rsvdata_path, "rb") as handle:
                body = handle.read()
        except OSError as exc:
            await respond(404, f"Файл бинарника недоступен ({self.rsvdata_path}): "
                               f"{exc}".encode("utf-8"))
            return
        await respond(200, body, content_type=b"application/octet-stream", extra_headers=[
            (b"content-disposition", b'attachment; filename="rsvdata.cfe"')])


def build_app(args):
    """Returns (app, ctx): main() logs the startup line via ctx."""
    app = FastMCP(name=SERVER_NAME, version=SERVER_VERSION)
    ctx = tools.RouterContext(home_dir=args.home, log_dir=args.log_dir)
    ctx.rsvdata_path = args.rsvdatabinary
    ctx.serve_binary = args.transport == "http"
    ctx.rsv_mcp = args.rsvmcp or ""
    tools.register_tools(app, ctx)
    if args.rsvdatabinary and not os.path.isfile(args.rsvdatabinary):
        log(f"ВНИМАНИЕ: --rsvdatabinary указан, но файла нет: {args.rsvdatabinary}")
    return app, ctx


def main(argv=None):
    args = parse_args(argv)
    app, ctx = build_app(args)

    if args.transport == "stdio":
        ctx.log.write(f"router {SERVER_VERSION} запущен (stdio), home: {args.home}")
        app.run(transport="stdio")
        return

    # Stateless plain-JSON endpoint: same behavior as the 1C upstream service.
    # Host-origin protection is off — the token gate is our auth layer.
    # BinaryRoute serves GET /bin/rsvdata.cfe outside the /mcp branch.
    http_app = app.http_app(path=ENDPOINT_PATH, json_response=True, stateless_http=True,
                            host_origin_protection=False)
    http_app = BinaryRoute(http_app, ctx.rsvdata_path)
    if args.token:
        http_app = TokenGate(http_app, args.token)
    log(f"{SERVER_NAME} v{SERVER_VERSION}: http://{args.host}:{args.port}{ENDPOINT_PATH}")
    log(f"home: {args.home}")
    if ctx.rsvdata_path:
        log(f"бинарник RSVData: {ctx.rsvdata_path} -> GET {BIN_PATH}")
    ctx.log.write(f"router {SERVER_VERSION} запущен: http://{args.host}:{args.port}{ENDPOINT_PATH}, "
                  f"home: {args.home}, цели: {', '.join(ctx.store.aliases()) or '—'}")
    uvicorn.run(http_app, host=args.host, port=args.port, log_level="warning", lifespan="on")


if __name__ == "__main__":
    main()
