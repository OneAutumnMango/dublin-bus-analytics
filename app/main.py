from fastapi import FastAPI

from app.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="dublin-bus-analytics")
    app.state.settings = settings or Settings.from_env()

    @app.get("/api/health")
    def health():
        return {"ok": True}

    return app


app = create_app()
