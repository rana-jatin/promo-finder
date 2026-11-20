"""Stage 4: candidate assembly, all in code.

Templates 1-3 come from mined beats, template 4 from linked stitches:
  continuous  one stretch, hook to cliffhanger
  cold_open   2-4 s flash of the escalation, then the build-up running through it to a FRESH beat
  bridge      end of Ep N + opening of Ep N+1, cut before the answer
  arc_stitch  2-3 segments from different episodes
Segments are kept in PLAYBACK order (a cold open is non-chronological by design).

Hard validators (run on every assembly, every repair and every editor nudge):
  20-60 s total, 1-4 segments, each >= 2 s, hook starts <= 3.0 s into the clip, clip ends <= 1.5 s
  after the cliffhanger beat, no cut inside a dialogue line, no flash frames at segment edges, every
  cited ID exists, and the MM:SS.ss round trip re-extracts exactly the lines the judge was shown.
"""
from __future__ import annotations

import copy
import hashlib
import json

from .boundaries import Cut, flash_window, legal_near, snap_end, snap_start, tier1_start
from .features import peak_intensity
from .parse import EPS, Show, ep_of, fmt_ts, parse_ts


# --------------------------------------------------------------------------- derived fields
def _tier(ep, t: float) -> int:
    return 1 if ep.is_cut(t) else 2


def _clip_time(cand: dict, ep_n: int, t: float) -> float | None:
    off = 0.0
    for s in cand["segments"]:
        if s["ep"] == ep_n and s["start"] - EPS <= t < s["end"] - EPS:
            return round(off + t - s["start"], 2)
        off += s["end"] - s["start"]
    return None


def refresh(show: Show, feats: dict, cand: dict) -> dict:
    """Recompute everything derived from the segments (call after any boundary change)."""
    segs = cand["segments"]
    for s in segs:
        s["start"], s["end"] = round(s["start"], 2), round(s["end"], 2)
    cand["duration"] = round(sum(s["end"] - s["start"] for s in segs), 2)
    cand["episodes"] = sorted({s["ep"] for s in segs})
    hb, cb = show.block(cand["hook"]["id"]), show.block(cand["cliff"]["id"])
    cand["hook"]["at_s"] = _clip_time(cand, hb.ep, hb.start) if hb else None
    last = segs[-1]
    if cb and cb.ep == last["ep"] and last["start"] - EPS <= cb.start and cb.end <= last["end"] + EPS:
        cand["cliff"]["tail_s"] = round(last["end"] - cb.end, 2)
    else:
        cand["cliff"]["tail_s"] = None
    cand["line_ids"] = [[ln.id for ln in show.ep(s["ep"]).lines_within(s["start"], s["end"])] for s in segs]
    cand["peak_intensity"] = peak_intensity(feats, show, segs) if feats else 0.0
    cand["prerank"] = round(cand.get("confidence", 0.5) * (0.5 + 0.5 * cand["peak_intensity"]), 4)
    return cand


def validate(show: Show, cand: dict, cfg: dict) -> list[str]:
    P = cfg["promo"]
    errs: list[str] = []
    segs = cand["segments"]
    if not 1 <= len(segs) <= P["max_segments"]:
        errs.append("segment_count")
    for s in segs:
        if s["ep"] not in show.episodes:
            errs.append("unknown_episode")
            continue
        ep = show.ep(s["ep"])
        if not (-EPS <= s["start"] < s["end"] <= ep.duration + EPS):
            errs.append("segment_bounds")
            continue
        if s["end"] - s["start"] < P["min_segment"] - EPS:
            errs.append("segment_too_short")
        if ep.lines_crossing(s["start"]) or ep.lines_crossing(s["end"]):
            errs.append("cuts_inside_line")
        if not ep.is_cut(s["start"]) and ep.shot_at(s["start"]).end - s["start"] < P["flash_min_s"] - EPS:
            errs.append("flash_frame_start")
        if not ep.is_cut(s["end"]) and s["end"] - ep.shot_ending_at(s["end"]).start < P["flash_min_s"] - EPS:
            errs.append("flash_frame_end")
    if cand["duration"] < P["min_dur"] - EPS:
        errs.append("too_short")
    if cand["duration"] > P["max_dur"] + EPS:
        errs.append("too_long")
    at = cand["hook"].get("at_s")
    if at is None:
        errs.append("hook_not_in_clip")
    elif at > P["hook_max_s"] + EPS:
        errs.append("hook_late")
    tail = cand["cliff"].get("tail_s")
    if tail is None:
        errs.append("cliff_not_in_last_segment")
    elif tail > P["cliff_max_tail_s"] + EPS:
        errs.append("cliff_tail_long")
    for bid in (cand["hook"]["id"], cand["cliff"]["id"], cand.get("peak_id")):
        if bid and show.block(bid) is None:
            errs.append("unknown_id")
    if cand["template"] == "cold_open" or segs[0].get("role") == "flash":
        f = segs[0]
        lo, hi = P["cold_open_flash"]
        if not lo - EPS <= f["end"] - f["start"] <= hi + EPS:
            errs.append("flash_length")
        cb = show.block(cand["cliff"]["id"])
        if cb and cb.ep == f["ep"] and cb.end <= f["end"] + EPS:
            errs.append("cold_open_cliff_not_fresh")
    # round trip: MM:SS.ss strings must re-extract exactly the lines that were shown
    for s, ids in zip(segs, cand.get("line_ids") or []):
        a, b = parse_ts(fmt_ts(s["start"])), parse_ts(fmt_ts(s["end"]))
        if [ln.id for ln in show.ep(s["ep"]).lines_within(a, b)] != ids:
            errs.append("round_trip_mismatch")
    return sorted(set(errs))


