"""Web layer: run manager (background step, editor interrupt, resume), per-run keys, config overlay, API policy."""
import json
import os
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "sample_data" / "demo_show"


def _wait(h, timeout=120):
    t0 = time.time()
    while h.status in ("queued", "running"):
        assert time.time() - t0 < timeout, "run did not finish"
        time.sleep(0.1)


def _files():
    return {p.name: p.read_bytes() for p in SAMPLE.glob("episode-*.txt")}


def test_overlay_merges_routes_and_keeps_the_rest():
    from src.config import load_config
    base = load_config()
    cloud = load_config(overlay=ROOT / "config.cloud.yaml")
    assert cloud["llm"]["routes"]["judge"]["models"][0]["name"] == "gemini/gemini-flash-lite-latest"
    assert cloud["run"]["fast_episodes"] == 5 and cloud["run"]["judge_batch"] == base["run"]["judge_batch"]
    assert cloud["promo"] == base["promo"] and cloud["score"] == base["score"]


def test_manager_review_reject_resume_offline(tmp_path):
    from src.webrun import RunManager
    m = RunManager(tmp_path, overlay=ROOT / "config.cloud.yaml")
    h = m.create(_files(), "Demo", {"model": "offline", "duels": True, "repair_rounds": 1, "fast": True}, {})
    _wait(h)
    assert h.status == "review", h.error
    sl = h.review["shortlist"]
    assert sl and all("hook_at_s" in p and "scores" in p for p in sl)
    m.resume(h, [{"cand_id": sl[0]["cand_id"], "action": "reject", "reason": "test"}])
    _wait(h)
    if h.status == "review":  # backfilled shortlist: round 2
        assert h.review["round"] == 1 and sl[0]["cand_id"] not in {p["cand_id"] for p in h.review["shortlist"]}
        m.resume(h, [])
        _wait(h)
    assert h.status == "done", h.error
    ranked = json.loads((h.out_dir / "ranked.json").read_text(encoding="utf-8"))
    assert sl[0]["cand_id"] not in {p["cand_id"] for p in ranked["promos"]}
    q = m.quality(h)
    assert q["judged"] >= 1 and q["pass_rate"] is not None
    with pytest.raises(ValueError):
        m.resume(h, [])  # not waiting for review any more


def test_bad_upload_is_rejected_before_queueing(tmp_path):
    from src.webrun import RunManager
    m = RunManager(tmp_path)
    with pytest.raises(ValueError):
        m.create({"x.txt": b"just some text"}, None, {"model": "offline"}, {})
    assert not any((tmp_path / "runs").iterdir())


def test_visitor_key_stays_out_of_environ_and_logs(tmp_path, monkeypatch):
    from src.config import load_config
    from src.llm import LLM
    key = "gsk_visitor_secret_key_123456"
    seen = {}


    def fake_completion(**kw):
        seen.update(kw)
        raise RuntimeError(f"provider echoed key {key}")

    import litellm
    monkeypatch.setattr(litellm, "completion", fake_completion)
    cfg = load_config(None, {"llm.model": "groq/openai/gpt-oss-120b", "llm.cache_dir": str(tmp_path / "c")})
    llm = LLM(cfg, call_log=tmp_path / "calls.jsonl", api_keys={"groq": key})
    before = dict(os.environ)
    from pydantic import BaseModel

    class S(BaseModel):
        ok: bool

    with pytest.raises(RuntimeError) as e:
        llm.complete_json(stage="judge", system="s", user="u", schema=S)
    assert seen["api_key"] == key
    assert dict(os.environ) == before and key not in "".join(os.environ.values())
    assert key not in llm.scrub(str(e.value))


def test_api_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("PROMO_WORK_DIR", str(tmp_path))
    monkeypatch.delenv("DEMO_PASSCODE", raising=False)
    from fastapi.testclient import TestClient
    import server
    c = TestClient(server.app)
    assert c.get("/api/health").json()["ok"]
    assert c.get("/").status_code == 200
    one = {"files": ("episode-001-demo.txt", (SAMPLE / "episode-001-demo.txt").read_bytes(), "text/plain")}
    r = c.post("/api/runs", data={"settings": json.dumps({"engine": "custom", "source": "sample", "model": "groq/x"})})
    assert r.status_code == 403
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("GROQ_API_KEY", "y")
    r = c.post("/api/runs", data={"settings": json.dumps({"engine": "cloud", "source": "upload"})}, files=one)
    assert r.status_code == 403  # server keys on uploads need the passcode, and none is set
    r = c.post("/api/runs", data={"settings": json.dumps({"engine": "offline", "source": "upload"})}, files=one)
    assert r.status_code == 200, r.text
    rid = r.json()["run_id"]
    for _ in range(300):
        s = c.get(f"/api/runs/{rid}").json()
        if s["status"] not in ("queued", "running"):
            break
        time.sleep(0.1)
    assert s["status"] == "review", s
    assert c.get(f"/api/runs/{rid}/files/runs.db").status_code == 404
    assert c.get(f"/api/runs/{rid}/files/ranked.json").status_code == 200
