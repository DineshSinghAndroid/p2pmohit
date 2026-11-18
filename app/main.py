import asyncio
import csv
import io
import json
import os
import secrets
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl, model_validator

from .database import Database
from .logic import calculate_stats
from .worker import run_browser

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB = Database(os.getenv("DB_PATH", str(DATA_DIR / "payout_test.sqlite3")))
try:  # a run left "running" by a previous server process has no worker anymore
    from datetime import datetime, timezone
    for _r in DB.list_runs() if hasattr(DB, "list_runs") else []:
        if _r.get("status") == "running":
            DB.update_run(_r["id"], "stopped", datetime.now(timezone.utc).isoformat(), "Server restarted")
except Exception:
    pass
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin")
SESSIONS: set[str] = set()
RUN_EVENTS: dict[str, threading.Event] = {}
RUN_LOCK = threading.Lock()
app = FastAPI(title="Payout Click Tester")
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")


class LoginRequest(BaseModel):
    username: str
    password: str


class StartRequest(BaseModel):
    site_url: HttpUrl = Field(default="https://welcome.p2p-expert-traders.com", validate_default=True)
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1000)
    min_amount: str | None = None
    max_amount: str | None = None
    max_clicks: int | None = Field(default=None, ge=1, le=10000)
    delay_ms: int = Field(default=500, ge=0, le=60000)
    clicks_per_minute: int | None = Field(default=None, ge=1, le=600)
    proxy: str | None = None
    stealth_mode: bool = True
    force: bool = False

    @model_validator(mode="after")
    def amount_bounds(self):
        for value in (self.min_amount, self.max_amount):
            if value is not None:
                try:
                    amount = Decimal(value)
                    if not amount.is_finite():
                        raise ValueError("Amounts must be finite decimal numbers")
                    if amount < 0:
                        raise ValueError("Amounts must be non-negative")
                except InvalidOperation as exc:
                    raise ValueError("Amounts must be valid decimal numbers") from exc
        if self.min_amount is not None and self.max_amount is not None:
            if Decimal(self.min_amount) > Decimal(self.max_amount):
                raise ValueError("Minimum amount cannot exceed maximum amount")
        if self.site_url.username or self.site_url.password:
            raise ValueError("Site URL must not contain embedded credentials")
        return self


def _authenticated(request: Request):
    token = request.cookies.get("payout_session", "")
    if not token or token not in SESSIONS:
        raise HTTPException(status_code=401, detail="Authentication required")


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, exc: RequestValidationError):
    return JSONResponse(
        content={"detail": [
            {"loc": error.get("loc", []), "msg": error.get("msg", "Invalid input"), "type": error.get("type", "value_error")}
            for error in exc.errors()
        ]},
        status_code=422,
    )


@app.get("/", response_class=HTMLResponse)
def index():
    return (BASE_DIR / "app" / "static" / "index.html").read_text()


