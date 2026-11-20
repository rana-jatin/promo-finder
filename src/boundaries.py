"""Tier 1 / tier 2 boundary rules. Pure functions over a parsed Episode: code owns every timestamp.

Tier 1 (default): cut on cell (shot) boundaries, never inside a dialogue line.
Tier 2 (fallback, only when tier 1 breaks the 3s hook rule or the 1.5s cliffhanger rule):
    trim mid-shot, only where no line overlaps the trim point
      start = max(prev_line_end, first_beat_start - pre_pad)
      end   = min(next_line_start, last_beat_end + post_pad), snapped to the shot end when it is
              under snap_to_shot_end_s away and no line starts before it
Flash-frame guard: no shot fragment under flash_min_s at either clip edge.
"""
from __future__ import annotations

from dataclasses import dataclass

from .parse import EPS, Block, Episode


@dataclass
class Cut:
    t: float
    tier: int
    note: str = ""


def _r(t: float) -> float:
    return round(t + 0.0, 2)


def tier1_start(ep: Episode, t: float) -> float:
    start = ep.shot_at(t).start
    for _ in range(100):
        crossing = ep.lines_crossing(start)
        if not crossing:
            break
        start = ep.shot_at(min(ln.start for ln in crossing)).start
    return _r(start)


def tier1_end(ep: Episode, t: float) -> float:
    end = ep.shot_ending_at(t).end
    for _ in range(100):
        crossing = ep.lines_crossing(end)
        if not crossing:
            break
        end = ep.shot_ending_at(max(ln.end for ln in crossing)).end
    return _r(end)


def tier2_start(ep: Episode, anchor: float, P: dict) -> float | None:
    """Mid-shot start just before `anchor` (the start of the first beat that must be shown)."""
    t = min(max(ep.prev_line_end(anchor), anchor - P["pre_pad_s"], 0.0), anchor)
    if ep.lines_crossing(t):
        return None
    if ep.is_cut(t):
        return _r(t)
    cell = ep.shot_at(t)
    if cell.end - t < P["flash_min_s"]:  # opening sliver would flash: start earlier if still clean
        t2 = max(cell.start, cell.end - P["flash_min_s"])
        if ep.lines_crossing(t2):
            return None
        t = t2
    return _r(t)


def tier2_end(ep: Episode, t_end: float, P: dict) -> float | None:
    """Mid-shot end just after `t_end` (the end of the cliffhanger beat)."""
    nxt = min((ln.start for ln in ep.lines if ln.start >= t_end - EPS and ln.end > t_end + EPS),
              default=ep.duration)
    t = max(min(nxt, t_end + P["post_pad_s"]), t_end)
    if ep.is_cut(t):
        return _r(t)
    cell = ep.shot_ending_at(t)
    # flash-frame guard: if the shot ends within snap_to_shot_end_s and no line starts first, play it out
    if cell.end - t < P["snap_to_shot_end_s"] and nxt >= cell.end - EPS:
        t = cell.end
    elif t - cell.start < P["flash_min_s"] and cell.start >= t_end - EPS:
        t = cell.start  # the final shot would only flash: end on the cut before it
    if ep.lines_crossing(t):
        return None
    return _r(t)


def snap_start(ep: Episode, anchor: float, P: dict, hook_t: float | None = None) -> Cut:
    """Start of a segment whose first beat starts at `anchor`. If `hook_t` is given, the hook must
    land within hook_max_s of the returned start."""
    s1 = tier1_start(ep, anchor)
    if hook_t is None or hook_t - s1 <= P["hook_max_s"] + EPS:
        return Cut(s1, 1)
    s2 = tier2_start(ep, anchor, P)
    if s2 is not None and hook_t - s2 <= P["hook_max_s"] + EPS:
        return Cut(s2, 2)
    # nearest legal cut that satisfies the hook rule: start on the hook itself
    s3 = tier1_start(ep, hook_t)
    if hook_t - s3 <= P["hook_max_s"] + EPS:
        return Cut(s3, 1, "start_moved_to_hook")
    s4 = tier2_start(ep, hook_t, P)
    if s4 is not None:
        return Cut(s4, 2, "start_moved_to_hook")
    return Cut(s1, 1, "hook_late")


def snap_end(ep: Episode, t_end: float, P: dict, enforce_tail: bool = True) -> Cut:
    """End of a segment whose last beat ends at `t_end`."""
    e1 = tier1_end(ep, t_end)
    if not enforce_tail or e1 - t_end <= P["cliff_max_tail_s"] + EPS:
        return Cut(e1, 1)
    e2 = tier2_end(ep, t_end, P)
    if e2 is not None:
        return Cut(e2, 2)
    return Cut(e1, 1, "cliff_tail_long")


def flash_window(ep: Episode, beat: Block, P: dict) -> tuple[float, float] | None:
    """A 2-4s window that shows `beat` (cold-open flash), cut cleanly on both sides."""
    lo, hi = P["cold_open_flash"]
    cell = ep.shot_at(beat.start)
    # A: the beat's own shot, merged forward with following shots if it is too short
    a, b = cell.start, cell.end
    while b - a < lo - EPS and b < ep.duration - EPS:
        b = ep.shot_at(b + EPS).end
    if lo - EPS <= b - a <= hi + EPS and not ep.lines_crossing(a) and not ep.lines_crossing(b) \
            and b >= min(beat.end, a + hi) - EPS:
        return _r(a), _r(b)
    # B: trimmed window starting just before the beat. Candidate ends are shot cuts, padded line ends
    # (clamped to the next line's start, as in tier2_end), line starts, and the 2 s minimum, all
    # filtered by the same rules as any other cut: no line crossing, no flash-frame fragment.
    starts = []
    s2 = tier2_start(ep, beat.start, P)
    if s2 is not None and beat.start - s2 <= 1.0:
        starts.append(s2)
    if not ep.lines_crossing(cell.start) and beat.start - cell.start <= 1.0:
        starts.append(cell.start)
    for s in starts:
        points = {c for c in ep.cut_points()}
        for ln in ep.lines:
            points.add(min(ln.end + P["post_pad_s"], ep.next_line_start(ln.end + EPS)))
            points.add(ln.start)
        points |= {s + lo, beat.end + P["post_pad_s"]}
        clean = sorted({_r(p) for p in points
                        if s + lo - EPS <= p <= min(s + hi, ep.duration) + EPS and not ep.lines_crossing(p)
                        and (ep.is_cut(p) or p - ep.shot_ending_at(p).start >= P["flash_min_s"] - EPS)})
        if clean:
            covering = [p for p in clean if p >= beat.end - EPS]
            return _r(s), _r(covering[0] if covering else clean[-1])
    return None


def legal_near(ep: Episode, t: float, P: dict, side: str) -> float:
    """Nearest legal cut to an editor-nudged time: a shot boundary within 0.5s wins, else a
    tier-2 point that does not cut through a line."""
    cuts = [c for c in ep.cut_points() if abs(c - t) <= 0.5]
    if cuts:
        return _r(min(cuts, key=lambda c: abs(c - t)))
    if not ep.lines_crossing(t):
        return _r(t)
    crossing = ep.lines_crossing(t)
    if side == "start":
        return _r(max(0.0, min(ln.start for ln in crossing) - P["pre_pad_s"]))
    return _r(min(ep.duration, max(ln.end for ln in crossing) + P["post_pad_s"]))
