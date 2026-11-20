"""Assembly validators, ranking maths, the LLM adapter (mocked provider) and the full graph offline."""
import json
import types

import pytest

from src.assemble import build, extend_as_cold_open, iou, nudge, validate
from src.features import show_features
from src.parse import fmt_ts, parse_show
from src.rank import bradley_terry, select
from tests.synthetic_show import make_show


@pytest.fixture
def synth(tmp_path):
    show = parse_show(make_show(tmp_path))
    return show, show_features(show)


def _spec(show, ep, start_line, cliff_line, template="continuous"):
    e = show.ep(ep)
    s, c = e.lines[start_line], e.lines[cliff_line]
    return {"template": template, "source": "test", "hook_id": s.id, "cliff_id": c.id, "confidence": 0.8,
            "segs": [{"ep": ep, "start_id": s.id, "end_id": c.id, "role": "main"}]}


def test_build_continuous_passes_all_validators(synth, cfg):
    show, feats = synth
    cand, rej = build(show, feats, _spec(show, 1, 1, 6), cfg)
    assert rej is None and not validate(show, cand, cfg)
    assert cand["hook"]["at_s"] <= 3.0 and 20 <= cand["duration"] <= 60
    assert cand["cliff"]["tail_s"] <= 1.5


def test_too_short_becomes_cold_open(synth, cfg):
    show, feats = synth
    cand, rej = build(show, feats, _spec(show, 1, 5, 7), cfg)  # ~15 s of tension: below the 20 s floor
    assert rej is None, rej
    assert cand["template"] == "cold_open" and cand["segments"][0]["role"] == "flash"
    assert "auto:cold_open_extension" in cand["notes"] and not validate(show, cand, cfg)


def test_validator_catches_hook_late_and_line_cuts(synth, cfg):
    show, feats = synth
    cand, _ = build(show, feats, _spec(show, 1, 1, 6), cfg)
    bad = json.loads(json.dumps(cand))
    bad["hook"]["at_s"] = 3.4
    assert "hook_late" in validate(show, bad, cfg)
    bad = json.loads(json.dumps(cand))
    bad["segments"][0]["end"] = show.ep(1).lines[6].start + 1.0  # inside a line
    assert "cuts_inside_line" in validate(show, bad, cfg)


def test_nudge_snaps_to_legal_cut(synth, cfg):
    show, feats = synth
    cand, _ = build(show, feats, _spec(show, 1, 1, 6), cfg)
    new, errs = nudge(show, feats, cand, [{"seg": 0, "start": -0.3}], cfg)
    assert new is not None and not errs
    assert show.ep(1).is_cut(new["segments"][0]["start"]) or not show.ep(1).lines_crossing(new["segments"][0]["start"])


def test_iou_and_bradley_terry():
    a = {"segments": [{"ep": 1, "start": 0.0, "end": 30.0}]}
    b = {"segments": [{"ep": 1, "start": 15.0, "end": 45.0}]}
    c = {"segments": [{"ep": 2, "start": 0.0, "end": 30.0}]}
    assert abs(iou(a, b) - 1 / 3) < 1e-9 and iou(a, c) == 0.0
    p = bradley_terry(["x", "y", "z"], [("x", "y"), ("x", "y"), ("x", "z"), ("y", "z"), ("y", "z")])
    assert p["x"] > p["y"] > p["z"]


def test_select_caps_and_dedupe(cfg):
    def cand(i, ep, start, score, trope="t"):
        return {"cand_id": f"c{i}", "eligible": True, "final_score": score, "hook": {"ep": ep}, "trope": trope,
                "segments": [{"ep": ep, "start": start, "end": start + 30}], "episodes": [ep]}
    scored = [cand(1, 1, 0, 4.9), cand(2, 1, 5, 4.8), cand(3, 1, 100, 4.7), cand(4, 1, 200, 4.6),
              cand(5, 2, 0, 4.5, "u"), cand(6, 3, 0, 4.4, "u")]
    short, _ = select(scored, ["c6"], cfg)
    ids = [c["cand_id"] for c in short]
    assert "c2" not in ids  # IoU 0.71 with c1
    assert ids.count("c3") + ids.count("c4") == 1  # max 2 per episode
    assert "c6" not in ids  # rejected by the editor


# --------------------------------------------------------------------------- LLM adapter with a mocked provider
def _resp(text, finish="stop", completion_tokens=5):
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text), finish_reason=finish)],
        usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=completion_tokens), model="mock")


