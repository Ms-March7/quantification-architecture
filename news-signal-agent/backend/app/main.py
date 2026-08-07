from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.config import load_local_env
from app.scheduler import start_scheduler

APP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = APP_ROOT.parents[1]
load_local_env(PROJECT_ROOT)


def create_app() -> FastAPI:
    app = FastAPI(
        title="News Signal Agent",
        description="Auditable watchlist-driven news signal workflow.",
        version="0.1.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router, prefix="/api")
    app.state.workflow_scheduler = start_scheduler(PROJECT_ROOT)

    @app.get("/")
    def web_index() -> FileResponse:
        return FileResponse(APP_ROOT / "web" / "index.html")

    @app.get("/app")
    def web_app() -> FileResponse:
        return FileResponse(APP_ROOT / "web" / "index.html")

    return app


app = create_app()
