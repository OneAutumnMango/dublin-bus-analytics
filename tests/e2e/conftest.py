"""Browser tests: seeded demo data, a fake bus server and a real uvicorn, driven by Playwright.

Everything here is marked `e2e` and left out of the default run (see pyproject addopts)."""
import importlib.util
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ROUTES = ["15", "46A"]
DAYS = 5

_spec = importlib.util.spec_from_file_location("seed_demo", ROOT / "scripts" / "seed_demo.py")
seed_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_demo)


def pytest_collection_modifyitems(items):
    for item in items:
        if Path(item.fspath).is_relative_to(Path(__file__).parent):
            item.add_marker(pytest.mark.e2e)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def seed_end() -> date:
    return date.today() - timedelta(days=1)


@pytest.fixture(scope="session")
def base_url(tmp_path_factory, seed_end):
    data = tmp_path_factory.mktemp("e2e-data")
    seed_demo.seed(data, ROUTES, DAYS, 1, seed_end)
    fake_port, port = _free_port(), _free_port()
    fake = seed_demo.fake_server(fake_port, ROUTES)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    env = {**os.environ, "SERVER_URL": f"http://127.0.0.1:{fake_port}", "DATA_DIR": str(data), "BACKUP_ENABLED": "false"}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"], cwd=ROOT, env=env)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(url + "/", timeout=1)
                break
            except OSError:
                if proc.poll() is not None:
                    raise RuntimeError(f"uvicorn exited with {proc.returncode}") from None
                time.sleep(0.2)
        else:
            raise RuntimeError("uvicorn did not start within 20 s")
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        fake.shutdown()


@pytest.fixture
def app(page, base_url):
    """A page that blocks every non-local request and fails the test on console errors, page errors or failed local requests."""
    problems = []
    # the routes page probes /api/backup/download and treats 404 as "no backup yet", so that one is expected
    local = lambda u: u.startswith(base_url) and "/api/backup/download" not in u  # noqa: E731

    def gate(route):
        route.continue_() if local(route.request.url) else route.abort()

    page.context.route("**/*", gate)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.on("console", lambda m: m.type == "error" and local(m.location.get("url") or base_url) and problems.append(f"console: {m.text}"))
    page.on("response", lambda r: local(r.url) and r.status >= 400 and problems.append(f"{r.status} {r.url}"))
    page.on("requestfailed", lambda r: local(r.url) and "ERR_ABORTED" not in (r.failure or "") and problems.append(f"failed {r.url}"))  # aborted = left the page mid-request
    page.goto(base_url + "/")
    yield page
    assert not problems, "\n".join(problems)