def test_empty_answers_from_token_budget_and_output_filter(tmp_path, cfg, monkeypatch):
    """Azure returns 200 with empty content in two cases: a reasoning model spent max_tokens thinking
    (finish=length), or the OUTPUT filter blocked the answer (finish=content_filter). Neither is fixed by
    re-asking the same model the same way."""
    import litellm
    from src.llm import LLM
    from src.schemas import DuelVerdict

    c = json.loads(json.dumps(cfg))
    c["llm"].update(model="openai/nano", fallbacks=["groq/backup"], rpm=None, cache_dir=str(tmp_path / "cache"),
                    max_tokens_cap=8000)
    calls = []
    ok = '{"winner": "A", "margin": "clear", "reason": "r"}'

    def fake(**kw):
        user = kw["messages"][-1]["content"]
        calls.append((kw["model"], kw["max_tokens"]))
        if kw["model"] == "openai/nano" and "thinker" in user and kw["max_tokens"] < 8000:
            return _resp("", finish="length", completion_tokens=kw["max_tokens"])
        if kw["model"] == "openai/nano" and "always empty" in user:
            return _resp("", finish="length", completion_tokens=kw["max_tokens"])
        if kw["model"] == "openai/nano" and "slap" in user:
            return _resp('{"winner": "A", "mar', finish="content_filter")
        return _resp(ok)

    monkeypatch.setattr(litellm, "completion", fake)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0)
    llm = LLM(c)

    def ask(user):
        calls.clear()
        llm.complete_json(stage="duel", system="s", user=user, schema=DuelVerdict, max_tokens=2000)
        return list(calls)

    # budget exhausted: same model, doubled budget, until it answers
    assert ask("thinker") == [("openai/nano", 2000), ("openai/nano", 4000), ("openai/nano", 8000)]
    # still empty at the cap: fall back for this call
    assert [m for m, _ in ask("always empty")][-1] == "groq/backup"
    # output filter with a half-written answer: straight to the fallback, no corrective retry on Azure
    assert ask("slap clip") == [("openai/nano", 2000), ("groq/backup", 2000)]
    # the route itself is not moved: the next normal call is back on the primary
    assert ask("calm clip") == [("openai/nano", 2000)]


def test_llm_cache_retry_and_quota_fallback(tmp_path, cfg, monkeypatch):
    import litellm
    from src.llm import LLM
    from src.schemas import DuelVerdict

    c = json.loads(json.dumps(cfg))
    c["llm"].update(model="gemini/primary", fallbacks=["groq/backup"], rpm=None, cache_dir=str(tmp_path / "cache"))
    calls = []

    def fake(**kw):
        calls.append(kw["model"])
        if kw["model"] == "gemini/primary":
            raise litellm.exceptions.RateLimitError(
                message="Quota exceeded for metric GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                llm_provider="gemini", model="primary")
        if len(calls) == 2:
            return _resp("not json at all")
        return _resp('```json\n{"winner": "promo b", "margin": "Decisive", "reason": "sharper"}\n```')

    monkeypatch.setattr(litellm, "completion", fake)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0)
    llm = LLM(c)
    v = llm.complete_json(stage="duel", system="s", user="u", schema=DuelVerdict)
    assert v.winner == "B" and v.margin == "decisive"
    assert calls == ["gemini/primary", "groq/backup", "groq/backup"]  # daily cap -> fallback; bad JSON -> 1 retry
    v2 = llm.complete_json(stage="duel", system="s", user="u", schema=DuelVerdict)
    assert v2 == v and len(calls) == 3  # served from the disk cache
    assert llm.snapshot()["per_stage"]["duel"]["cache_hits"] == 1


