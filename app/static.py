"""Static web UI mount. The directory is resolved relative to the package (override: WEB_DIR env), and
nothing touches the disk until a request arrives (check_dir=False), so importing app.main stays I/O free."""
import os
from pathlib import Path

from starlette.staticfiles import StaticFiles

HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def web_dir() -> Path:
    return Path(os.environ.get("WEB_DIR") or Path(__file__).resolve().parent.parent / "web")


class SecureStaticFiles(StaticFiles):
    async def check_config(self):  # a missing directory just yields 404s
        pass

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers.update(HEADERS)
        return resp


def static_app() -> SecureStaticFiles:
    return SecureStaticFiles(directory=web_dir(), html=True, check_dir=False)
