"""Static web UI mount. The directory is resolved relative to the package (override: WEB_DIR env), and
nothing touches the disk until a request arrives (check_dir=False), so importing app.main stays I/O free."""
import os
import posixpath
from pathlib import Path

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles

HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
EXTRA = {**HEADERS, "Cache-Control": "no-cache"}


def web_dir() -> Path:
    return Path(os.environ.get("WEB_DIR") or Path(__file__).resolve().parent.parent / "web")


class SecureStaticFiles(StaticFiles):
    """Serves only index.html, css/** and js/**; every response (errors too) gets the security headers."""

    async def check_config(self):  # a missing directory just yields 404s
        pass

    async def get_response(self, path, scope):
        p = posixpath.normpath(path)
        if not (p in (".", "index.html") or p.startswith(("css/", "js/"))):
            raise HTTPException(404)
        return await super().get_response(path, scope)

    async def __call__(self, scope, receive, send):
        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                have = {k.lower() for k, _ in message["headers"]}
                message["headers"] = list(message["headers"]) + [
                    (k.lower().encode(), v.encode()) for k, v in EXTRA.items() if k.lower().encode() not in have]
            await send(message)

        try:
            await super().__call__(scope, receive, send_with_headers)
        except HTTPException as e:  # raised by StaticFiles: build the response here so it carries the headers
            await JSONResponse({"detail": e.detail}, e.status_code, headers=EXTRA)(scope, receive, send)


def static_app() -> SecureStaticFiles:
    return SecureStaticFiles(directory=web_dir(), html=True, check_dir=False)