# --------------------------------------------------------------------------- building
def _seg(ep_n: int, ep, start: float, end: float, role: str) -> dict:
    return {"ep": ep_n, "start": round(start, 2), "end": round(end, 2), "start_tier": _tier(ep, start),
            "end_tier": _tier(ep, end), "role": role}


def candidate_id(template: str, segments: list[dict]) -> str:
    key = json.dumps([template, [(s["ep"], round(s["start"], 2), round(s["end"], 2)) for s in segments]])
    return "C" + hashlib.sha1(key.encode()).hexdigest()[:8]


def build(show: Show, feats: dict, spec: dict, cfg: dict) -> tuple[dict | None, dict | None]:
    P = cfg["promo"]
    hook_b, cliff_b = show.block(spec["hook_id"]), show.block(spec["cliff_id"])
    if hook_b is None or cliff_b is None:
        return None, _reject(spec, ["unknown_id"])
    segs: list[dict] = []
    notes: list[str] = []
    hook_placed = False
    for s in spec["segs"]:
        ep = show.ep(s["ep"])
        if s.get("flash_id"):
            w = flash_window(ep, show.block(s["flash_id"]), P)
            if w is None:
                return None, _reject(spec, ["no_clean_flash_window"])
            segs.append(_seg(s["ep"], ep, w[0], w[1], "flash"))
            hook_placed = hook_placed or s["flash_id"] == spec["hook_id"]
            continue
        anchor = show.block(s["start_id"]).start if s.get("start_id") else float(s.get("start_t") or 0.0)
        need_hook = not hook_placed and hook_b.ep == s["ep"] and anchor - EPS <= hook_b.start
        st = snap_start(ep, anchor, P, hook_t=hook_b.start if need_hook else None)
        hook_placed = hook_placed or need_hook
        en = Cut(ep.duration, 1) if s.get("to_end") else snap_end(ep, show.block(s["end_id"]).end, P)
        notes += [n for n in (st.note, en.note) if n]
        if en.t <= st.t + EPS:
            return None, _reject(spec, ["empty_segment"])
        segs.append(_seg(s["ep"], ep, st.t, en.t, s.get("role", "main")))
    cand = {
        "cand_id": candidate_id(spec["template"], segs),
        "template": spec["template"], "source": spec["source"], "segments": segs,
        "hook": {"id": spec["hook_id"], "ep": hook_b.ep, "type": spec.get("hook_type", "other")},
        "cliff": {"id": spec["cliff_id"], "ep": cliff_b.ep},
        "peak_id": spec.get("peak_id"), "trope": spec.get("trope", "other"),
        "mined_question": spec.get("open_question", ""), "confidence": spec.get("confidence", 0.5),
        "why_mined": spec.get("why", ""), "notes": notes, "repairs": [],
    }
    refresh(show, feats, cand)
    errs = validate(show, cand, cfg)
    if errs:
        fixed = auto_repair(show, feats, cand, errs, cfg)
        if fixed is not None and not validate(show, fixed, cfg):
            return fixed, None
        return None, _reject(spec, errs, cand)
    return cand, None


def _reject(spec: dict, errs: list[str], cand: dict | None = None) -> dict:
    out = {"source": spec["source"], "template": spec["template"], "errors": errs,
           "hook_id": spec.get("hook_id"), "cliff_id": spec.get("cliff_id")}
    if cand:
        out["segments"] = [{"ep": s["ep"], "start": fmt_ts(s["start"]), "end": fmt_ts(s["end"])} for s in cand["segments"]]
        out["duration"] = cand["duration"]
    return out


