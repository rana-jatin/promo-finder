"""Runs the promo graph for the web app (server.py): one background thread per run step, so HTTP requests only
read status. A run lives in <root>/runs/<id>/{show,out} with its own LLM adapter and SQLite checkpointer;
the editor interrupt() is a pause between two steps (start ... review, resume ... review|done).

Everything is in memory + local disk: a server restart drops live runs (the UI says so), which is the
accepted trade-off for a free single-instance host. Results are meant to be downloaded."""
from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from langgraph.types import Command

from .config import load_config
from .graph import prepare_run
from .io_utils import read_jsonl
from .llm import LLM, PROVIDER_HINTS, QuotaExhausted
from .parse import parse_show

ROOT = Path(__file__).resolve().parent.parent
STAGES = ["parse", "bible", "mine", "link", "assemble", "refine", "coverage", "select", "duel", "order", "write",
          "editor"]
# Every output a visitor may read or download, in pipeline-result order. parsed.json (the script itself) and
# runs.db (checkpoints) stay private. llm_calls.jsonl holds call metadata only, and keys are scrubbed from it.
DOWNLOADS = {"ranked.json": "application/json", "candidates.jsonl": "application/x-ndjson",
             "report.md": "text/markdown; charset=utf-8", "run_meta.json": "application/json",
             "decisions.jsonl": "application/x-ndjson", "bible.json": "application/json",
             "beats.jsonl": "application/x-ndjson", "pool.jsonl": "application/x-ndjson",
             "rejected_by_code.jsonl": "application/x-ndjson", "duels.jsonl": "application/x-ndjson",
             "features.json": "application/json", "llm_calls.jsonl": "application/x-ndjson"}


def recording_key(settings: dict) -> str:
    """Which recorded sample run matches these settings (tools/record_sample.py writes the same keys)."""
    return f"fast={bool(settings.get('fast'))}|duels={bool(settings.get('duels'))}|repair={settings.get('repair_rounds')}"


def _json(p: Path) -> Any:
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


class RunHandle:
    def __init__(self, run_id: str, root: Path, settings: dict, public: dict):
        self.id = run_id
        self.dir = root / "runs" / run_id
        self.show_dir, self.out_dir = self.dir / "show", self.dir / "out"
        self.settings, self.public = settings, public
        self.created = time.time()
        self.touched = self.created
        self.status = "queued"  # queued | running | review | done | error
        self.error: str | None = None
        self.review: dict | None = None
        self.started: Counter = Counter()
        self.finished: Counter = Counter()
        self.log: list[dict] = []
        self.step_started: float | None = None
        self.busy_s = 0.0
        self.lock = threading.Lock()
        self.app = self.ctx = self.config = self.llm = None
        self.recorded: dict | None = None  # sample replays: metrics of the run that recorded the cache

    def note(self, text: str) -> None:
        with self.lock:
            self.log.append({"t": round(time.time() - self.created, 1), "msg": text})
            del self.log[:-200]

    def snapshot(self) -> dict:
        with self.lock:
            stages = []
            for s in STAGES:
                st, fin = self.started.get(s, 0), self.finished.get(s, 0)
                state = "running" if st > fin else "done" if fin else "pending"
                if s == "editor" and self.status == "review":
                    state = "waiting"
                stages.append({"name": s, "state": state, "started": st, "finished": fin})
            busy = self.busy_s + (time.time() - self.step_started if self.step_started else 0)
            return {"id": self.id, "status": self.status, "error": self.error, "settings": self.public,
                    "models": self.llm.describe() if self.llm and not self.llm.offline else
                    (["offline heuristic baseline (no API)"] if self.llm else []),
                    "stages": stages, "log": list(self.log[-60:]), "elapsed_s": round(busy, 1),
                    "review": self.review, "files": [f for f in DOWNLOADS if (self.out_dir / f).exists()],
                    "llm": self.llm.snapshot()["total"] if self.llm else {}, "recorded": self.recorded}


