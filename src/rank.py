"""Stage 5B in code: penalties, eligibility, dedupe + diversity (select), Bradley-Terry over duels and
the post-first funnel order (order). The bible's spoiler list is only ever used here, in code."""
from __future__ import annotations

import math
import re
import statistics

from .assemble import iou
from .judge import hard_fail
from .parse import EPS, Show
from .judge import render_clip


# --------------------------------------------------------------------------- penalties
def penalties(show: Show, cand: dict, bible: dict, cfg: dict) -> tuple[float, dict, list[str]]:
    Z = cfg["penalties"]
    detail: dict[str, float] = {}
    flags: list[str] = []
    extra = max(0, len(cand["segments"]) - 2)
    if extra:
        detail["jump_cuts"] = round(extra * Z["per_extra_segment"], 3)
    mism = 0
    segs = cand["segments"]
    for a, b in zip(segs, segs[1:]):
        sa, sb = show.ep(a["ep"]).scene_at(a["end"] - EPS), show.ep(b["ep"]).scene_at(b["start"])
        if (a["ep"], sa.index) == (b["ep"], sb.index):
            continue
        if (sa.int_ext and sb.int_ext and sa.int_ext != sb.int_ext) or \
                (_tod(sa.time_of_day) and _tod(sb.time_of_day) and _tod(sa.time_of_day) != _tod(sb.time_of_day)):
            mism += 1
    if mism:
        detail["heading_mismatch"] = round(mism * Z["heading_mismatch"], 3)
    lo, hi = Z["sweet_spot"]
    if not lo <= cand["duration"] <= hi:
        detail["outside_sweet_spot"] = Z["outside_sweet_spot"]
    text = render_clip(show, cand)[0].lower()
    for sp in bible.get("spoilers") or []:
        if max(cand["episodes"]) >= int(sp.get("episode", 10 ** 6)) and any(
                k and re.search(r"(?<![a-z])" + re.escape(k.lower()), text) for k in sp.get("keywords") or []):
            detail["spoiler"] = Z["spoiler"]
            flags.append(f"spoiler:Ep{sp['episode']}")
            break
    return round(sum(detail.values()), 3), detail, flags


def _tod(t: str) -> str:
    t = (t or "").upper()
    return "NIGHT" if "NIGHT" in t or "EVENING" in t else "DAY" if "DAY" in t or "MORNING" in t else ""


# --------------------------------------------------------------------------- select
def score_all(show: Show, judged: list[dict], bible: dict, cfg: dict) -> list[dict]:
    out = []
    for j in judged:
        pen, detail, flags = penalties(show, j, bible, cfg)
        v = j["verdict"]
        final = round(v["score"] - pen, 3)
        eligible = final >= cfg["score"]["min_score"] and not hard_fail(v, cfg)
        fl = list(dict.fromkeys(j.get("flags", []) + flags))
        if v["main_failure"] == "needs_context":
            fl.append(f"needs_context:{v.get('missing_entity') or 'unknown'}")
        if j.get("peak_intensity", 0) >= 0.85 and v["score"] < cfg["score"]["min_score"]:
            fl.append("check_this_one:heuristic_high_judge_low")
        out.append({**j, "penalty": pen, "penalty_detail": detail, "final_score": final, "eligible": eligible,
                    "spoiler": any(f.startswith("spoiler:") for f in fl), "flags": list(dict.fromkeys(fl))})
    out.sort(key=lambda x: (-x["final_score"], x["cand_id"]))
    return out


def select(scored: list[dict], rejected_ids: list[str], cfg: dict) -> tuple[list[dict], list[dict]]:
    S = cfg["select"]
    picked: list[dict] = []
    per_ep: dict[int, int] = {}
    per_trope: dict[str, int] = {}
    rejected = set(rejected_ids)
    for c in scored:
        if not c["eligible"] or c["cand_id"] in rejected:
            continue
        if any(iou(c, p) > S["iou_dedupe"] for p in picked):
            continue
        ep = c["hook"]["ep"]
        trope = c.get("trope") or "other"
        if per_ep.get(ep, 0) >= S["max_per_episode"]:
            continue
        if trope != "other" and per_trope.get(trope, 0) >= S["max_per_trope"]:
            continue
        picked.append(c)
        per_ep[ep] = per_ep.get(ep, 0) + 1
        per_trope[trope] = per_trope.get(trope, 0) + 1
        if len(picked) >= S["shortlist"] + S["reserve"]:
            break
    return picked[: S["shortlist"]], picked[S["shortlist"]:]