# --------------------------------------------------------------------------- one auto-repair (nearest legal cut)
def tighten_to_hook(show: Show, feats: dict, cand: dict, cfg: dict) -> dict | None:
    P = cfg["promo"]
    c = copy.deepcopy(cand)
    hb = show.block(c["hook"]["id"])
    for s in c["segments"]:
        if s["role"] != "flash" and s["ep"] == hb.ep and s["start"] - EPS <= hb.start < s["end"]:
            st = snap_start(show.ep(s["ep"]), hb.start, P, hook_t=hb.start)
            if st.t > s["start"] + EPS:
                s["start"], s["start_tier"] = st.t, st.tier
                c["notes"] = c["notes"] + ["auto:start_tightened_to_hook"]
                return refresh(show, feats, c)
            return None
    return None


def extend_as_cold_open(show: Show, feats: dict, cand: dict, cfg: dict, flash_id: str | None = None) -> dict | None:
    """Flash the hook (or `flash_id`) first, then let the main segment start earlier so the clip
    reaches the duration floor with the build-up in context."""
    P = cfg["promo"]
    c = copy.deepcopy(cand)
    fb = show.block(flash_id or c["hook"]["id"])
    if fb is None:
        return None
    w = flash_window(show.ep(fb.ep), fb, P)
    if w is None:
        return None
    rest = [s for s in c["segments"] if s["role"] != "flash"]
    flash = _seg(fb.ep, show.ep(fb.ep), w[0], w[1], "flash")
    if len(rest) + 1 > P["max_segments"]:
        return None
    main = rest[0]
    need = P["min_dur"] + 2.0 - (w[1] - w[0]) - sum(s["end"] - s["start"] for s in rest[1:])
    if main["end"] - main["start"] < need:
        ep = show.ep(main["ep"])
        main["start"] = min(main["start"], tier1_start(ep, max(0.0, main["end"] - need)))
        main["start_tier"] = 1
    c["segments"] = [flash, *rest]
    c["hook"]["id"] = fb.id
    c["hook"]["ep"] = fb.ep
    c["peak_id"] = fb.id
    if c["template"] == "continuous":
        c["template"] = "cold_open"
    c["notes"] = c["notes"] + ["auto:cold_open_extension"]
    return refresh(show, feats, c)


def auto_repair(show: Show, feats: dict, cand: dict, errs: list[str], cfg: dict) -> dict | None:
    if "too_short" in errs:
        return extend_as_cold_open(show, feats, cand, cfg)
    if "hook_late" in errs or "too_long" in errs:
        return tighten_to_hook(show, feats, cand, cfg)
    return None


# --------------------------------------------------------------------------- specs from mined beats
def specs_from_beats(show: Show, beats: list[dict]) -> list[dict]:
    specs: list[dict] = []
    for rec in beats:
        res = rec["result"]
        if rec.get("kind") == "mine":
            n = rec["ep"]
            for k, m in enumerate(res.get("moments") or [], 1):
                base = {"source": f"mine:E{n:02d}:m{k}", "hook_type": m["hook_type"], "trope": m["trope"],
                        "open_question": m["open_question"], "confidence": m["confidence"],
                        "cliff_id": m["cliff_id"]}
                if m["template"] == "cold_open" and m.get("peak_id"):
                    specs.append({**base, "template": "cold_open", "hook_id": m["peak_id"], "peak_id": m["peak_id"],
                                  "segs": [{"ep": n, "flash_id": m["peak_id"], "role": "flash"},
                                           {"ep": n, "start_id": m["start_id"], "end_id": m["cliff_id"], "role": "main"}]})
                else:
                    specs.append({**base, "template": "continuous", "hook_id": m["hook_id"],
                                  "segs": [{"ep": n, "start_id": m["start_id"], "end_id": m["cliff_id"], "role": "main"}]})
            b = res.get("bridge")
            if b and rec.get("next_ep") is not None:
                nxt = rec["next_ep"]
                specs.append({"source": f"mine:E{n:02d}:bridge", "template": "bridge", "hook_type": "other",
                              "trope": b["trope"], "open_question": b["open_question"],
                              "confidence": b["confidence"], "hook_id": b.get("hook_id") or b["start_id"],
                              "cliff_id": b["next_cliff_id"],
                              "segs": [{"ep": n, "start_id": b["start_id"], "to_end": True, "role": "main"},
                                       {"ep": nxt, "start_id": b.get("next_start_id"), "start_t": 0.0,
                                        "end_id": b["next_cliff_id"], "role": "next"}]})
        elif rec.get("kind") == "link":
            for k, st in enumerate(res.get("stitches") or [], 1):
                specs.append({"source": f"link:{k}", "template": "arc_stitch", "hook_type": "other",
                              "trope": st["trope"], "open_question": st["open_question"],
                              "confidence": st["confidence"], "why": st.get("why", ""),
                              "hook_id": st["hook_id"], "cliff_id": st["cliff_id"],
                              "segs": [{"ep": ep_of(sg["start_id"]), "start_id": sg["start_id"],
                                        "end_id": sg["end_id"], "role": "stitch"} for sg in st["segments"]]})
    return specs


