"""Loop 1 repairs: deterministic code driven by the cold judge's main_failure. REPAIRS is keyed on exactly the
judge's labels (schemas.FAILURES minus "none"); a test keeps the two in step.

  answered_in_clip / no_open_question   pull the end back before the row the judge says answers it
                                        (else to the last "open" line) and re-snap
  ends_on_replay                        the flash's moment is also the ending: run the main segment on to
                                        the next open line after the flash, within max_dur
  hook_late / hook_weak                 flash the judge's best_hook row as a cold open, else tighten the
                                        start onto the hook, else flash the clip's peak beat
  needs_context / unclear_stakes        not repairable without new story knowledge: flagged, not patched
Every repair re-runs the full validator suite; an invalid repair counts as unrepairable.
(A clip under 20 s never reaches the judge: assemble.auto_repair fixes that at build time.)
"""
from __future__ import annotations

import copy
from typing import Callable, Optional

from .assemble import extend_as_cold_open, refresh, tighten_to_hook, validate
from .boundaries import snap_end
from .features import block_intensity, ends_open
from .parse import EPS, Show


def _in_clip(cand: dict, show: Show, bid: str | None) -> bool:
    b = show.block(bid) if bid else None
    return bool(b) and any(s["ep"] == b.ep and s["start"] - EPS <= b.start < s["end"] for s in cand["segments"])


def _is_open(ln) -> bool:
    return ends_open(ln.text) or block_intensity(ln)[0] >= 0.75


def _peak_block(show: Show, cand: dict, P: dict) -> str | None:
    """Most intense line/action/sfx in the clip that is neither in the first 3 s nor the final beat."""
    best, best_w, off = None, 0.0, 0.0
    cliff = show.block(cand["cliff"]["id"])
    for s in cand["segments"]:
        ep = show.ep(s["ep"])
        for b in ep.beats:
            if not (s["start"] - EPS <= b.start < s["end"]):
                continue
            t = off + b.start - s["start"]
            if t <= P["hook_max_s"] or (cliff and b.id == cliff.id) or (cliff and b.ep == cliff.ep and b.end > cliff.start - 3.0):
                continue
            w = block_intensity(b)[0]
            if w > best_w:
                best, best_w = b.id, w
        off += s["end"] - s["start"]
    return best if best_w >= 0.5 else None


def _end_before(show: Show, feats: dict, cand: dict, block_id: str, cfg: dict) -> dict | None:
    """Cut the clip so it ends on the last line BEFORE block_id (drop any later segments)."""
    P = cfg["promo"]
    rb = show.block(block_id)
    c = copy.deepcopy(cand)
    for i, s in enumerate(c["segments"]):
        if s["ep"] == rb.ep and s["start"] - EPS <= rb.start < s["end"] and s["role"] != "flash":
            ep = show.ep(s["ep"])
            prev = [ln for ln in ep.lines if s["start"] - EPS <= ln.start and ln.end <= rb.start + EPS]
            if not prev:
                return None
            last = prev[-1]
            en = snap_end(ep, last.end, P)
            if en.t <= s["start"] + EPS or en.t >= s["end"] - EPS:
                return None
            s["end"], s["end_tier"] = en.t, en.tier
            c["segments"] = c["segments"][: i + 1]
            c["cliff"] = {"id": last.id, "ep": last.ep}
            return refresh(show, feats, c)
    return None


def _open_line_before_cliff(show: Show, cand: dict) -> str | None:
    s = cand["segments"][-1]
    ep = show.ep(s["ep"])
    cliff = show.block(cand["cliff"]["id"])
    for ln in reversed([x for x in ep.lines if s["start"] - EPS <= x.start and x.end <= s["end"] + EPS]):
        if cliff and ln.start >= cliff.start - EPS:
            continue
        if _is_open(ln):
            return ln.id
    return None


def _run_past_flash(show: Show, feats: dict, cand: dict, cfg: dict) -> dict | None:
    """End the last segment on the first open line that starts after the flash's moment, if it fits max_dur."""
    P = cfg["promo"]
    segs = cand["segments"]
    if len(segs) < 2 or segs[0].get("role") != "flash":
        return None
    flash, last = segs[0], segs[-1]
    ep = show.ep(last["ep"])
    cliff = show.block(cand["cliff"]["id"])
    after = flash["end"] if flash["ep"] == last["ep"] else last["start"]
    if cliff is not None and cliff.ep == last["ep"]:
        after = max(after, cliff.end)
    room = P["max_dur"] - (cand["duration"] - (last["end"] - last["start"]))
    for ln in ep.lines:
        if ln.start < after - EPS or ln.start < last["start"] - EPS:
            continue
        en = snap_end(ep, ln.end, P)
        if en.t - last["start"] > room + EPS:
            return None
        if not _is_open(ln) or en.t <= last["end"] + EPS:
            continue
        c = copy.deepcopy(cand)
        c["segments"][-1]["end"], c["segments"][-1]["end_tier"] = en.t, en.tier
        c["cliff"] = {"id": ln.id, "ep": ln.ep}
        c["notes"] = c["notes"] + ["repair:ran_past_flash"]
        return refresh(show, feats, c)
    return None


Attempts = list[Callable[[], Optional[dict]]]


def _cut_before_answer(show: Show, feats: dict, cand: dict, verdict: dict, cfg: dict) -> Attempts:
    tries: Attempts = []
    rid = verdict.get("resolution_id")
    if rid and _in_clip(cand, show, rid):
        tries.append(lambda: _end_before(show, feats, cand, rid, cfg))
    ol = _open_line_before_cliff(show, cand)
    if ol:
        nxt = next((x for x in show.ep(show.block(ol).ep).lines if x.start > show.block(ol).start + EPS), None)
        if nxt:
            tries.append(lambda: _end_before(show, feats, cand, nxt.id, cfg))
    return tries


def _fresh_ending(show: Show, feats: dict, cand: dict, verdict: dict, cfg: dict) -> Attempts:
    return [lambda: _run_past_flash(show, feats, cand, cfg)]


def _fix_hook(show: Show, feats: dict, cand: dict, verdict: dict, cfg: dict) -> Attempts:
    tries: Attempts = []
    bh = verdict.get("best_hook_id")
    if bh and _in_clip(cand, show, bh) and bh != cand["cliff"]["id"]:
        tries.append(lambda: extend_as_cold_open(show, feats, cand, cfg, flash_id=bh))
    if cand["hook"].get("at_s", 0) > 1.0:
        tries.append(lambda: tighten_to_hook(show, feats, cand, cfg))
    pk = _peak_block(show, cand, cfg["promo"])
    if pk:
        tries.append(lambda: extend_as_cold_open(show, feats, cand, cfg, flash_id=pk))
    return tries


REPAIRS: dict[str, Optional[Callable[..., Attempts]]] = {
    "answered_in_clip": _cut_before_answer,
    "ends_on_replay": _fresh_ending,
    "hook_late": _fix_hook,
    "hook_weak": _fix_hook,
    "no_open_question": _cut_before_answer,
    "needs_context": None,
    "unclear_stakes": None,
}
REPAIRABLE = frozenset(k for k, fn in REPAIRS.items() if fn is not None)


def repair(show: Show, feats: dict, cand: dict, verdict: dict, cfg: dict) -> dict | None:
    f = verdict["main_failure"]
    plan = REPAIRS.get(f)
    for attempt in (plan(show, feats, cand, verdict, cfg) if plan else []):
        new = attempt()
        if new is not None and not validate(show, new, cfg):
            new["repairs"] = cand.get("repairs", []) + [f]
            return new
    return None
