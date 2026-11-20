"""Stage 5B in code: penalties, eligibility, dedupe + diversity (select), Bradley-Terry over duels and
the post-first funnel order (order). The bible's spoiler list is only ever used here, in code."""
from __future__ import annotations

import math
import re
import statistics

from .assemble import iou
from .judge import hard_fail
from .parse import EPS, Show


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
    scenes = scene_texts(show, cand)
    for sp in bible.get("spoilers") or []:
        at = int(sp.get("episode", 10 ** 6))
        hit = next((k for k in sp.get("keywords") or [] for ep, text in scenes
                    if ep >= at and phrase_in(k, text)), None)
        if hit:
            detail["spoiler"] = Z["spoiler"]
            flags.append(f"spoiler:Ep{sp['episode']}")
            break
    return round(sum(detail.values()), 3), detail, flags


# words that carry no twist on their own: "faked his death" matches on {faked, death}
_STOP = {"a", "an", "the", "his", "her", "their", "its", "he", "she", "they", "him", "them", "of", "to", "in", "on",
         "at", "for", "and", "or", "as", "by", "with", "from", "is", "was", "be", "been", "has", "had", "have",
         "that", "this", "who", "whom", "it", "into", "s"}


def content_words(phrase: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", phrase.lower()) if w not in _STOP]


def phrase_in(phrase: str, text: str) -> bool:
    """A scene matches a spoiler phrase when ALL its content words appear. Each word matches at a word start,
    so 'father' also matches 'fathers' (but 'faked' does not match 'fake')."""
    words = content_words(phrase)
    return bool(words) and all(re.search(r"(?<![a-z0-9])" + re.escape(w), text) for w in words)


def scene_texts(show: Show, cand: dict) -> list[tuple[int, str]]:
    """(episode, lower-cased text) for each scene the clip shows: spoken lines with speakers, actions, sound and
    shot descriptions, limited to the clip's own time ranges."""
    out: dict[tuple[int, int], list[str]] = {}
    for s in cand["segments"]:
        for b in show.ep(s["ep"]).blocks_overlapping(s["start"], s["end"]):
            bits = [b.speaker, b.text] if b.kind == "line" else [b.text]
            out.setdefault((s["ep"], b.scene), []).extend(x for x in bits if x)
    return [(ep, " \n ".join(t).lower()) for (ep, _), t in out.items()]


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


def select(scored: list[dict], rejected_ids: list[str], cfg: dict,
           why: dict[str, int] | None = None) -> tuple[list[dict], list[dict]]:
    """Shortlist + reserve. `why`, if given, counts the eligible candidates held back and the reason."""
    S = cfg["select"]
    picked: list[dict] = []
    per_ep: dict[int, int] = {}
    per_trope: dict[str, int] = {}
    rejected = set(rejected_ids)
    held = why if why is not None else {}

    def hold(reason: str) -> None:
        held[reason] = held.get(reason, 0) + 1

    for k, c in enumerate(scored):
        if not c["eligible"]:
            continue
        if c["cand_id"] in rejected:
            hold("rejected by the editor")
            continue
        if any(iou(c, p) > S["iou_dedupe"] for p in picked):
            hold("overlaps a higher-ranked promo")
            continue
        ep = c["hook"]["ep"]
        trope = c.get("trope") or "other"
        if per_ep.get(ep, 0) >= S["max_per_episode"]:
            hold(f"over the {S['max_per_episode']}-per-episode cap")
            continue
        if trope != "other" and per_trope.get(trope, 0) >= S["max_per_trope"]:
            hold(f"over the {S['max_per_trope']}-per-trope cap")
            continue
        picked.append(c)
        per_ep[ep] = per_ep.get(ep, 0) + 1
        per_trope[trope] = per_trope.get(trope, 0) + 1
        if len(picked) >= S["shortlist"] + S["reserve"]:
            rest = sum(1 for x in scored[k + 1:] if x["eligible"])
            if rest:
                held["beyond shortlist + reserve"] = held.get("beyond shortlist + reserve", 0) + rest
            break
    return picked[: S["shortlist"]], picked[S["shortlist"]:]


def clip_text(text: str, n: int = 90) -> str:
    """Shorten at a word boundary with an ellipsis; never cut mid-word."""
    text = " ".join(str(text).split())
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0].rstrip(",;:.-")
    return (cut or text[:n]) + "…"


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
    bits.append(f"cliff C {v['cliffhanger_score']:.1f}: \"{clip_text(v['open_question'], 110)}\"")
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
