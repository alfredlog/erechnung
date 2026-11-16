"""FastAPI entry point: public tools, accounts, and the workspace of logged-in companies."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api import auth, legal, public, workspace
from app.api.deps import WEB_DIR, LoginRequired
from app.core.config import settings
from app.core.db import init_db


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
app.include_router(public.router)
app.include_router(auth.router)
app.include_router(workspace.router)
app.include_router(legal.router)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():  # browsers ask for it at the root
    return FileResponse(WEB_DIR / "static" / "favicon.ico", media_type="image/x-icon")


@app.exception_handler(LoginRequired)
def _login_required(request: Request, _exc: LoginRequired):
    return RedirectResponse(f"/anmelden?next={request.url.path}", 303)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "kosit_configured": bool(settings.kosit_url), "llm_configured": settings.llm_enabled}
