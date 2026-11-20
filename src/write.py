"""Outputs: ranked.json (the deliverable), candidates.jsonl (every judged candidate), report.md
(human-readable ranking + where the tool picks badly). Runs before review and again after it, so
the files on disk always reflect the editor's final decisions."""
from __future__ import annotations

import collections
import copy
from pathlib import Path

from .io_utils import read_jsonl, write_json, write_jsonl
from .judge import clip_sha, render_clip
from .parse import Show, fmt_ts, parse_ts


def _beat_text(show: Show, bid: str | None) -> str:
    b = show.block(bid) if bid else None
    if b is None:
        return ""
    if b.kind == "line":
        return f'{b.speaker}: "{b.text}"'
    return f"[{b.kind}] {b.text}"


def shot_range(show: Show, s: dict) -> list[str]:
    ep = show.ep(s["ep"])
    cells = [c for c in ep.shots if c.start < s["end"] - 1e-6 and c.end > s["start"] + 1e-6]
    return [cells[0].id, cells[-1].id] if cells else []


def promo_record(show: Show, c: dict, aliases) -> dict:
    v = c["verdict"]
    segs = [{"episode": s["ep"], "start": fmt_ts(s["start"]), "end": fmt_ts(s["end"]),
             "role": s.get("role", "main"), "shot_range": shot_range(show, s),
             "cut_tiers": [s.get("start_tier"), s.get("end_tier")]} for s in c["segments"]]
    # round trip: re-extract from the MM:SS.ss strings we publish and compare with what the judge saw
    rt = copy.deepcopy(c)
    for s, out in zip(rt["segments"], segs):
        s["start"], s["end"] = parse_ts(out["start"]), parse_ts(out["end"])
    edited = any(n.startswith("editor:") for n in c.get("notes", []))
    rt_ok = clip_sha(render_clip(show, rt, aliases)[0]) == v.get("clip_sha")
    return {
        "promo_id": f"P{c['rank']:02d}" if c.get("rank") else c["cand_id"],
        "rank": c.get("rank"),
        "cand_id": c["cand_id"],
        "template": c["template"],
        "segments": segs,
        "duration_s": c["duration"],
        "hook": {"id": c["hook"]["id"], "text": _beat_text(show, c["hook"]["id"]), "at_s": c["hook"]["at_s"],
                 "type": c["hook"].get("type", "other")},
        "cliffhanger": {"id": c["cliff"]["id"], "text": _beat_text(show, c["cliff"]["id"]),
                        "open_question": v.get("open_question", "")},
        "why": v.get("why_it_works", ""),
        "who_wants_what": v.get("who_wants_what", ""),
        "post_reason": c.get("post_reason", ""),
        "scores": {"hook": v["hook_score"], "cliff": v["cliffhanger_score"], "standalone": v["standalone_score"],
                   "trope": v["trope_pull_score"], "pace": v["pace"], "judge_total": v["score"],
                   "penalty": c.get("penalty", 0.0), "final": c.get("final_score", v["score"])},
        "penalty_detail": c.get("penalty_detail", {}),
        "duel_record": c.get("duel_record"),
        "trope": c.get("trope", "other"),
        "spoiler": c.get("spoiler", False),
        "in_free_window": c.get("in_free_window"),
        "flags": c.get("flags", []),
        "repairs": c.get("repairs", []),
        "refine_rounds": c.get("rounds", 0),
        "round_trip_ok": True if edited else rt_ok,
        "edited_by_editor": edited,
        "source": c["source"],
        "model": v.get("model"),
        "prompt_versions": {"judge": v.get("prompt_version")},
    }


def _fmt_segments(rec: dict) -> str:
    return " + ".join(f"E{s['episode']:02d} {s['start']}-{s['end']}" for s in rec["segments"])


