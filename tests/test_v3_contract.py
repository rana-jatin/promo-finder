"""The code the v3 prompts rely on: prompt files + placeholders, the cold clip render (numbering, markers,
speaker map, row map), the judge's cliffhanger clamp and threshold pass/fail, repair keys = judge labels,
vocab enums, and the spoiler phrase rule."""
import json
import re
import typing

import pytest

from src.assemble import build
from src.bible import clip_cast
from src.features import show_features
from src.parse import parse_show
from tests.synthetic_show import make_show

PROMPT_STAGES = ("bible", "mine", "link", "judge", "duel")


@pytest.fixture
def synth(tmp_path):
    show = parse_show(make_show(tmp_path))
    return show, show_features(show)


def _fake_llm(monkeypatch, cfg, tmp_path, reply: dict, seen: list | None = None):
    from src.llm import LLM

    def fake(self, stage, route, messages, max_tokens, temperature):
        if seen is not None:
            seen.append({"stage": stage, "system": messages[0]["content"], "user": messages[-1]["content"]})
        return json.dumps(reply), {"model": "fake", "requested_model": "fake",
                                   "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    monkeypatch.setattr(LLM, "_call", fake)
    c = json.loads(json.dumps(cfg))
    c["llm"].update(model="fake/model", cache_dir=str(tmp_path / "cache"), rpm=None)
    return LLM(c, call_log=tmp_path / "calls.jsonl"), c


# --------------------------------------------------------------------------- config + prompt files
def test_prompt_version_v3_resolves_every_stage(cfg):
    from src.config import load_config
    from src.llm import load_prompt
    assert cfg["prompt_version"] == "v3"
    assert cfg["prompts"] == {s: f"{s}_v3" for s in PROMPT_STAGES}
    for s in PROMPT_STAGES:
        system, user = load_prompt(cfg["prompts"][s])
        assert system and user
    mixed = load_config(overrides={"prompts.judge": "judge_v1"})
    assert mixed["prompts"]["judge"] == "judge_v1" and mixed["prompts"]["mine"] == "mine_v3"


# --------------------------------------------------------------------------- clip render
def _two_segment_clip():
    return {"duration": 24.0, "segments": [{"ep": 1, "start": 0.0, "end": 12.0}, {"ep": 2, "start": 0.0, "end": 12.0}]}


CAST_BIBLE = {"characters": [
    {"name": "Mara", "aliases": ["Lady Mara"], "visual_descriptor": "the young woman in grey"},
    {"name": "Tomas", "aliases": [], "visual_descriptor": ""}]}


def test_render_numbers_from_zero_with_unnumbered_markers(synth):
    from src.judge import render_clip
    show, _ = synth
    text, row_map = render_clip(show, _two_segment_clip(), clip_cast(CAST_BIBLE))
    lines = text.splitlines()
    numbered = [ln for ln in lines if re.match(r"^\[\d+\] ", ln)]
    assert numbered[0].startswith("[0] ")
    assert [int(re.match(r"^\[(\d+)\]", ln).group(1)) for ln in numbered] == list(range(len(numbered)))
    assert lines.count("--- 3.0s HOOK WINDOW ENDS ---") == 1 and lines.count("--- CUT ---") == 1
    hook_at = lines.index("--- 3.0s HOOK WINDOW ENDS ---")
    before = [float(re.search(r"\] +([\d.]+)s", ln).group(1)) for ln in lines[:hook_at] if ln.startswith("[")]
    assert before and max(before) < 3.0
    assert len(row_map) == len(numbered) and {r["seg"] for r in row_map} == {0, 1}
    assert all(set(r) == {"seg", "id"} for r in row_map)
    # rows after the CUT map to segment 1
    cut = lines.index("--- CUT ---")
    first_after = int(re.match(r"^\[(\d+)\]", next(ln for ln in lines[cut:] if ln.startswith("["))).group(1))
    assert row_map[first_after]["seg"] == 1 and row_map[first_after - 1]["seg"] == 0


def test_speaker_map_names_only_what_is_said_aloud(synth):
    from src.judge import render_clip
    show, _ = synth
    cast = clip_cast(CAST_BIBLE)
    text, _ = render_clip(show, _two_segment_clip(), cast)
    # nobody says a name: both people are SPEAKER_n, and each keeps ONE label across the CUT
    assert "MARA" not in text and "TOMAS" not in text
    labels = set(re.findall(r"LINE (\S+?)[ :]", text))
    assert labels == {"SPEAKER_1", "SPEAKER_2"}
    assert "Close-up of the young woman in grey" in text  # unnamed Mara in VISUAL rows: her descriptor
    assert re.search(r"Close-up of SPEAKER_\d", text)  # Tomas has no descriptor: his speaker label

    show.ep(1).lines[0].text = "Tomas, we should go home now."  # someone now says his name aloud
    text, _ = render_clip(show, _two_segment_clip(), cast)
    assert "LINE TOMAS" in text and "Close-up of Tomas" in text
    assert set(re.findall(r"LINE (\S+?)[ :]", text)) == {"TOMAS", "SPEAKER_1"}

    duel, _ = render_clip(show, _two_segment_clip(), cast, prefix="B_")
    assert "LINE B_SPEAKER_1" in duel and "LINE TOMAS" in duel


# --------------------------------------------------------------------------- judge
def _cand(show, feats, cfg):
    e = show.ep(1)
    spec = {"template": "continuous", "source": "test", "hook_id": e.lines[1].id, "cliff_id": e.lines[6].id,
            "confidence": 0.8, "segs": [{"ep": 1, "start_id": e.lines[1].id, "end_id": e.lines[6].id, "role": "main"}]}
    cand, rej = build(show, feats, spec, cfg)
    assert rej is None
    return cand


def test_judge_clamps_cliffhanger_and_logs_the_clash(synth, cfg, tmp_path, monkeypatch):
    from src.judge import cold_judge, hard_fail, passes
    show, feats = synth
    reply = {"who_wants_what": "x", "open_question": "Will he pay?", "answered_in_clip": True, "resolution_line": 0,
             "best_hook_line": None, "hook_score": 5, "cliffhanger_score": 5, "standalone_score": 5,
             "trope_pull_score": 5, "main_failure": "hook_late", "missing_entity": None, "why_it_works": "y"}
    llm, c = _fake_llm(monkeypatch, cfg, tmp_path, reply)
    v = cold_judge(llm, show, _cand(show, feats, c), c, [])
    assert v["cliffhanger_score"] == 2.0 and v["clash"] == {"rule": "answered_in_clip", "cliffhanger_score_given": 5.0}
    log = [json.loads(x) for x in (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(r.get("event") == "judge_clash" for r in log)
    assert v["resolution_id"] and v["resolution_seg"] == 0  # [0] is a real row
    assert hard_fail(v, c) and not passes(v, c)


def test_pass_fail_comes_from_thresholds_not_the_label(cfg):
    from src.judge import passes
    good = {"score": 4.0, "hook_score": 4.0, "cliffhanger_score": 4.0, "standalone_score": 3.0,
            "trope_pull_score": 3.0, "main_failure": "hook_late"}
    assert passes(good, cfg)  # the judge named a failure, but every threshold is met
    assert not passes({**good, "main_failure": "none", "hook_score": 2.0}, cfg)  # under a floor
    assert not passes({**good, "main_failure": "none", "score": 3.4}, cfg)  # under min_score


def test_repair_keys_match_the_judge_labels():
    from src.repair import REPAIRABLE, REPAIRS
    from src.schemas import FAILURES, JudgeVerdict
    labels = set(typing.get_args(JudgeVerdict.model_fields["main_failure"].annotation))
    assert labels == set(FAILURES)
    assert set(REPAIRS) == labels - {"none"}
    assert {"needs_context", "unclear_stakes"}.isdisjoint(REPAIRABLE)


# --------------------------------------------------------------------------- mining
def test_mine_last_episode_preview_is_na_none_and_vocab_is_enforced(synth, cfg, tmp_path, monkeypatch):
    from src.mine import mine_episode
    show, feats = synth
    last = show.episode_numbers[-1]
    e = show.ep(last)
    reply = {"no_promo": False, "moments": [
        {"template": "continuous", "start_id": e.lines[1].id, "hook_id": "", "cliff_id": e.lines[6].id,
         "hook_type": "slap", "trope": "secret_baby", "intended_question": "q", "why": "w", "confidence": 0.8},
        {"template": "bridge", "start_id": e.lines[8].id, "next_cliff_id": e.lines[9].id}],
        "setups": [{"id": e.lines[2].id, "what": "a vow", "arc": "Revenge Plot"}]}
    seen: list = []
    llm, c = _fake_llm(monkeypatch, cfg, tmp_path, reply, seen)
    bible = {"premise": "p", "tropes": ["hidden_identity"], "arcs": [{"id": "revenge_plot", "name": "Revenge"}]}
    rec = mine_episode(llm, show, last, last + 1, bible, feats, c)
    user, system = seen[0]["user"], seen[0]["system"]
    assert "FIRST 20 s OF EPISODE N/A" in user and user.rstrip().endswith("NONE")
    assert "trope: one of hidden_identity, other." in system and "hook_type: one of slap," in system
    assert '"enum":["hidden_identity","other"]' in system.replace(" ", "")  # the schema enum the model sees
    m = rec["result"]["moments"]
    assert len(m) == 1 and m[0]["trope"] == "other" and m[0]["hook_id"] == m[0]["start_id"]
    assert [d["why"] for d in rec["dropped"]] == ["bridge_without_next_episode"]
    assert rec["result"]["setups"][0]["arc"] == "revenge_plot"
    assert rec["prompt_version"] == "mine_v3"


# --------------------------------------------------------------------------- spoilers
def test_spoiler_phrase_needs_all_content_words():
    from src.rank import content_words, phrase_in
    assert content_words("faked his death") == ["faked", "death"]
    assert phrase_in("faked his death", "she knows he faked the whole death scene")
    assert not phrase_in("faked his death", "he faked a smile")
    assert phrase_in("real father", "you are not my real father!")
    assert not phrase_in("the", "the the the")  # no content words: never matches
