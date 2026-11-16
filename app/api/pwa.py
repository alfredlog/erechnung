"""Installable app (PWA): manifest, service worker, offline page, install help, Android share target."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.api.deps import WRITERS, optional_user, page, read_upload
from app.core.config import settings
from app.core.db import get_db
from app.core.tables import Organization, User
from app.services import agent, jobs

router = APIRouter()
SW_VERSION = "1"  # raise to make installed apps drop their cache

SERVICE_WORKER = """
const CACHE = "era-%(version)s";
const OFFLINE = "/offline";
const PRECACHE = [OFFLINE, "/static/logo-mark.svg", "/static/htmx.min.js", "/static/icon-192.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(PRECACHE)));
  self.skipWaiting();
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});
// Always the network first (invoices must be current); the cache only helps without a connection.
// Pages with invoice data are never stored on the device.
self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  if (req.mode === "navigate") {
    e.respondWith(fetch(req).catch(() => caches.match(OFFLINE)));
    return;
  }
  const url = new URL(req.url);
  if (url.origin === location.origin && url.pathname.startsWith("/static/")) {
    e.respondWith(fetch(req).then((res) => {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(req, copy));
      return res;
    }).catch(() => caches.match(req)));
  }
});
"""


@router.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return JSONResponse({
        "name": settings.app_name, "short_name": "E-Rechnung", "id": "/", "lang": "de",
        "description": "E-Rechnungen empfangen, prüfen, schreiben und an DATEV übergeben.",
        "start_url": "/eingang?app=1", "scope": "/", "display": "standalone",
        "background_color": "#f5f6f8", "theme_color": "#16202E",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/static/icon-maskable-192.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
            {"src": "/static/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
        "shortcuts": [
            {"name": "Eingang", "url": "/eingang", "icons": [{"src": "/static/icon-192.png", "sizes": "192x192"}]},
            {"name": "Rechnung schreiben", "url": "/rechnungen/neu",
             "icons": [{"src": "/static/icon-192.png", "sizes": "192x192"}]},
        ],
        # Android: the app appears in "Teilen" – share a PDF from WhatsApp or Mail and it lands in the inbox
        "share_target": {"action": "/teilen", "method": "POST", "enctype": "multipart/form-data",
                         "params": {"files": [{"name": "files", "accept": [
                             "application/pdf", "application/xml", "text/xml", "image/*", ".pdf", ".xml"]}]}},
    }, media_type="application/manifest+json")


@router.get("/sw.js", include_in_schema=False)
def service_worker():
    return Response(SERVICE_WORKER % {"version": SW_VERSION}, media_type="text/javascript",
                    headers={"Cache-Control": "no-cache"})


@router.get("/offline", response_class=HTMLResponse, include_in_schema=False)
def offline(request: Request):
    return page(request, "offline.html", None)


@router.get("/app", response_class=HTMLResponse)
def install_help(request: Request, user=Depends(optional_user)):
    return page(request, "app.html", user, nav="app")


@router.post("/teilen")
async def share(files: list[UploadFile], user: User | None = Depends(optional_user), db: Session = Depends(get_db)):
    if user is None:
        return RedirectResponse("/anmelden?m=share_login", 303)
    if user.role not in WRITERS:
        return RedirectResponse("/eingang?m=read_only", 303)
    org = db.get(Organization, user.organization_id)
    new = 0
    for file in files[:20]:
        data = await read_upload(file)
        _row, is_new = agent.receive(db, org, file.filename or "geteilt", data, source="upload", user_id=user.id)
        new += is_new
    jobs.run_pending_inline(db)
    return RedirectResponse("/eingang?m=" + ("uploaded" if new else "duplicate"), 303)
