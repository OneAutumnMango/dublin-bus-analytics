import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import router as api_router
from app.backup_api import router as backup_router
from app.config import Settings
from app.poller import LazyTracker, Poller, SeqResolver
from app.serverclient import ServerClient


def create_app(settings: Settings | None = None, *, start_background: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(app.state.poller.run()) if start_background else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            app.state.tracker.close()
            await app.state.client.aclose()

    app = FastAPI(title="dublin-bus-analytics", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(api_router)
    # constructors do no I/O (LazyTracker opens the DB on first ingest), so importing this module is safe
    app.state.client = ServerClient(settings.server_url)
    app.state.tracker = LazyTracker(settings.data_dir, SeqResolver(app.state.client))
    app.state.poller = Poller(settings, app.state.client, app.state.tracker)

    app.include_router(backup_router)

    @app.get("/api/health")
    def health():
        return {"ok": True}

    return app


app = create_app()