# --------------------------------------------------------------------------- overlap + pool
def _merged(cand: dict) -> dict[int, list[tuple[float, float]]]:
    by: dict[int, list[tuple[float, float]]] = {}
    for s in cand["segments"]:
        by.setdefault(s["ep"], []).append((s["start"], s["end"]))
    out = {}
    for ep, iv in by.items():
        iv.sort()
        m = [list(iv[0])]
        for a, b in iv[1:]:
            if a <= m[-1][1]:
                m[-1][1] = max(m[-1][1], b)
            else:
                m.append([a, b])
        out[ep] = [tuple(x) for x in m]
    return out


def iou(a: dict, b: dict) -> float:
    ma, mb = _merged(a), _merged(b)
    la = sum(e - s for iv in ma.values() for s, e in iv)
    lb = sum(e - s for iv in mb.values() for s, e in iv)
    inter = 0.0
    for ep in set(ma) & set(mb):
        for s1, e1 in ma[ep]:
            for s2, e2 in mb[ep]:
                inter += max(0.0, min(e1, e2) - max(s1, s2))
    union = la + lb - inter
    return inter / union if union > 0 else 0.0


def assemble_pool(show: Show, feats: dict, beats: list[dict], cfg: dict) -> tuple[list[dict], list[dict]]:
    pool: list[dict] = []
    rejected: list[dict] = []
    for spec in specs_from_beats(show, beats):
        cand, rej = build(show, feats, spec, cfg)
        if cand is None and spec["template"] == "cold_open" and "no_clean_flash_window" in rej["errors"]:
            # the peak cannot be flashed cleanly (e.g. a 5 s line): keep the beat as a continuous promo
            main = spec["segs"][-1]
            alt = {**spec, "template": "continuous", "source": spec["source"] + ":as_continuous",
                   "segs": [{**main, "role": "main"}]}
            cand, rej2 = build(show, feats, alt, cfg)
            rej = rej if cand is None else None
        if cand:
            pool.append(cand)
        else:
            rejected.append(rej)
    pool.sort(key=lambda c: (-c["prerank"], c["cand_id"]))
    kept: list[dict] = []
    for c in pool:
        dup = next((k for k in kept if k["cand_id"] == c["cand_id"] or iou(c, k) >= 0.8), None)
        if dup:
            rejected.append({"source": c["source"], "template": c["template"], "errors": ["near_duplicate"],
                             "duplicate_of": dup["cand_id"]})
            continue
        kept.append(c)
    for i, c in enumerate(kept, 1):
        c["pool_rank"] = i
    return kept, rejected


# --------------------------------------------------------------------------- editor nudges
def nudge(show: Show, feats: dict, cand: dict, changes: list[dict], cfg: dict) -> tuple[dict | None, list[str]]:
    """changes: [{"seg": 0, "start": "00:41.00" | -0.5, "end": ...}]. Each nudged edge snaps to the
    nearest legal cut, then the full validator suite runs again."""
    P = cfg["promo"]
    c = copy.deepcopy(cand)
    for ch in changes:
        i = int(ch.get("seg", 0))
        if not 0 <= i < len(c["segments"]):
            return None, ["bad_segment_index"]
        s = c["segments"][i]
        ep = show.ep(s["ep"])
        for side in ("start", "end"):
            if ch.get(side) is None:
                continue
            v = ch[side]
            t = parse_ts(v) if isinstance(v, str) and ":" in v else s[side] + float(v)
            s[side] = legal_near(ep, max(0.0, min(ep.duration, t)), P, side)
            s[f"{side}_tier"] = _tier(ep, s[side])
    c["notes"] = c["notes"] + ["editor:nudged"]
    refresh(show, feats, c)
    errs = validate(show, c, cfg)
    return (None, errs) if errs else (c, [])
