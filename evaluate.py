"""Quality proof against a hand-labelled gold set, plus judge reliability checks.

    python evaluate.py --gold eval/gold.json --run outputs/show1
    python evaluate.py --gold eval/gold.json --run outputs/show1 --run outputs/show1-r0   # ablation table
    python evaluate.py --run outputs/show1 --rejudge-model groq/openai/gpt-oss-120b      # cross-model tau
    python evaluate.py --run outputs/show1 --stability 3 --leak-test 5

Gold metrics (label the gold set BEFORE looking at tool output; see eval/gold.template.json):
  precision@k       share of top-k promos (inside labelled episodes) matching a gold promo, IoU >= 0.5
  candidate recall  share of gold promos matched by anything assembled (low = mining is the bottleneck)
  boundary error    median |start| and |end| error in seconds on matched pairs
  negative FP rate  share of top-k promos that touch an episode you labelled as having no good promo
Judge agreement (judged candidates inside labelled episodes; independent of which episodes the ranking favours):
  judge_pass_precision  share of judge-passed candidates that match a gold promo
  gold_pass_rate        share of gold-matching candidates the judge passed (low = judge too harsh)
  non_gold_reject_rate  share of non-gold candidates the judge rejected (low = judge too lenient)
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.assemble import iou, refresh
from src.bible import bible_brief, clip_cast
from src.config import load_config
from src.features import load_features
from src.io_utils import read_jsonl
from src.judge import cold_judge, render_clip
from src.llm import LLM, fill, load_prompt
from src.parse import load_show, parse_ts
from src.schemas import JudgeVerdict


def _segs(raw) -> list[dict]:
    out = []
    for s in raw:
        if isinstance(s, (list, tuple)):
            ep, a, b = s[0], s[1], s[2]
        else:
            ep, a, b = s.get("episode", s.get("ep")), s["start"], s["end"]
        out.append({"ep": int(ep), "start": parse_ts(a) if isinstance(a, str) else float(a),
                    "end": parse_ts(b) if isinstance(b, str) else float(b)})
    return out


def _best_match(g: dict, cands: list[dict]) -> tuple[float, dict | None]:
    best = (0.0, None)
    for c in cands:
        v = iou({"segments": g["segments"]}, {"segments": c["segments"]})
        if v > best[0]:
            best = (v, c)
    return best


def gold_metrics(gold: dict, run: Path, k: int, thr: float) -> dict:
    ranked = json.loads((run / "ranked.json").read_text(encoding="utf-8"))["promos"]
    top = [{"id": p["promo_id"], "segments": _segs(p["segments"])} for p in ranked[:k]]
    pool = [{"id": c["cand_id"], "segments": _segs(c["segments"])} for c in read_jsonl(run / "pool.jsonl")]
    judged = [{"id": c["cand_id"], "segments": _segs(c["segments"]), "eligible": bool(c.get("eligible"))}
              for c in read_jsonl(run / "candidates.jsonl")]
    golds = [{"id": g["id"], "segments": _segs(g["segments"])} for g in gold["promos"]]
    labelled = set(gold["labelled_episodes"])
    negative = set(gold.get("negative_episodes", []))

    in_scope = [p for p in top if {s["ep"] for s in p["segments"]} <= labelled]
    hits = [p for p in in_scope if any(iou({"segments": p["segments"]}, {"segments": g["segments"]}) >= thr for g in golds)]
    fp = [p for p in top if {s["ep"] for s in p["segments"]} & negative]
    rec_pool = [g for g in golds if _best_match(g, pool)[0] >= thr]
    rec_judged = [g for g in golds if _best_match(g, judged)[0] >= thr]
    errs_s, errs_e = [], []
    for g in golds:
        v, c = _best_match(g, pool)
        if c is not None and v >= thr:
            errs_s.append(abs(g["segments"][0]["start"] - c["segments"][0]["start"]) if g["segments"][0]["ep"] == c["segments"][0]["ep"] else None)
            errs_e.append(abs(g["segments"][-1]["end"] - c["segments"][-1]["end"]) if g["segments"][-1]["ep"] == c["segments"][-1]["ep"] else None)
    errs_s = [x for x in errs_s if x is not None]
    errs_e = [x for x in errs_e if x is not None]
    # Judge agreement inside labelled episodes. Unlike precision@k this does not depend on the ranking
    # happening to pick labelled episodes, so it still says something with a small gold set.
    def matches_gold(c):
        return any(iou({"segments": c["segments"]}, {"segments": g["segments"]}) >= thr for g in golds)
    in_lab = [c for c in judged if {s["ep"] for s in c["segments"]} <= labelled]
    good = [c for c in in_lab if matches_gold(c)]
    other = [c for c in in_lab if not matches_gold(c)]
    passed = [c for c in in_lab if c["eligible"]]
    return {
        "judged_in_labelled_episodes": len(in_lab),
        "judge_pass_precision": round(sum(matches_gold(c) for c in passed) / len(passed), 3) if passed else None,
        "gold_pass_rate": round(sum(c["eligible"] for c in good) / len(good), 3) if good else None,
        "non_gold_reject_rate": round(sum(not c["eligible"] for c in other) / len(other), 3) if other else None,
        "run": str(run), "k": k, "iou_threshold": thr,
        "precision_at_k": round(len(hits) / len(in_scope), 3) if in_scope else None,
        "top_k_in_labelled_episodes": len(in_scope),
        "candidate_recall": round(len(rec_pool) / len(golds), 3) if golds else None,
        "judged_recall": round(len(rec_judged) / len(golds), 3) if golds else None,
        "median_start_error_s": round(statistics.median(errs_s), 2) if errs_s else None,
        "median_end_error_s": round(statistics.median(errs_e), 2) if errs_e else None,
        "negative_episode_fp": len(fp), "negative_episode_fp_rate": round(len(fp) / max(len(top), 1), 3),
        "gold_promos": len(golds),
    }


def kendall_tau(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    if n < 2:
        return None
    conc = disc = 0
    for i in range(n):
        for j in range(i + 1, n):
            s = (a[i] - a[j]) * (b[i] - b[j])
            conc += s > 0
            disc += s < 0
    return round((conc - disc) / (n * (n - 1) / 2), 3)


def _rebuild(show, feats, p: dict) -> dict:
    c = {"cand_id": p["cand_id"], "template": p["template"], "segments": _segs(p["segments"]),
         "hook": {"id": p["hook"]["id"]}, "cliff": {"id": p["cliffhanger"]["id"]}, "confidence": 0.5}
    for s, raw in zip(c["segments"], p["segments"]):
        s["role"] = raw.get("role", "main")
    return refresh(show, feats, c)


def judge_checks(run: Path, args, cfg: dict) -> dict:
    out: dict = {}
    show = load_show(run / "parsed.json")
    feats = load_features(str(run / "parsed.json"))
    bible = json.loads((run / "bible.json").read_text(encoding="utf-8"))
    cast = clip_cast(bible)
    hook_s = cfg["promo"]["hook_max_s"]
    promos = json.loads((run / "ranked.json").read_text(encoding="utf-8"))["promos"]
    cands = [_rebuild(show, feats, p) for p in promos]
    if args.rejudge_model and cands:
        c2 = load_config(args.config, {"llm.model": args.rejudge_model})
        alt = LLM(c2, call_log=run / "llm_calls_eval.jsonl")
        mine_scores = [p["scores"]["judge_total"] for p in promos]
        alt_scores = [cold_judge(alt, show, c, c2, cast)["score"] for c in cands]
        out["cross_model"] = {"model": args.rejudge_model, "tau": kendall_tau(mine_scores, alt_scores),
                              "primary": mine_scores, "alternate": alt_scores}
    if args.stability and cands:
        llm = LLM(cfg, call_log=run / "llm_calls_eval.jsonl")
        sample = cands[:10]
        spreads = []
        for c in sample:
            scores = []
            for r in range(args.stability):
                text = render_clip(show, c, cast, hook_s=hook_s)[0]
                system, user_t = load_prompt(cfg["prompts"]["judge"])
                v = llm.complete_json(stage="judge_stability", system=system, user=fill(user_t, clip=text),
                                      schema=JudgeVerdict, temperature=0.8, cache_salt=f"stability-{r}")
                scores.append(v.hook_score * 0.3 + v.cliffhanger_score * 0.25 + v.standalone_score * 0.2
                              + v.trope_pull_score * 0.15)
            spreads.append(round(max(scores) - min(scores), 2))
        out["stability"] = {"runs": args.stability, "spread_per_promo": spreads,
                            "flagged_over_1pt": sum(s > 1.0 for s in spreads)}
    if args.leak_test and cands:
        llm = LLM(cfg, call_log=run / "llm_calls_eval.jsonl")
        deltas = []
        for c in cands[: args.leak_test]:
            text = render_clip(show, c, cast, hook_s=hook_s)[0]
            system, user_t = load_prompt(cfg["prompts"]["judge"])
            cold = llm.complete_json(stage="judge", system=system, user=fill(user_t, clip=text), schema=JudgeVerdict)
            warm = llm.complete_json(stage="judge_leak", system=system,
                                     user="You have read this show's story bible:\n" + bible_brief(bible, True)
                                     + "\n\n" + fill(user_t, clip=text), schema=JudgeVerdict)
            deltas.append(round(warm.standalone_score - cold.standalone_score, 2))
        out["leak_test"] = {"promos": len(deltas), "standalone_delta_with_bible": deltas,
                            "mean_delta": round(statistics.fmean(deltas), 2) if deltas else None}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", help="hand-labelled gold set (see eval/gold.template.json)")
    ap.add_argument("--run", action="append", required=True, help="output folder of a run (repeat for ablation)")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--iou", type=float, default=0.5)
    ap.add_argument("--config")
    ap.add_argument("--rejudge-model")
    ap.add_argument("--stability", type=int, default=0)
    ap.add_argument("--leak-test", type=int, default=0)
    args = ap.parse_args()
    load_dotenv()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cfg = load_config(args.config)
    report: dict = {"runs": []}
    if args.gold:
        gold = json.loads(Path(args.gold).read_text(encoding="utf-8"))
        for r in args.run:
            report["runs"].append(gold_metrics(gold, Path(r), args.k, args.iou))
        cols = ["precision_at_k", "top_k_in_labelled_episodes", "candidate_recall", "judged_recall",
                "judged_in_labelled_episodes", "judge_pass_precision", "gold_pass_rate", "non_gold_reject_rate",
                "median_start_error_s", "median_end_error_s"]
        print("| run | " + " | ".join(cols) + " |\n|" + "---|" * (len(cols) + 1))
        for m in report["runs"]:
            print(f"| {Path(m['run']).name} | " + " | ".join(str(m[c]) for c in cols) + " |")
    if args.rejudge_model or args.stability or args.leak_test:
        report["judge_checks"] = judge_checks(Path(args.run[0]), args, cfg)
        print(json.dumps(report["judge_checks"], indent=2))
    out = Path(args.run[0]) / "eval_report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