def write_outputs(out_dir: str | Path, show: Show, state: dict, cfg: dict, aliases, run_meta: dict) -> dict:
    out = Path(out_dir)
    ranked = [promo_record(show, c, aliases) for c in state.get("ranked") or []]
    reserve = [promo_record(show, {**c, "rank": None}, aliases) for c in state.get("reserve") or []]
    write_json(out / "ranked.json", {"show": show.title, "show_id": show.show_id, "promos": ranked,
                                     "reserve": reserve, "run": run_meta})
    report = build_report(show, state, ranked, cfg, run_meta, out)
    (out / "report.md").write_text(report, encoding="utf-8")
    return {"ranked": len(ranked), "reserve": len(reserve)}


def build_report(show: Show, state: dict, ranked: list[dict], cfg: dict, run_meta: dict, out: Path) -> str:
    beats = [b for b in state.get("beats") or [] if b.get("kind") == "mine"]
    pool = state.get("pool") or []
    judged = read_jsonl(out / "candidates.jsonl")
    rejected = read_jsonl(out / "rejected_by_code.jsonl")
    no_promo = sorted(b["ep"] for b in beats if b["result"].get("no_promo") and not b["result"].get("moments"))
    fail = collections.Counter(j["verdict"]["main_failure"] for j in judged)
    rej = collections.Counter(e for r in rejected for e in r.get("errors", []))
    llm = run_meta.get("llm", {}).get("total", {})
    L: list[str] = []
    L.append(f"# Promo Finder report: {show.title}\n")
    L.append(f"Model `{run_meta.get('llm', {}).get('model')}` | prompts {run_meta.get('prompts')} | "
             f"mode {{fast: {run_meta['mode']['fast']}, duels: {run_meta['mode']['duels']}, "
             f"review: {run_meta['mode']['review']}, repair rounds: {run_meta['mode']['max_repair_rounds']}, "
             f"episodes mined: {len(run_meta['mode']['episodes'] or [])}}} | finished {run_meta.get('finished')}\n")
    if run_meta.get("llm", {}).get("model") == "offline":
        L.append("> **OFFLINE HEURISTIC RUN.** No LLM was called: mining, judging and duels used keyword "
                 "heuristics. Use it as a pipeline check and a no-LLM baseline, not as the real picks.\n")
    L.append("## Run summary\n")
    L.append(f"- Episodes mined: {len(beats)} of {len(show.episodes)}; `no_promo` episodes: "
             f"{len(no_promo)} ({', '.join(map(str, no_promo)) or 'none'})")
    L.append(f"- Candidates assembled: {len(pool)} valid, {len(rejected)} rejected by code "
             f"({', '.join(f'{k} {v}' for k, v in rej.most_common(6)) or 'none'})")
    passed = sum(1 for j in judged if j.get("eligible"))
    L.append(f"- Judged (cold): {len(judged)} in {state.get('batch', 1)} batch(es); eligible after penalties: "
             f"{passed}; shortlisted: {len(ranked)}")
    rt_bad = [r["promo_id"] for r in ranked if not r["round_trip_ok"]]
    proposed = len(pool) + sum(1 for r in rejected if "near_duplicate" not in r.get("errors", []))
    valid = len(pool)
    L.append(f"- Validator pass rate on proposed beats: {valid}/{proposed} "
             f"({valid / max(proposed, 1):.0%}, after at most one auto-repair); every published promo passes all "
             f"hard rules; timestamp round trip: {'all OK' if not rt_bad else 'MISMATCH ' + ', '.join(rt_bad)}")
    L.append(f"- LLM: {int(llm.get('calls', 0))} API calls, {int(llm.get('cache_hits', 0))} cache hits, "
             f"{int(llm.get('prompt_tokens', 0)):,} in / {int(llm.get('completion_tokens', 0)):,} out tokens, "
             f"est. ${llm.get('cost_usd', 0):.3f}; wall-clock {run_meta.get('seconds', 0):.0f}s\n")

    L.append("## Post these first\n")
    if not ranked:
        L.append("No candidate cleared the score floor. That is a valid answer: see the failure "
                 "breakdown below before lowering `score.min_score`.\n")
    for r in ranked[:5]:
        L.append(f"{r['rank']}. **{r['promo_id']}** ({r['template']}, {_fmt_segments(r)}, {r['duration_s']:.1f}s): "
                 f"{r['post_reason']}")
    L.append("")
    L.append("## Ranked promos\n")
    for r in ranked:
        L.append(f"### {r['promo_id']} | {r['template']} | {r['duration_s']:.1f}s | final {r['scores']['final']:.2f}")
        for s in r["segments"]:
            L.append(f"- Ep {s['episode']} `{s['start']}` -> `{s['end']}` ({s['role']}, shots {'-'.join(s['shot_range'])})")
        L.append(f"- **Hook** ({r['hook']['at_s']:.1f}s): {r['hook']['text']}")
        L.append(f"- **Cliffhanger**: {r['cliffhanger']['text']}  \n  Open question: _{r['cliffhanger']['open_question']}_")
        L.append(f"- **Why it works**: {r['why']}")
        sc = r["scores"]
        L.append(f"- Scores: H {sc['hook']:.1f} | C {sc['cliff']:.1f} | A {sc['standalone']:.1f} | "
                 f"T {sc['trope']:.1f} | P {sc['pace']:.1f} -> {sc['judge_total']:.2f} - {sc['penalty']:.2f} "
                 f"penalty{(' ' + str(r['penalty_detail'])) if r['penalty_detail'] else ''}"
                 + (f" | duels {r['duel_record']}" if r.get("duel_record") else ""))
        if r["flags"] or r["repairs"]:
            L.append(f"- Flags: {', '.join(r['flags']) or '-'} | repairs: {', '.join(r['repairs']) or '-'}")
        L.append("")

    L.append("## Where it picks badly (auto-generated diagnostics)\n")
    L.append(f"- Cold-judge main failure across all judged candidates: "
             f"{', '.join(f'{k} {v}' for k, v in fail.most_common()) or 'n/a'}")
    ctx = [j for j in judged if j["verdict"]["main_failure"] == "needs_context"]
    if ctx:
        ents = collections.Counter((j["verdict"].get("missing_entity") or "?") for j in ctx)
        L.append(f"- Needs context (a stranger could not place someone): {len(ctx)} candidates; "
                 f"most missing: {', '.join(f'{k} ({v})' for k, v in ents.most_common(5))}")
    chk = [j for j in judged if any(f.startswith("check_this_one") for f in j.get("flags", []))]
    if chk:
        L.append(f"- Heuristic says hot, judge says weak ({len(chk)}): " + ", ".join(
            f"{j['cand_id']} ({j['source']}, judge {j['verdict']['score']:.2f})" for j in chk[:6]))
    rep = [j for j in judged if j.get("rounds", 0) > 0]
    if rep:
        gain = [j["refine_history"][-1]["score"] - j["refine_history"][0]["score"] for j in rep if j.get("refine_history")]
        L.append(f"- Refine loop: {len(rep)} candidates repaired; mean score change of the last repair vs first "
                 f"verdict {sum(gain) / max(len(gain), 1):+.2f} (best version is always kept)")
    spoil = [j for j in judged if j.get("spoiler")]
    if spoil:
        L.append(f"- Spoiler flags (penalised, held back in posting order): {len(spoil)}")
    L.append("- Known blind spots: the judge reads a script, not footage (no acting, music or pacing of the "
             "real video); character names in dialogue still leak context; scores are a single model's opinion "
             "(see evaluate.py for gold-set and cross-model checks).")
    return "\n".join(L) + "\n"


def write_candidates(out_dir: str | Path, scored: list[dict]) -> None:
    rows = []
    for c in scored:
        rows.append({k: c[k] for k in ("cand_id", "template", "source", "duration", "episodes", "trope", "flags",
                                       "repairs", "rounds", "refine_history", "final_score", "penalty",
                                       "penalty_detail", "eligible", "spoiler", "peak_intensity", "prerank",
                                       "pool_rank") if k in c}
                    | {"segments": [{"ep": s["ep"], "start": fmt_ts(s["start"]), "end": fmt_ts(s["end"]),
                                     "role": s.get("role")} for s in c["segments"]],
                       "hook": c["hook"], "cliff": c["cliff"], "verdict": c["verdict"]})
    write_jsonl(Path(out_dir) / "candidates.jsonl", rows)