class RunManager:
    def __init__(self, root: str | Path, workers: int = 2, ttl_s: float = 6 * 3600,
                 seed_cache: str | Path | None = None, overlay: str | Path | None = None,
                 recorded: str | Path | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.cache_dir = self.root / "llm_cache"
        if seed_cache and Path(seed_cache).is_dir():  # pre-recorded answers for the sample show: instant demo
            shutil.copytree(seed_cache, self.cache_dir, dirs_exist_ok=True)
        self.overlay = overlay
        self.recorded = _json(Path(recorded)) if recorded and Path(recorded).exists() else {}
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="run")
        self.runs: dict[str, RunHandle] = {}
        self.lock = threading.Lock()
        self.ttl_s = ttl_s

    # ---- lifecycle
    def create(self, files: dict[str, bytes], title: str | None, settings: dict, api_keys: dict[str, str]) -> RunHandle:
        self.cleanup()
        run_id = uuid.uuid4().hex[:12]
        public = {k: v for k, v in settings.items() if k != "api_key"}
        h = RunHandle(run_id, self.root, settings, public)
        if settings.get("source") == "sample" and settings.get("engine") == "cloud":
            h.recorded = (self.recorded.get("runs") or {}).get(recording_key(settings))
        h.show_dir.mkdir(parents=True)
        for name, data in files.items():
            (h.show_dir / name).write_bytes(data)
        if title:
            (h.show_dir / "title.txt").write_text(title, encoding="utf-8")
        try:  # fail fast on files that are not screenplays in the expected format
            show = parse_show(h.show_dir)
            if not any(ep.lines for ep in show.episodes.values()):
                raise ValueError("no timestamped dialogue lines found")
        except Exception as e:
            shutil.rmtree(h.dir, ignore_errors=True)
            raise ValueError(f"could not parse the screenplays: {e}") from e

        overrides = {"llm.cache_dir": str(self.cache_dir), "llm.model": settings.get("model"),
                     "score.max_repair_rounds": settings.get("repair_rounds")}
        if settings.get("model") and settings["model"] != "offline":
            overrides["llm.fallbacks"] = []
        cfg = load_config(None, overrides, overlay=self.overlay)
        h.llm = LLM(cfg, call_log=h.out_dir / "llm_calls.jsonl", api_keys=api_keys)
        h.app, h.config, h.ctx = prepare_run(h.show_dir, h.out_dir, cfg, h.llm, run_id,
                                             duels=bool(settings.get("duels")), review=True,
                                             fast=bool(settings.get("fast")))
        with self.lock:
            self.runs[run_id] = h
        h.note(f"queued: {len(files)} episode file(s), " + ", ".join(h.llm.describe() if not h.llm.offline
                                                                      else ["offline heuristic baseline"]))
        self.pool.submit(self._step, h, {})
        return h

    def get(self, run_id: str) -> RunHandle | None:
        with self.lock:
            h = self.runs.get(run_id)
        if h:
            h.touched = time.time()
        return h

    def resume(self, h: RunHandle, decisions: list[dict]) -> None:
        if h.status != "review":
            raise ValueError(f"run is {h.status}, not waiting for review")
        with h.lock:
            h.status, h.review = "queued", None
        acts = Counter(d.get("action", "approve") for d in decisions)
        h.note("editor decisions: " + (", ".join(f"{n} {a}" for a, n in acts.items()) or "approve all"))
        self.pool.submit(self._step, h, Command(resume=decisions))

    def busy(self) -> int:
        with self.lock:
            return sum(1 for h in self.runs.values() if h.status in ("queued", "running"))

    def cleanup(self) -> None:
        now = time.time()
        with self.lock:
            old = [k for k, h in self.runs.items() if now - h.touched > self.ttl_s and h.status not in ("queued", "running")]
            for k in old:
                shutil.rmtree(self.runs.pop(k).dir, ignore_errors=True)

    # ---- one graph step: start -> (review | done), or resume -> (review | done)
    def _step(self, h: RunHandle, inp: Any) -> None:
        with h.lock:
            h.status, h.step_started = "running", time.time()
        try:
            for mode, chunk in h.app.stream(inp, h.config, context=h.ctx, stream_mode=["tasks", "updates"]):
                if mode != "tasks":
                    continue
                name = chunk.get("name")
                if name not in STAGES:
                    continue
                with h.lock:
                    if "result" in chunk or "error" in chunk or "interrupts" in chunk:
                        h.finished[name] += 1
                    else:
                        h.started[name] += 1
                if chunk.get("interrupts"):
                    continue  # the editor pausing is reported once the step ends
                if "result" not in chunk:
                    continue
                if name not in ("mine", "refine"):
                    h.note(f"{name} done")
                elif h.finished[name] == h.started[name]:  # fan-out stages: one line when the last task ends
                    h.note(f"{name} done ({h.finished[name]} tasks)")
            snap = h.app.get_state(h.config)
            if snap.interrupts:
                payload = snap.interrupts[0].value
                with h.lock:
                    h.review, h.status = self._enrich(h, payload), "review"
                h.note(f"waiting for the editor (review round {payload.get('round', 0) + 1})")
            else:
                with h.lock:
                    h.status = "done"
                h.note("finished: ranked.json and report.md written")
        except QuotaExhausted as e:
            self._fail(h, f"Quota exhausted: {e}")
        except Exception as e:  # noqa: BLE001 - shown to the user, never re-raised in a worker thread
            kind = type(e).__name__
            msg = f"{kind}: {PROVIDER_HINTS[kind]}. " if kind in PROVIDER_HINTS else f"{kind}: "
            self._fail(h, msg + str(e)[:400])
        finally:
            with h.lock:
                if h.step_started:
                    h.busy_s += time.time() - h.step_started
                h.step_started = None

    def _fail(self, h: RunHandle, msg: str) -> None:
        msg = h.llm.scrub(msg) if h.llm else msg
        with h.lock:
            h.status, h.error = "error", msg
        h.note("stopped: " + msg[:200])

    @staticmethod
    def _enrich(h: RunHandle, payload: dict) -> dict:
        """Add what the review cards need from ranked.json (hook offset, score breakdown, trope)."""
        ranked = _json(h.out_dir / "ranked.json") or {}
        by = {p["cand_id"]: p for p in ranked.get("promos", [])}
        items = []
        for it in payload.get("shortlist", []):
            p = by.get(it["cand_id"], {})
            items.append({**it, "hook_at_s": (p.get("hook") or {}).get("at_s"), "scores": p.get("scores"),
                          "trope": p.get("trope"), "repairs": p.get("repairs", []),
                          "in_free_window": p.get("in_free_window"), "spoiler": p.get("spoiler")})
        try:
            held = h.app.get_state(h.config).values.get("held_back") or {}
        except Exception:  # noqa: BLE001 - display only
            held = {}
        return {"round": payload.get("round", 0), "shortlist": items,
                "reserve": len(ranked.get("reserve", [])), "held_back": held}

    # ---- quality tab
    @staticmethod
    def quality(h: RunHandle) -> dict:
        cands = read_jsonl(h.out_dir / "candidates.jsonl")
        code_rejects = read_jsonl(h.out_dir / "rejected_by_code.jsonl")
        pool = read_jsonl(h.out_dir / "pool.jsonl")
        meta = _json(h.out_dir / "run_meta.json") or {}
        repaired = [c for c in cands if c.get("rounds", 0) > 0]
        def clean(h0: dict) -> bool:  # threshold pass (v3); older runs only logged the label
            return h0["passed"] if "passed" in h0 else h0.get("main_failure") in ("none", None)
        first_pass = [c for c in cands if clean((c.get("refine_history") or [{}])[0])]
        n = len(cands)
        return {
            "pool": len(pool), "judged": n, "rejected_by_code": len(code_rejects),
            "eligible": sum(1 for c in cands if c.get("eligible")),
            "pass_rate": round(sum(1 for c in cands if c.get("eligible")) / n, 3) if n else None,
            "first_pass_clean": len(first_pass),
            "repaired": len(repaired),
            "repair_yield": round(sum(1 for c in repaired if c.get("eligible")) / len(repaired), 3) if repaired else None,
            "templates": dict(Counter(c.get("template") for c in cands)),
            "failures": dict(Counter("clean pass" if clean((c.get("refine_history") or [{}])[0])
                                     else ((c.get("refine_history") or [{}])[0].get("main_failure") or "unspecified")
                                     for c in cands)),
            "reserve": len((_json(h.out_dir / "ranked.json") or {}).get("reserve", [])),
            "recorded": h.recorded,
            "llm": meta.get("llm", {}).get("total") or (h.llm.snapshot()["total"] if h.llm else {}),
            "calls_per_model": meta.get("llm", {}).get("calls_per_model", {}),
            "seconds": meta.get("seconds"), "review": meta.get("review"),
        }