# --------------------------------------------------------------------------- Bradley-Terry + order
def bradley_terry(ids: list[str], games: list[tuple[str, str]], iters: int = 200) -> dict[str, float]:
    """games: (winner, loser). MM updates (Hunter 2004) with one pseudo-win and one pseudo-loss
    against a phantom of strength 1, so unbeaten or winless items stay finite."""
    p = {i: 1.0 for i in ids}
    wins = {i: 0.0 for i in ids}
    pairs: dict[tuple[str, str], int] = {}
    for w, l in games:
        if w in p and l in p:
            wins[w] += 1
            k = tuple(sorted((w, l)))
            pairs[k] = pairs.get(k, 0) + 1
    for _ in range(iters):
        new = {}
        for i in ids:
            denom = 2.0 / (p[i] + 1.0)
            for (a, b), n in pairs.items():
                if i in (a, b):
                    j = b if i == a else a
                    denom += n / (p[i] + p[j])
            new[i] = (wins[i] + 1.0) / denom
        g = math.exp(statistics.fmean(math.log(v) for v in new.values())) if new else 1.0
        p = {k: v / g for k, v in new.items()}
    return p


def _z(xs: list[float]) -> list[float]:
    if len(xs) < 2:
        return [0.0 for _ in xs]
    m, sd = statistics.fmean(xs), statistics.pstdev(xs)
    return [0.0 if sd < 1e-9 else (x - m) / sd for x in xs]


def order(shortlist: list[dict], duels: list[dict], cfg: dict) -> list[dict]:
    R = cfg["rank"]
    if not shortlist:
        return []
    ids = [c["cand_id"] for c in shortlist]
    games = [(d["winner_id"], d["loser_id"]) for d in duels]
    record = {i: [0, 0] for i in ids}
    for w, l in games:
        if w in record:
            record[w][0] += 1
        if l in record:
            record[l][1] += 1
    if games:
        bt = bradley_terry(ids, games)
        base = _z([math.log(bt[i]) for i in ids])
    else:
        bt = {}
        base = _z([c["final_score"] for c in shortlist])
    ranked = []
    for c, b in zip(shortlist, base):
        in_window = max(c["episodes"]) <= R["free_window"]
        post = b + (R["funnel_boost"] if in_window else 0.0) - (R["spoiler_holdback"] if c.get("spoiler") else 0.0)
        ranked.append({**c, "bt_strength": round(bt.get(c["cand_id"], 0.0), 4) if bt else None,
                       "duel_record": f"{record[c['cand_id']][0]}-{record[c['cand_id']][1]}" if games else None,
                       "in_free_window": in_window, "post_score": round(post, 4)})
    ranked.sort(key=lambda x: (-x["post_score"], -x["final_score"]))
    for i, c in enumerate(ranked, 1):
        c["rank"] = i
        c["post_reason"] = rationale(c, ranked, cfg)
    return ranked


def rationale(c: dict, ranked: list[dict], cfg: dict) -> str:
    """One line generated from the score breakdown, e.g. strongest hook, lands at 0.4 s, in free window."""
    v = c["verdict"]
    bits = []
    hooks = sorted((x["verdict"]["hook_score"] for x in ranked), reverse=True)
    if v["hook_score"] >= hooks[0] - 1e-9:
        bits.append(f"strongest hook in the shortlist (H {v['hook_score']:.1f}) at {c['hook']['at_s']:.1f}s")
    else:
        bits.append(f"hook H {v['hook_score']:.1f} at {c['hook']['at_s']:.1f}s")
    bits.append(f"cliff C {v['cliffhanger_score']:.1f}: \"{v['open_question'][:70]}\"")
    if c.get("duel_record"):
        bits.append(f"duels {c['duel_record']}")
    eps = c["episodes"]
    ep_txt = f"Ep {eps[0]}" if len(eps) == 1 else f"Eps {eps[0]}-{eps[-1]}"
    fw = cfg["rank"]["free_window"]
    bits.append(f"{ep_txt}, inside the {fw}-episode free window" if c["in_free_window"]
                else f"{ep_txt}: a new viewer waits {eps[-1] - 1} episodes to reach it (outside the free window)")
    if c.get("spoiler"):
        bits.append("held back: touches a late twist")
    return "; ".join(bits)
