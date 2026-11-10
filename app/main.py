import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router as api_router
from app.backup.api import router as backup_router
from app.core.config import Settings
from app.collect.poller import LazyTracker, Poller, seq_lookup
from app.backup.scheduler import BackupScheduler
from app.collect.serverclient import ServerClient
from app.api.static import static_app

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, *, start_background: bool = True,
               client: ServerClient | None = None) -> FastAPI:
    """`client` lets tests inject a fake-backed ServerClient; the lifespan closes whichever client the app
    holds. Everything is wired from it at creation: replacing app.state.client afterwards is unsupported."""
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = ([asyncio.create_task(app.state.poller.run()),
                  asyncio.create_task(app.state.scheduler.run())] if start_background else [])
        try:
            yield
        finally:
            for task in tasks:  # a snapshot already running in a worker thread finishes in the background
                task.cancel()
            try:
                for task in tasks:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    except Exception:  # a task that died earlier must not skip the cleanup below
                        log.exception("background task failed")
            finally:
                try:
                    app.state.tracker.close()
                finally:
                    await app.state.client.aclose()

    app = FastAPI(title="dublin-bus-analytics", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(api_router)
    # constructors do no I/O (LazyTracker opens the DB on first ingest), so importing this module is safe
    app.state.client = client or ServerClient(settings.server_url)
    app.state.tracker = LazyTracker(settings.data_dir, seq_lookup)
    app.state.poller = Poller(settings, app.state.client, app.state.tracker)
    app.state.scheduler = BackupScheduler(settings)

    app.include_router(backup_router)

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    app.mount("/", static_app(), name="web")  # last: a mount at / would shadow routes added after it
    return app


app = create_app()