@app.get("/sw.js")
def service_worker():
    sw_file = BASE_DIR / "app" / "static" / "sw.js"
    return FileResponse(sw_file, media_type="application/javascript", headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@app.post("/api/login")
def login(payload: LoginRequest, request: Request, response: Response):
    if not ADMIN_PASS:
        raise HTTPException(status_code=503, detail="Dashboard password is not configured")
    if not (secrets.compare_digest(payload.username.encode(), ADMIN_USER.encode()) and secrets.compare_digest(payload.password.encode(), ADMIN_PASS.encode())):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = secrets.token_urlsafe(32)
    SESSIONS.add(token)
    forwarded_https = request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
    response.set_cookie("payout_session", token, httponly=True,
                        secure=request.url.scheme == "https" or forwarded_https,
                        samesite="strict", max_age=8 * 60 * 60)
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get("payout_session", "")
    SESSIONS.discard(token)
    response.delete_cookie("payout_session")
    return {"ok": True}


SETTINGS_FILE = DATA_DIR / "saved_settings.json"


@app.get("/api/settings")
def get_settings(request: Request, response: Response):
    _authenticated(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}


@app.put("/api/settings")
async def put_settings(request: Request):
    _authenticated(request)
    body = await request.json()
    keep = {}
    try:
        if SETTINGS_FILE.exists():
            keep = json.loads(SETTINGS_FILE.read_text())
    except Exception:
        pass
    for k in ("site_url", "email", "password", "min_amount", "max_amount", "proxy"):
        if k in body:
            keep[k] = str(body[k])
    if "stealth_mode" in body:
        keep["stealth_mode"] = bool(body["stealth_mode"])
    if "saved_accounts" in body and isinstance(body["saved_accounts"], list):
        keep["saved_accounts"] = [
            {"email": str(a.get("email", "")).strip(), "password": str(a.get("password", "")).strip()}
            for a in body["saved_accounts"] if isinstance(a, dict) and str(a.get("email", "")).strip()
        ]
    SETTINGS_FILE.write_text(json.dumps(keep))
    try:
        SETTINGS_FILE.chmod(0o600)
    except Exception:
        pass
    return {"saved": True}


@app.get("/api/me")
def me(request: Request):
    _authenticated(request)
    return {"authenticated": True}


@app.post("/api/runs")
async def start_run(payload: StartRequest, request: Request):
    _authenticated(request)
    run_id = secrets.token_hex(8)
    now = datetime.now(timezone.utc).isoformat()
    config = payload.model_dump()
    site = payload.site_url
    port = site.port
    default_port = port == (443 if site.scheme == "https" else 80)
    authority = site.host if not port or default_port else f"{site.host}:{port}"
    config["site_url"] = f"{site.scheme}://{authority}"
    run_info = {
        "id": run_id, "status": "running", "site_url": config["site_url"],
        "min_amount": config["min_amount"], "max_amount": config["max_amount"],
        "max_clicks": config["max_clicks"], "delay_ms": config["delay_ms"],
        "clicks_per_minute": config["clicks_per_minute"], "login_status": "pending", "started_at": now,
    }
    with RUN_LOCK:
        if payload.force:
            for r in DB.list_runs(limit=50):
                if r["status"] == "running":
                    ev = RUN_EVENTS.get(r["id"])
                    if ev:
                        ev.set()
                    DB.update_run(r["id"], "stopped", datetime.now(timezone.utc).isoformat(), "Force-stopped for new run")
                    RUN_EVENTS.pop(r["id"], None)

        if DB.has_active_run():
            raise HTTPException(status_code=409, detail="A browser run is already active")
        DB.create_run(run_info)
        stop_event = threading.Event()
        RUN_EVENTS[run_id] = stop_event
        asyncio.create_task(asyncio.to_thread(run_browser, run_id, config, stop_event, DB, str(DATA_DIR / "screenshots")))
    return {"id": run_id, "status": "running"}


@app.post("/api/runs/active/stop")
def stop_active_runs(request: Request):
    _authenticated(request)
    stopped_count = 0
    with RUN_LOCK:
        for r in DB.list_runs(limit=50):
            if r["status"] == "running":
                event = RUN_EVENTS.get(r["id"])
                if event:
                    event.set()
                DB.update_run(r["id"], "stopped", datetime.now(timezone.utc).isoformat(), "Stopped by user")
                RUN_EVENTS.pop(r["id"], None)
                stopped_count += 1
    return {"ok": True, "stopped": stopped_count}


@app.post("/api/runs/{run_id}/stop")
def stop_run(run_id: str, request: Request):
    _authenticated(request)
    run = DB.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    event = RUN_EVENTS.get(run_id)
    if event:
        event.set()
    elif run.get("status") == "running":
        DB.update_run(run_id, "stopped", datetime.now(timezone.utc).isoformat(), "Stopped (no active worker)")
    return {"id": run_id, "status": "stopping"}


@app.get("/api/runs")
def list_runs(request: Request):
    _authenticated(request)
    return DB.list_runs()


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, request: Request):
    _authenticated(request)
    run = DB.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    clicks = DB.list_clicks(run_id)
    return {"run": run, "clicks": clicks, "stats": calculate_stats(clicks)}


@app.get("/api/runs/{run_id}/export.csv")
def export_run(run_id: str, request: Request):
    _authenticated(request)
    run = DB.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["row_id", "amount", "timestamp", "popup_shown", "outcome", "latency_ms", "error_text"])
    writer.writeheader()
    for click in DB.list_clicks(run_id):
        writer.writerow({key: click.get(key) for key in writer.fieldnames})
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="payout-run-{run_id}.csv"'})
