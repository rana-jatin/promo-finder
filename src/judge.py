"""Stage 5A: the cold judge. One promo per call, promo text only: no bible, no episode, nothing a
scrolling stranger would not have. Batching promos would leak context between them.

S = 0.30 H + 0.25 C + 0.20 A + 0.15 T + 0.10 P   (H, C, A, T from the judge; P from code)
Penalties (code) are applied later in rank.select. Pass/fail comes from config thresholds
(score.min_score, score.pass_floors); the judge's main_failure only chooses the repair.
"""
from __future__ import annotations

import hashlib
import re

from .bible import GENERIC_TITLES, _norm, strip_honorifics
from .features import block_intensity, pace
from .llm import LLM, ask, provenance
from .parse import EPS, Show
from .schemas import JudgeVerdict

CUT = "--- CUT ---"


def _hook_marker(hook_s: float) -> str:
    return f"--- {hook_s:.1f}s HOOK WINDOW ENDS ---"


def _said(form: str, spoken: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(form)}(?![a-z0-9])", spoken))


def _member_of(speaker: str, cast: list[dict]) -> int | None:
    """Index of the cast member a script speaker label belongs to: exact name/alias, else without honorifics."""
    s = _norm(speaker)
    s = s[4:] if s.startswith("the ") else s
    for i, m in enumerate(cast):
        if s in m["labels"] or f"the {s}" in m["labels"]:
            return i
    bare = strip_honorifics(s)
    for i, m in enumerate(cast):
        if bare and bare not in GENERIC_TITLES and any(strip_honorifics(x) == bare for x in m["labels"]):
            return i
    return None


def _spoken_text(lines: list) -> str:
    text = " \n ".join(_norm(b.text) for b in lines)
    for g in sorted((g for g in GENERIC_TITLES if " " in g), key=len, reverse=True):
        text = re.sub(rf"(?<![a-z0-9]){re.escape(g)}(?![a-z0-9])", " ", text)  # "Your Grace" names no one
    return text


def speaker_map(lines: list, cast: list[dict], prefix: str = "") -> tuple[dict[str, str], dict[int, str]]:
    """One map per clip, across all its segments. A person keeps a real name only if a line in the clip says one
    of their names aloud (the bible's aliases decide which strings are names); everyone else is SPEAKER_n in
    order of first line. Returns ({script speaker label: shown label}, {cast index: name form said aloud})."""
    spoken = _spoken_text(lines)
    said: dict[int, str] = {}
    for i, m in enumerate(cast):
        form = next((f for f in m["names"] if _said(f, spoken)), None)  # names are longest first
        if form:
            said[i] = form
    labels: dict[str, str] = {}
    by_person: dict[object, str] = {}
    n = 0
    for b in lines:
        if b.speaker in labels:
            continue
        idx = _member_of(b.speaker, cast)
        key: object = idx if idx is not None else ("raw", _norm(b.speaker))
        if key not in by_person:
            if idx is not None and idx in said:
                by_person[key] = said[idx].upper()
            elif idx is None and any(len(f) >= 3 and f not in GENERIC_TITLES and _said(f, spoken)
                                     for f in (_norm(b.speaker), strip_honorifics(b.speaker))):
                by_person[key] = b.speaker.upper()  # not in the bible, but someone says the name
            else:
                n += 1
                by_person[key] = f"{prefix}SPEAKER_{n}"
        labels[b.speaker] = by_person[key]
    return labels, said


def _visual_rewriter(cast: list[dict], said: dict[int, str], lines: list, labels: dict[str, str]):
    """Names in VISUAL/SOUND rows: kept if said aloud in the clip, else the appearance descriptor, else the
    person's speaker label (if they talk in the clip), else 'someone'. One regex pass, longest name first."""
    speaks: dict[int, str] = {}
    for b in lines:
        idx = _member_of(b.speaker, cast)
        if idx is not None:
            speaks.setdefault(idx, labels[b.speaker])
    repl: dict[str, str | None] = {}
    for i, m in enumerate(cast):
        sub = None if i in said else (f"the {m['desc']}" if m["desc"] else speaks.get(i, "someone"))
        for f in m["names"]:
            repl.setdefault(f, sub)
    if not repl:
        return lambda text: text
    pat = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(re.escape(f) for f in sorted(repl, key=len, reverse=True))
                     + r")(?![A-Za-z0-9])", re.I)

    def rewrite(text: str) -> str:
        out = pat.sub(lambda mt: repl.get(_norm(mt.group(0))) or mt.group(0), text)
        out = re.sub(r"\b(the|a|an) the\b", "the", out, flags=re.I)
        return re.sub(r"(^|[.!?]\s+)(the|someone)\b", lambda mt: mt.group(1) + mt.group(2).capitalize(), out)
    return rewrite