def test_routes_hydrogen_context_flash_mine_luna_judge(tmp_path, cfg, monkeypatch):
    import litellm
    from src.llm import LLM
    from src.schemas import DuelVerdict

    monkeypatch.setenv("OPEN_API_KEY", "test-key")
    c = json.loads(json.dumps(cfg))
    c["llm"]["cache_dir"] = str(tmp_path / "cache")
    for r in c["llm"]["routes"].values():
        for m in r["models"]:
            m["rpm"] = None
    calls = []
    hydrogen, flash, lite = "gemini/gemini-4-hydrogen", "gemini/gemini-3.8-flash", "gemini/gemini-flash-lite-latest"
    luna, qwen, oss = "openai/gpt-6-luna", "groq/qwen/qwen3.8-27b", "groq/openai/gpt-oss-120b"

    def fake(**kw):
        user, model = kw["messages"][-1]["content"], kw["model"]
        calls.append((model, user, bool(kw.get("safety_settings"))))
        if model == luna:
            assert kw["api_key"] == "test-key"  # read from OPEN_API_KEY, not LiteLLM's default OPENAI_API_KEY
        if model == luna and "violent" in user:
            raise litellm.exceptions.BadRequestError(
                message="Invalid prompt: your prompt was flagged as potentially violating our usage policy "
                        "(content_policy_violation)", model="gpt-6-luna", llm_provider="openai")
        if model == qwen and "very violent" in user:
            raise litellm.exceptions.BadRequestError(message="content_policy: request blocked",
                                                     model="qwen3.8-27b", llm_provider="groq")
        if model == luna and "capped" in user:
            raise litellm.exceptions.RateLimitError(message="Rate limit reached: tokens per day (TPD)",
                                                    llm_provider="openai", model="gpt-6-luna")
        if model in (flash, lite) and "blocked" in user:
            raise litellm.exceptions.BadRequestError(
                message="GeminiException - The prompt was blocked: blockReason SAFETY", model=model, llm_provider="gemini")
        if model == hydrogen and "capped" in user:
            raise litellm.exceptions.RateLimitError(
                message="Quota exceeded for metric: generate_content_free_tier_requests, limit: PerDay",
                llm_provider="gemini", model=model)
        return _resp('{"winner": "A", "margin": "clear", "reason": "r"}')

    monkeypatch.setattr(litellm, "completion", fake)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0)
    llm = LLM(c)
    assert llm.model_for("bible") == llm.model_for("link") == hydrogen
    assert llm.model_for("mine") == flash
    assert llm.model_for("judge") == llm.model_for("judge_leak") == llm.model_for("duel") == luna

    def ask(stage, user):
        calls.clear()
        llm.complete_json(stage=stage, system="s", user=user, schema=DuelVerdict)
        return [m for m, _, _ in calls]

    assert ask("bible", "show") == [hydrogen] and calls[0][2]  # safety settings sent
    assert ask("mine", "episode") == [flash] and calls[0][2]
    assert ask("duel", "calm clip") == [luna]
    assert ask("duel", "violent clip") == [luna, qwen]  # refusal: Qwen for this call only
    assert ask("duel", "very violent clip") == [luna, qwen, oss]  # both refuse: gpt-oss is the last resort
    assert ask("duel", "calm clip 2") == [luna]  # back on Luna for the next call
    assert ask("judge", "capped clip") == [luna, qwen]  # daily cap
    assert ask("duel", "later clip") == [qwen]  # judge route stays on Qwen
    assert ask("link", "capped season") == [hydrogen, flash]  # Hydrogen daily cap: context falls back to Flash
    assert ask("bible", "show 2") == [flash]  # context route stays on Flash
    assert ask("mine", "episode 2") == [flash]  # the mine route has its own state: still on Flash
    assert ask("bible", "blocked show") == [flash, oss]  # Flash refuses too: gpt-oss is the context last resort
    assert ask("mine", "blocked episode") == [flash, lite, oss]  # both Gemini refuse: gpt-oss is the mine last resort
    assert ask("mine", "episode 3") == [flash]  # a refusal moves only that one call


# --------------------------------------------------------------------------- full graph, offline, synthetic show
def test_graph_offline_end_to_end_with_review(tmp_path, cfg):
    from langgraph.types import Command
    from src.graph import build_app
    from src.llm import LLM

    show_dir = make_show(tmp_path, n_eps=3)
    out = tmp_path / "out"
    c = json.loads(json.dumps(cfg))
    c["llm"]["model"] = "offline"
    ctx = {"show_dir": str(show_dir), "out_dir": str(out), "config": c, "run_duels": True, "enable_review": True,
           "fast": False, "episodes": None, "llm": LLM(c)}
    app = build_app(out / "runs.db")
    conf = {"configurable": {"thread_id": "t"}, "recursion_limit": 300}
    res = app.invoke({}, conf, context=ctx)
    assert "__interrupt__" in res
    shortlist = res["__interrupt__"][0].value["shortlist"]
    assert shortlist, "offline run should shortlist something on the synthetic show"
    first = shortlist[0]["cand_id"]
    res = app.invoke(Command(resume=[{"cand_id": first, "action": "reject", "reason": "test"}]), conf, context=ctx)
    assert "__interrupt__" in res  # backfilled shortlist goes back to the editor
    again = [p["cand_id"] for p in res["__interrupt__"][0].value["shortlist"]]
    assert first not in again
    res = app.invoke(Command(resume=[{"cand_id": again[0], "action": "nudge",
                                      "changes": [{"seg": 0, "end": 0.5}]}]), conf, context=ctx)
    assert "__interrupt__" not in res
    ranked = json.loads((out / "ranked.json").read_text(encoding="utf-8"))
    assert ranked["promos"] and all(p["round_trip_ok"] for p in ranked["promos"])
    assert first not in [p["cand_id"] for p in ranked["promos"]]
    decisions = [json.loads(x) for x in (out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {d["action"] for d in decisions} >= {"reject", "nudge"}
    assert (out / "report.md").exists() and (out / "candidates.jsonl").exists()
