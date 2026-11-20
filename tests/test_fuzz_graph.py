"""Fuzz the full graph with a fake provider that returns plausible but messy structured output:
random valid IDs, some invalid ones, every template and every judge failure type. Exercises the
cold-open / bridge / arc-stitch assembly and every repair branch that offline heuristics never reach."""
import hashlib
import json
import random
import re

import pytest

from src.assemble import validate
from src.parse import load_show
from tests.conftest import PACK
from tests.synthetic_show import make_show

ROW = re.compile(r"^(E\d+\.[SLAX]\d+) \[(\d+\.\d)\]", re.M)
FAILS = ["none", "none", "hook_late", "hook_weak", "answered_in_clip", "no_open_question", "needs_context",
         "unclear_stakes", "too_short"]


def _rows(text: str) -> list[tuple[str, float]]:
    return [(m.group(1), float(m.group(2))) for m in ROW.finditer(text)]


def fake_call(self, stage, route, messages, max_tokens, temperature):
    user = messages[-1]["content"] if messages[-1]["role"] == "user" else messages[1]["content"]
    rnd = random.Random(hashlib.md5((stage + user).encode()).hexdigest())
    if stage == "bible":
        out = {"premise": "A woman marries a stranger with a secret.", "characters": [
            {"name": "Lena", "aliases": ["Lady Lena"], "role": "heroine", "visual_descriptor": "young woman in a plain gown"},
            {"name": "Viktor", "aliases": [], "role": "hero", "visual_descriptor": "scarred bearded soldier"},
            {"name": "Mara", "role": "heroine", "visual_descriptor": "woman in grey"}],
            "tropes": ["hidden_identity"], "spoilers": [{"episode": 2, "reveal": "x", "keywords": ["guards"]}]}
    elif stage == "mine":
        main, _, nxt = user.partition("FIRST ")
        rows = _rows(main)
        lines = [r for r in rows if ".L" in r[0]]
        out = {"no_promo": False, "moments": [], "setups": [], "payoffs": []}
        if rnd.random() < 0.15 or len(lines) < 6:
            out["no_promo"] = True
        else:
            a = rnd.randrange(0, max(1, len(lines) - 5))
            cliff = next((l for l in lines[a:] if l[1] >= lines[a][1] + rnd.choice([12, 22, 35])), lines[-1])
            out["moments"].append({"template": "continuous", "start_id": lines[a][0], "hook_id": lines[min(a + rnd.choice([0, 1]), len(lines) - 1)][0],
                                   "cliff_id": cliff[0], "hook_type": "Slap!", "trope": "Hidden Identity", "confidence": rnd.choice([0.4, 0.9, 7])})
            b = rnd.randrange(2, max(3, len(lines) - 3))
            peak = lines[b]
            after = [l for l in lines if l[1] > peak[1] + 5]
            before = [l for l in lines if l[1] < peak[1] - 15] or lines[:1]
            if after:
                out["moments"].append({"template": "cold_open", "start_id": before[-1][0], "hook_id": peak[0], "peak_id": peak[0],
                                       "cliff_id": after[0][0], "hook_type": "reveal", "trope": "betrayal", "confidence": 0.7})
            if rnd.random() < 0.3:
                out["moments"].append({"template": "continuous", "start_id": "E99.L01", "hook_id": "E99.L01", "cliff_id": "E99.L02"})
            nrows = [r for r in _rows(nxt) if ".L" in r[0]]
            if nrows and rnd.random() < 0.6:
                out["bridge"] = {"start_id": lines[-3][0], "hook_id": lines[-3][0], "next_cliff_id": rnd.choice(nrows)[0],
                                 "trope": "cliff", "confidence": 0.6}
            out["setups"] = [{"id": rnd.choice(lines)[0], "what": "a vow"}]
            out["payoffs"] = [{"id": rnd.choice(lines)[0], "what": "the vow breaks"}]
    elif stage == "link":
        blocks = re.findall(r"^(E\d+\.[SLAX]\d+) \(Ep (\d+), (setup|payoff)\).*?\n((?:    .*\n?)+)", user, re.M)
        stitches = []
        by_ep = {}
        for bid, ep, kind, ctx in blocks:
            ids = [r[0] for r in _rows("\n".join(x.strip() for x in ctx.splitlines()))]
            if ids:
                by_ep.setdefault(ep, []).append(ids)
        eps = list(by_ep)
        for _ in range(3):
            if len(eps) < 2:
                break
            e1, e2 = rnd.sample(eps, 2)
            s1, s2 = rnd.choice(by_ep[e1]), rnd.choice(by_ep[e2])
            stitches.append({"segments": [{"start_id": s1[0], "end_id": s1[-1]}, {"start_id": s2[0], "end_id": s2[-1]}],
                             "hook_id": s1[0], "cliff_id": s2[-1], "trope": "hidden_identity", "confidence": 0.8})
        out = {"stitches": stitches}
    elif stage.startswith("judge"):
        n = len(re.findall(r"^\[\d+\]", user, re.M))
        f = rnd.choice(FAILS)
        out = {"hook_score": rnd.choice([2, 3, 4, 5, 6]), "cliffhanger_score": rnd.choice([1, 3, 4, 5]),
               "standalone_score": rnd.choice([2, 3, 4, 5]), "trope_pull_score": rnd.choice([2, 3, 4, 5]),
               "open_question": "" if f == "no_open_question" else "What will he do?",
               "answered_in_clip": f == "answered_in_clip", "who_wants_what": "x", "why_it_works": "y",
               "main_failure": f.upper() if rnd.random() < 0.2 else f,
               "missing_entity": "the duke" if f == "needs_context" else None,
               "resolution_line": rnd.randint(1, max(1, n)) if f == "answered_in_clip" else None,
               "best_hook_line": f"[{rnd.randint(1, max(1, n))}]" if f in ("hook_weak", "hook_late") else None}
    elif stage == "duel":
        out = {"winner": rnd.choice(["A", "B", "Promo B"]), "margin": "clear", "reason": "z"}
    else:
        raise AssertionError(stage)
    text = json.dumps(out)
    if rnd.random() < 0.1:
        text = "```json\n" + text + "\n```"
    return text, {"model": "fuzz", "requested_model": route.active["name"], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def _run(show_dir, out, cfg, monkeypatch, review=True):
    from langgraph.types import Command
    from src.graph import build_app
    from src.llm import LLM

    monkeypatch.setattr(LLM, "_call", fake_call)
    c = json.loads(json.dumps(cfg))
    c["llm"].update(model="fuzz/model", cache_dir=str(out / "cache"), rpm=None)
    ctx = {"show_dir": str(show_dir), "out_dir": str(out), "config": c, "run_duels": True, "enable_review": review,
           "fast": False, "episodes": None, "llm": LLM(c)}
    app = build_app(out / "runs.db")
    conf = {"configurable": {"thread_id": "fuzz"}, "recursion_limit": 300, "max_concurrency": 8}
    res = app.invoke({}, conf, context=ctx)
    for _ in range(4):
        if "__interrupt__" not in res:
            break
        sl = res["__interrupt__"][0].value["shortlist"]
        dec = [{"promo_id": sl[0]["promo_id"], "action": "reject", "reason": "fuzz"}] if len(sl) > 3 else []
        dec += [{"cand_id": p["cand_id"], "action": "nudge", "changes": [{"seg": 0, "start": -0.4, "end": "+0.6"}]} for p in sl[1:3]]
        res = app.invoke(Command(resume=dec), conf, context=ctx)
    assert "__interrupt__" not in res
    return c


def _check(out, c):
    show = load_show(out / "parsed.json")
    ranked = json.loads((out / "ranked.json").read_text(encoding="utf-8"))["promos"]
    cands = [json.loads(x) for x in (out / "candidates.jsonl").read_text(encoding="utf-8").splitlines()]
    pool = [json.loads(x) for x in (out / "pool.jsonl").read_text(encoding="utf-8").splitlines()]
    assert pool, "fuzzed mining should assemble something"
    for p in ranked:
        assert p["round_trip_ok"], p["promo_id"]
        assert p["hook"]["at_s"] <= 3.0 + 1e-6 and 20 <= p["duration_s"] <= 60
    templates = {p["template"] for p in pool}
    return show, ranked, cands, templates


def test_fuzz_synthetic(tmp_path, cfg, monkeypatch):
    out = tmp_path / "out"
    c = _run(make_show(tmp_path, n_eps=4), out, cfg, monkeypatch)
    _, ranked, cands, templates = _check(out, c)
    assert cands and any(x.get("rounds", 0) > 0 for x in cands), "some repairs should run"


@pytest.mark.skipif(not PACK.exists(), reason="confidential pack not present")
def test_fuzz_full_pack(tmp_path, cfg, monkeypatch):
    out = tmp_path / "out"
    c = _run(PACK, out, cfg, monkeypatch)
    show, ranked, cands, templates = _check(out, c)
    assert {"continuous", "cold_open", "bridge"} <= templates, templates
    repaired = [x for x in cands if x.get("repairs")]
    assert repaired, "repairs should succeed on some candidates"
    # every repaired / auto-fixed candidate that was published still passes the full validator suite
    from src.assemble import refresh
    from src.features import load_features
    feats = load_features(str(out / "parsed.json"))
    for x in cands:
        segs = [{"ep": s["ep"], "start": float(s["start"][:2]) * 60 + float(s["start"][3:]),
                 "end": float(s["end"][:2]) * 60 + float(s["end"][3:]), "role": s["role"]} for s in x["segments"]]
        cand = refresh(show, feats, {"cand_id": x["cand_id"], "template": x["template"], "segments": segs,
                                     "hook": {"id": x["hook"]["id"]}, "cliff": {"id": x["cliff"]["id"]}})
        assert not validate(show, cand, c), (x["cand_id"], validate(show, cand, c))
