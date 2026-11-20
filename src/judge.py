"""Stage 5A: the cold judge. One promo per call, promo text only: no bible, no episode, nothing a
scrolling stranger would not have. Batching promos would leak context between them.

S = 0.30 H + 0.25 C + 0.20 A + 0.15 T + 0.10 P   (H, C, A, T from the judge; P from code)
Penalties (code) are applied later in rank.select.
"""
from __future__ import annotations

import hashlib
import re

from .features import block_intensity, pace
from .llm import LLM, fill, load_prompt
from .parse import EPS, Show
from .schemas import JudgeVerdict

REPAIRABLE = {"hook_late", "hook_weak", "answered_in_clip", "no_open_question", "too_short"}


def _anonymize(text: str, aliases: list[tuple[str, str]]) -> str:
    for name, desc in aliases:
        text = re.sub(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", f"the {desc}", text, flags=re.I)
    return re.sub(r"\b(the|a|an) the\b", "the", text, flags=re.I)


def _speaker(speaker: str, aliases: list[tuple[str, str]]) -> str:
    for name, desc in aliases:
        if speaker.strip().lower() == name.lower():
            return desc.upper()
    return speaker


def render_clip(show: Show, cand: dict, aliases: list[tuple[str, str]] | None = None) -> tuple[str, list[str]]:
    """What a stranger experiences, row by row, with seconds from the start of the clip.
    Returns (text, row_ids) where row_ids[n-1] is the block behind row [n]."""
    aliases = aliases or []
    rows: list[tuple[float, str, str]] = []
    off = 0.0
    out_lines = [f"CLIP: {cand['duration']:.1f} s, {len(cand['segments'])} segment(s)"]
    row_ids: list[str] = []
    n = 0
    for i, s in enumerate(cand["segments"]):
        if i:
            out_lines.append("--- CUT TO A DIFFERENT MOMENT ---")
        ep = show.ep(s["ep"])
        rows = []
        for b in ep.blocks_overlapping(s["start"], s["end"]):
            t = off + max(b.start, s["start"]) - s["start"]
            if b.kind == "line":
                if b.start < s["start"] - EPS or b.end > s["end"] + EPS:
                    continue
                emo = f" ({', '.join(b.emotions)})" if b.emotions else ""
                vo = " (voice-over)" if "V.O." in b.modifiers else " (off-screen)" if "O.S." in b.modifiers else ""
                rows.append((t, b.id, f'LINE {_speaker(b.speaker, aliases)}{vo}{emo}: "{b.text}"'))
            elif b.kind == "shot":
                if not b.synthetic:
                    rows.append((t, b.id, "VISUAL: " + _anonymize(b.text, aliases)))
            elif b.kind == "action":
                rows.append((t, b.id, "VISUAL: " + _anonymize(b.text, aliases)))
            elif b.kind == "sfx":
                rows.append((t, b.id, "SOUND: " + _anonymize(b.text, aliases)))
        for t, bid, body in rows:
            n += 1
            row_ids.append(bid)
            out_lines.append(f"[{n}] {t:4.1f}s {body}")
        off += s["end"] - s["start"]
    return "\n".join(out_lines), row_ids


def clip_sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def offline_judge(show: Show, cand: dict) -> JudgeVerdict:
    """No-LLM baseline: keyword intensity of the first 3 s (H) and of the final beat (C)."""
    first3 = 0.0
    off = 0.0
    for s in cand["segments"]:
        ep = show.ep(s["ep"])
        for b in [*ep.lines, *ep.actions, *ep.sfx]:
            if s["start"] - EPS <= b.start < s["end"] and off + b.start - s["start"] <= 3.0:
                first3 = max(first3, block_intensity(b)[0])
        off += s["end"] - s["start"]
    cb = show.block(cand["cliff"]["id"])
    w = block_intensity(cb)[0] if cb else 0.3
    if cb is not None and cb.kind == "line" and cb.text.rstrip().endswith(("?", "!", "...", "—")):
        w = min(1.0, w + 0.25)
    lines = [show.block(i) for ids in cand["line_ids"] for i in ids]
    charged = sum(bool(block_intensity(ln)[1]) for ln in lines) / max(len(lines), 1)
    H, C, T = 1 + 4 * first3, 1 + 4 * w, 1 + 4 * min(1.0, charged * 1.5)
    failure = "none" if min(H, C) >= 3 else ("hook_weak" if H < C else "no_open_question")
    return JudgeVerdict(hook_score=round(H, 2), cliffhanger_score=round(C, 2), standalone_score=3.0,
                        trope_pull_score=round(T, 2), open_question="What happens next?" if C >= 3 else "",
                        answered_in_clip=C < 2.5, who_wants_what="(offline heuristic)",
                        why_it_works="(offline heuristic: keyword intensity)", main_failure=failure)


def weighted_score(v: dict, pace_p: float, W: dict) -> float:
    return round(W["hook"] * v["hook_score"] + W["cliff"] * v["cliffhanger_score"]
                 + W["standalone"] * v["standalone_score"] + W["trope"] * v["trope_pull_score"]
                 + W["pace"] * pace_p, 3)


def cold_judge(llm: LLM, show: Show, cand: dict, cfg: dict, aliases: list[tuple[str, str]]) -> dict:
    text, row_ids = render_clip(show, cand, aliases)
    system, user_t = load_prompt(cfg["prompts"]["judge"])
    res = llm.complete_json(stage="judge", system=system, user=fill(user_t, clip=text), schema=JudgeVerdict,
                            offline_fn=lambda: offline_judge(show, cand))
    v = res.model_dump()
    # hard gates live in code, not in the model's good intentions
    if v["answered_in_clip"] and v["main_failure"] == "none":
        v["main_failure"] = "answered_in_clip"
    if not v["open_question"].strip() and v["main_failure"] == "none":
        v["main_failure"] = "no_open_question"
    if v["main_failure"] != "needs_context":
        v["missing_entity"] = None
    for key, idx in (("resolution_id", v.get("resolution_line")), ("best_hook_id", v.get("best_hook_line"))):
        v[key] = row_ids[idx - 1] if isinstance(idx, int) and 1 <= idx <= len(row_ids) else None
    P, pace_info = pace(show, cand)
    v["pace"] = P
    v["pace_info"] = pace_info
    v["score"] = weighted_score(v, P, cfg["score"]["weights"])
    v["clip_sha"] = clip_sha(text)
    v["model"] = llm.model_for("judge")
    v["prompt_version"] = cfg["prompts"]["judge"]
    return v


def passes(v: dict, cfg: dict) -> bool:
    """Loop-stop 'pass' (v4): clears the floor and the judge names no failure at all."""
    return v["score"] >= cfg["score"]["min_score"] and v["main_failure"] == "none"


def hard_fail(v: dict, cfg: dict) -> bool:
    return v["main_failure"] in set(cfg["score"]["hard_fails"]) or not v["open_question"].strip() \
        or v["answered_in_clip"]