def render_clip(show: Show, cand: dict, cast: list[dict] | None = None, prefix: str = "",
                hook_s: float = 3.0) -> tuple[str, list[dict]]:
    """What a stranger experiences, row by row, with seconds from the start of the clip. Rows are numbered
    from [0]; the hook-window and CUT markers carry no number. Returns (text, row_map) where row_map[n] =
    {"seg": segment index, "id": block ID} for row [n]."""
    cast = cast or []
    picked: list[tuple[int, float, object]] = []  # (segment, clip time, block)
    seg_start = [0.0]
    for i, s in enumerate(cand["segments"]):
        for b in show.ep(s["ep"]).blocks_overlapping(s["start"], s["end"]):
            if b.kind == "line" and (b.start < s["start"] - EPS or b.end > s["end"] + EPS):
                continue
            if b.kind == "shot" and b.synthetic:
                continue
            picked.append((i, seg_start[-1] + max(b.start, s["start"]) - s["start"], b))
        seg_start.append(seg_start[-1] + s["end"] - s["start"])
    lines = [b for _, _, b in picked if b.kind == "line"]
    labels, said = speaker_map(lines, cast, prefix)
    visual = _visual_rewriter(cast, said, lines, labels)

    out = [f"CLIP: {cand['duration']:.1f} s, {len(cand['segments'])} segment(s)"]
    row_map: list[dict] = []
    hook_done = False

    def mark_hook(t: float) -> None:
        nonlocal hook_done
        if not hook_done and t >= hook_s - 1e-6:
            out.append(_hook_marker(hook_s))
            hook_done = True

    def cut_to(seg: int) -> None:
        mark_hook(seg_start[seg])
        out.append(CUT)

    cur = 0
    for i, t, b in picked:
        while cur < i:  # one CUT for every segment boundary crossed
            cur += 1
            cut_to(cur)
        mark_hook(t)
        if b.kind == "line":
            emo = f" ({', '.join(b.emotions)})" if b.emotions else ""
            vo = " (voice-over)" if "V.O." in b.modifiers else " (off-screen)" if "O.S." in b.modifiers else ""
            body = f'LINE {labels[b.speaker]}{vo}{emo}: "{b.text}"'
        elif b.kind == "sfx":
            body = "SOUND: " + visual(b.text)
        else:  # shot description or action
            body = "VISUAL: " + visual(b.text)
        out.append(f"[{len(row_map)}] {t:4.1f}s {body}")
        row_map.append({"seg": i, "id": b.id})
    while cur < len(cand["segments"]) - 1:  # trailing segments with no rows still get their CUT
        cur += 1
        cut_to(cur)
    if not hook_done:
        out.append(_hook_marker(hook_s))
    return "\n".join(out), row_map


def clip_sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def offline_judge(show: Show, cand: dict) -> JudgeVerdict:
    """No-LLM baseline: keyword intensity of the first 3 s (H) and of the final beat (C)."""
    first3 = 0.0
    off = 0.0
    for s in cand["segments"]:
        ep = show.ep(s["ep"])
        for b in ep.beats:
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


def _row(row_map: list[dict], n) -> dict | None:
    return row_map[n] if isinstance(n, int) and 0 <= n < len(row_map) else None


def cold_judge(llm: LLM, show: Show, cand: dict, cfg: dict, cast: list[dict] | None) -> dict:
    text, row_map = render_clip(show, cand, cast, hook_s=cfg["promo"]["hook_max_s"])
    res = ask(llm, cfg, "judge", JudgeVerdict, lambda: offline_judge(show, cand), user_vars={"clip": text})
    v = res.model_dump()
    # the prompt's rule, enforced in code: a clip that answers itself or raises no question is not a cliffhanger
    rule = ("answered_in_clip" if v["answered_in_clip"]
            else "empty_open_question" if not v["open_question"].strip() else None)
    if rule and v["cliffhanger_score"] > 2.0:
        v["clash"] = {"rule": rule, "cliffhanger_score_given": v["cliffhanger_score"]}
        llm.log_event({"stage": "judge", "event": "judge_clash", "cand_id": cand.get("cand_id"), "rule": rule,
                       "cliffhanger_score_given": v["cliffhanger_score"], "clamped_to": 2.0})
        v["cliffhanger_score"] = 2.0
    # the label picks the repair; keep it consistent with the judge's own evidence
    if v["answered_in_clip"] and v["main_failure"] == "none":
        v["main_failure"] = "answered_in_clip"
    if not v["open_question"].strip() and v["main_failure"] == "none":
        v["main_failure"] = "no_open_question"
    if v["main_failure"] != "needs_context":
        v["missing_entity"] = None
    for key, n in (("resolution", v.get("resolution_line")), ("best_hook", v.get("best_hook_line"))):
        r = _row(row_map, n)
        v[f"{key}_id"], v[f"{key}_seg"] = (r["id"], r["seg"]) if r else (None, None)
    P, pace_info = pace(show, cand)
    v["pace"] = P
    v["pace_info"] = pace_info
    v["score"] = weighted_score(v, P, cfg["score"]["weights"])
    v["clip_sha"] = clip_sha(text)
    v.update(provenance(llm, cfg, "judge"))
    return v


def passes(v: dict, cfg: dict) -> bool:
    """Loop-stop 'pass', from config thresholds only: the weighted score clears min_score and every score
    clears its floor in score.pass_floors. main_failure plays no part (it only picks the repair)."""
    S = cfg["score"]
    return v["score"] >= S["min_score"] and all(v[k] >= floor for k, floor in (S.get("pass_floors") or {}).items())


def hard_fail(v: dict, cfg: dict) -> bool:
    """Never eligible: the judge's own evidence says there is no open cliffhanger."""
    return bool(v["answered_in_clip"]) or not v["open_question"].strip()
