"""Promo Finder web app: FastAPI API + the static page in web/. One process, made for one Render instance.

    uvicorn server:app --host 0.0.0.0 --port 8000        (locally: http://localhost:8000)

Access policy (the URL is public):
  * offline engine (heuristic baseline, no API): always allowed.
  * server keys (SERVER_KEYS below, the same models as the CLI): the sample show only, which replays from a
    pre-recorded cache; uploads on the server's keys need DEMO_PASSCODE (unset = locked).
  * a visitor's own key + any LiteLLM model string: allowed. The key lives in memory for that run only.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")  # do not fetch the price map at import

from dotenv import load_dotenv  # noqa: E402
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from src.webrun import DOWNLOADS, ROOT, RunManager  # noqa: E402

load_dotenv(ROOT / ".env")

SAMPLE_DIR = ROOT / "sample_data" / "demo_show"
MAX_FILES, MAX_FILE_BYTES = 6, 300_000
RUNS_PER_HOUR = int(os.environ.get("RUNS_PER_HOUR", "8"))
MAX_ACTIVE = int(os.environ.get("MAX_ACTIVE_RUNS", "4"))
PRESETS = [
    {"id": "gemini/gemini-flash-lite-latest", "label": "Gemini Flash-Lite (fast, free tier)", "provider": "gemini"},
    {"id": "gemini/gemini-3.8-flash", "label": "Gemini 3.8 Flash", "provider": "gemini"},
    {"id": "groq/openai/gpt-oss-120b", "label": "Groq gpt-oss-120b", "provider": "groq"},
    {"id": "openai/gpt-6-luna", "label": "OpenAI gpt-6-luna", "provider": "openai"},
    {"id": "anthropic/claude-sonnet-5-5", "label": "Anthropic Claude Sonnet 5.5", "provider": "anthropic"},
]

manager = RunManager(os.environ.get("PROMO_WORK_DIR", "/tmp/promo" if os.name != "nt" else ROOT / ".web"),
                     workers=int(os.environ.get("RUN_WORKERS", "2")),
                     seed_cache=ROOT / "sample_data" / "llm_cache", overlay=ROOT / "config.cloud.yaml",
                     recorded=ROOT / "sample_data" / "recorded_run.json")
app = FastAPI(title="Promo Finder", docs_url="/api/docs", openapi_url="/api/openapi.json")
_hits: dict[str, deque] = defaultdict(deque)
_hits_lock = threading.Lock()


# Keys the first model of each route in config.yaml needs (Gemini: context + mine, OpenAI: judge) plus the Groq
# fallback. A missing key is an auth error, not a fallback, so the server engine stays off until all are set.
# GEMINI_API_KEY_1 (second Gemini key) is optional: it is only tried after GEMINI_API_KEY hits its daily cap.
SERVER_KEYS = ("GEMINI_API_KEY", "OPEN_API_KEY", "GROQ_API_KEY")


def server_keys() -> bool:
    return all(os.environ.get(k) for k in SERVER_KEYS)


def passcode_ok(code: str | None) -> bool:
    want = os.environ.get("DEMO_PASSCODE")
    return bool(want and code and secrets.compare_digest(want, code))


def rate_limit(request: Request) -> None:
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0].strip()
    now = time.time()
    with _hits_lock:
        q = _hits[ip]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= RUNS_PER_HOUR:
            raise HTTPException(429, f"limit of {RUNS_PER_HOUR} API-backed runs per hour reached; try the offline engine")
        q.append(now)


@app.get("/api/health")
def health():
    return {"ok": True, "active_runs": manager.busy()}


@app.get("/api/config")
def config():
    sample = sorted(p.name for p in SAMPLE_DIR.glob("episode-*.txt"))
    title = (SAMPLE_DIR / "title.txt").read_text(encoding="utf-8").strip() if (SAMPLE_DIR / "title.txt").exists() else ""
    return {"presets": PRESETS, "server_keys": server_keys(), "passcode_set": bool(os.environ.get("DEMO_PASSCODE")),
            "sample": {"title": title, "episodes": sample}, "limits": {"max_files": MAX_FILES,
            "max_file_kb": MAX_FILE_BYTES // 1000, "runs_per_hour": RUNS_PER_HOUR}}


@app.post("/api/runs")
async def create_run(request: Request, settings: str = Form(...), files: list[UploadFile] = File(default=[])):
    try:
        s = json.loads(settings)
    except json.JSONDecodeError:
        raise HTTPException(400, "settings must be JSON")
    engine, source = s.get("engine", "cloud"), s.get("source", "sample")
    code = s.pop("passcode", None)
    api_key = (s.pop("api_key", None) or "").strip()

    if source == "sample":
        payload = {p.name: p.read_bytes() for p in SAMPLE_DIR.glob("episode-*.txt")}
        title = (SAMPLE_DIR / "title.txt").read_text(encoding="utf-8").strip()
    else:
        if not files:
            raise HTTPException(400, "upload at least one .txt screenplay, or use the sample show")
        if len(files) > MAX_FILES:
            raise HTTPException(400, f"at most {MAX_FILES} episodes per run in the hosted demo (use run.py for a full show)")
        payload = {}
        for f in files:
            name = Path(f.filename or "").name
            if not name.lower().endswith(".txt"):
                raise HTTPException(400, f"{name or 'file'}: only .txt screenplays are accepted")
            data = await f.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise HTTPException(400, f"{name}: larger than {MAX_FILE_BYTES // 1000} KB")
            payload[name] = data
        title = (s.get("title") or "").strip()[:120] or None

    api_keys: dict[str, str] = {}
    if engine == "offline":
        model = "offline"
    elif engine == "cloud":
        model = None
        if not server_keys():
            raise HTTPException(400, "this server has no API keys configured: use the offline engine or your own key")
        if source != "sample" and not passcode_ok(code):
            raise HTTPException(403, "uploads on the server's keys need the demo passcode; or paste your own API key")
    elif engine == "custom":
        model = (s.get("model") or "").strip()
        if not model or "/" not in model:
            raise HTTPException(400, "enter a LiteLLM model string such as gemini/gemini-flash-lite-latest")
        if api_key:
            api_keys[model.split("/", 1)[0]] = api_key
        elif not passcode_ok(code):
            raise HTTPException(403, "paste an API key for this provider (or the demo passcode to use the server's keys)")
    else:
        raise HTTPException(400, f"unknown engine {engine!r}")
    if engine != "offline":
        rate_limit(request)
    if manager.busy() >= MAX_ACTIVE:
        raise HTTPException(503, "the demo is busy with other runs; try again in a minute")

    settings_clean = {"engine": engine, "source": source, "model": model, "fast": bool(s.get("fast", True)),
                      "duels": bool(s.get("duels", False)), "repair_rounds": max(0, min(2, int(s.get("repair_rounds", 1)))),
                      "episodes": sorted(payload)}
    try:
        h = manager.create(payload, title, settings_clean, api_keys)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"run_id": h.id}


def _run(run_id: str):
    h = manager.get(run_id)
    if not h:
        raise HTTPException(404, "run not found: the server may have restarted (free instances sleep after 15 min "
                                 "idle and lose their disk). Start a new run.")
    return h


@app.get("/api/runs/{run_id}")
def run_status(run_id: str):
    return _run(run_id).snapshot()


@app.post("/api/runs/{run_id}/resume")
async def run_resume(run_id: str, request: Request):
    h = _run(run_id)
    body = await request.json()
    decisions = body.get("decisions", []) if isinstance(body, dict) else body
    if not isinstance(decisions, list):
        raise HTTPException(400, "decisions must be a list")
    try:
        manager.resume(h, decisions)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.get("/api/runs/{run_id}/quality")
def run_quality(run_id: str):
    return manager.quality(_run(run_id))


@app.get("/api/runs/{run_id}/files/{name}")
def run_file(run_id: str, name: str):
    h = _run(run_id)
    if name not in DOWNLOADS or not (h.out_dir / name).exists():
        raise HTTPException(404, "no such output (yet)")
    return FileResponse(h.out_dir / name, media_type=DOWNLOADS[name], filename=f"{run_id}-{name}")


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
