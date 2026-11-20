"""Re-record the sample show for the hosted demo, with the cloud config (config.cloud.yaml):

  * sample_data/llm_cache/      model answers, so "Run on 3 sample episodes" replays with no API calls
  * sample_data/recorded_run.json  calls / tokens / cost / seconds of each recording run, shown next to a replay

Two passes:
  1. metrics: each settings combination runs with its OWN empty cache, so its numbers are what a fresh run
     really costs (models answer the same prompt differently run to run, so these caches cannot be merged).
  2. cache: one shared cache, seeded with pass 1's default-settings answers; the other combinations then add
     only what they need. Every combination replays exactly, and the default replays its own recorded run.
Each run also rejects the #1 promo once, so the backfill + re-rank after a reject replays too.
Uses the GEMINI_API_KEY / GROQ_API_KEY in .env; costs a few cents.

    python tools/record_sample.py                       both passes
    python tools/record_sample.py --cache-only DIR      pass 2 only, seeded from DIR (a pass-1 default cache)
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from src.webrun import RunManager, recording_key  # noqa: E402

SAMPLE = ROOT / "sample_data" / "demo_show"
CACHE_OUT = ROOT / "sample_data" / "llm_cache"
META_OUT = ROOT / "sample_data" / "recorded_run.json"
# (fast, duels, repair_rounds): the UI default first, then the switches a reviewer is likely to flip
COMBOS = [(True, False, 1), (True, True, 1), (True, False, 2), (True, False, 0), (False, True, 2)]


def wait(h, timeout=900):
    t0 = time.time()
    while h.status in ("queued", "running"):
        if time.time() - t0 > timeout:
            raise TimeoutError(h.id)
        time.sleep(1)
    if h.status == "error":
        raise RuntimeError(h.error)


def run_combo(m: RunManager, files: dict, title: str, fast: bool, duels: bool, rounds: int) -> dict:
    """One run to the first review, then reject #1 and approve the rest. Returns the first-review snapshot."""
    settings = {"engine": "cloud", "source": "sample", "model": None, "fast": fast, "duels": duels,
                "repair_rounds": rounds}
    h = m.create(files, title, settings, {})
    wait(h)
    s = h.snapshot()
    if h.status == "review" and s["review"]["shortlist"]:  # record one reject -> backfill cycle too
        m.resume(h, [{"cand_id": s["review"]["shortlist"][0]["cand_id"], "action": "reject",
                      "reason": "recording the backfill path"}])
        wait(h)
        while h.status == "review":
            m.resume(h, [])
            wait(h)
    return s


def shared_cache(files: dict, title: str, seed: Path) -> None:
    root = Path(tempfile.mkdtemp(prefix="pf-shared-"))
    m = RunManager(root, workers=1, seed_cache=seed, overlay=ROOT / "config.cloud.yaml")
    for fast, duels, rounds in COMBOS:
        s = run_combo(m, files, title, fast, duels, rounds)
        print(f"shared pass {fast=} {duels=} {rounds=}: +{int(s['llm'].get('calls', 0))} new calls", flush=True)
    tmp = CACHE_OUT.with_name("llm_cache.new")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(m.cache_dir, tmp)
    shutil.rmtree(CACHE_OUT, ignore_errors=True)
    tmp.rename(CACHE_OUT)
    print(f"wrote {sum(1 for _ in CACHE_OUT.rglob('*.json'))} cached answers to {CACHE_OUT}")


def main() -> None:
    load_dotenv(ROOT / ".env")
    files = {p.name: p.read_bytes() for p in SAMPLE.glob("episode-*.txt")}
    title = (SAMPLE / "title.txt").read_text(encoding="utf-8").strip()
    if len(sys.argv) == 3 and sys.argv[1] == "--cache-only":
        shared_cache(files, title, Path(sys.argv[2]))
        return
    runs, caches, models = {}, [], []
    for fast, duels, rounds in COMBOS:
        key = recording_key({"fast": fast, "duels": duels, "repair_rounds": rounds})
        root = Path(tempfile.mkdtemp(prefix="pf-record-"))
        m = RunManager(root, workers=1, overlay=ROOT / "config.cloud.yaml")
        s = run_combo(m, files, title, fast, duels, rounds)
        llm = s["llm"]
        runs[key] = {"calls": int(llm.get("calls", 0)), "prompt_tokens": int(llm.get("prompt_tokens", 0)),
                     "completion_tokens": int(llm.get("completion_tokens", 0)),
                     "cost_usd": round(float(llm.get("cost_usd", 0)), 4), "seconds": round(s["elapsed_s"], 1),
                     "shortlist": len(s["review"]["shortlist"]) if s["review"] else 0,
                     "reserve": s["review"]["reserve"] if s["review"] else 0}
        models = s["models"]
        print(f"{key}: {runs[key]}", flush=True)
        caches.append(m.cache_dir)
    META_OUT.write_text(json.dumps({"recorded_at": time.strftime("%Y-%m-%d %H:%M"), "models": models,
                                    "runs": runs}, indent=2), encoding="utf-8")
    shared_cache(files, title, caches[0])  # COMBOS[0] is the UI default


if __name__ == "__main__":
    main()
