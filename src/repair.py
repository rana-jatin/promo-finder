"""Loop 1 repairs: deterministic code driven by the cold judge's main_failure. v1 scope is the three
repairs that are reliable without new story knowledge; context problems are flagged, not patched.

  hook_late / hook_weak            flash the judge's best_hook row as a cold open, else tighten the
                                   start onto the hook, else flash the clip's peak beat
  answered_in_clip / no_open_q     pull the end back before the row the judge says answers it
                                   (else to the last "open" line) and re-snap
  too_short                        let the build-up start earlier behind a cold-open flash
  needs_context / unclear_stakes   not repairable in v1: flagged needs_context:<entity>
Every repair re-runs the full validator suite; an invalid repair counts as unrepairable.
"""
from __future__ import annotations

import copy

from .assemble import extend_as_cold_open, refresh, tighten_to_hook, validate
from .boundaries import snap_end
from .features import block_intensity
from .parse import EPS, Show


def _in_clip(cand: dict, show: Show, bid: str | None) -> bool:
    b = show.block(bid) if bid else None
    return bool(b) and any(s["ep"] == b.ep and s["start"] - EPS <= b.start < s["end"] for s in cand["segments"])


def _peak_block(show: Show, cand: dict, P: dict) -> str | None:
    """Most intense line/action/sfx in the clip that is neither in the first 3 s nor the final beat."""
    best, best_w, off = None, 0.0, 0.0
    cliff = show.block(cand["cliff"]["id"])
    for s in cand["segments"]:
        ep = show.ep(s["ep"])
        for b in [*ep.lines, *ep.actions, *ep.sfx]:
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
        t = ln.text.rstrip()
        if t.endswith(("?", "!", "...", "—", "-")) or block_intensity(ln)[0] >= 0.75:
            return ln.id
    return None


def repair(show: Show, feats: dict, cand: dict, verdict: dict, cfg: dict) -> dict | None:
    P = cfg["promo"]
    f = verdict["main_failure"]
    tries: list = []
    if f in ("hook_late", "hook_weak"):
        bh = verdict.get("best_hook_id")
        if bh and _in_clip(cand, show, bh) and bh != cand["cliff"]["id"]:
            tries.append(lambda: extend_as_cold_open(show, feats, cand, cfg, flash_id=bh))
        if cand["hook"].get("at_s", 0) > 1.0:
            tries.append(lambda: tighten_to_hook(show, feats, cand, cfg))
        pk = _peak_block(show, cand, P)
        if pk:
            tries.append(lambda: extend_as_cold_open(show, feats, cand, cfg, flash_id=pk))
    elif f in ("answered_in_clip", "no_open_question"):
        rid = verdict.get("resolution_id")
        if rid and _in_clip(cand, show, rid):
            tries.append(lambda: _end_before(show, feats, cand, rid, cfg))
        ol = _open_line_before_cliff(show, cand)
        if ol:
            nxt = next((x for x in show.ep(show.block(ol).ep).lines if x.start > show.block(ol).start + EPS), None)
            if nxt:
                tries.append(lambda: _end_before(show, feats, cand, nxt.id, cfg))
    elif f == "too_short":
        tries.append(lambda: extend_as_cold_open(show, feats, cand, cfg))
    for attempt in tries:
        new = attempt()
        if new is not None and not validate(show, new, cfg):
            new["repairs"] = cand.get("repairs", []) + [f]
            return new
    return None
