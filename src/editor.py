"""Loop 3 helpers: the human editor reviews the shortlist (approve / reject with reason / nudge a
boundary). Decisions are logged to decisions.jsonl: training data for the ranking weights later."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .assemble import nudge
from .parse import Show, fmt_ts


def review_payload(show: Show, ranked: list[dict], rnd: int) -> dict:
    items = []
    for c in ranked:
        hb, cb = show.block(c["hook"]["id"]), show.block(c["cliff"]["id"])
        items.append({
            "promo_id": f"P{c['rank']:02d}", "cand_id": c["cand_id"], "rank": c["rank"], "template": c["template"],
            "segments": [{"seg": i, "episode": s["ep"], "start": fmt_ts(s["start"]), "end": fmt_ts(s["end"])}
                         for i, s in enumerate(c["segments"])],
            "duration_s": c["duration"],
            "hook": f'{hb.speaker}: "{hb.text}"' if hb and hb.kind == "line" else (hb.text if hb else ""),
            "cliff": f'{cb.speaker}: "{cb.text}"' if cb and cb.kind == "line" else (cb.text if cb else ""),
            "open_question": c["verdict"].get("open_question", ""), "why": c["verdict"].get("why_it_works", ""),
            "final_score": c.get("final_score"), "flags": c.get("flags", []), "post_reason": c.get("post_reason", ""),
        })
    return {"round": rnd, "shortlist": items,
            "how_to_answer": [{"cand_id": "...", "action": "approve"},
                              {"cand_id": "...", "action": "reject", "reason": "why"},
                              {"cand_id": "...", "action": "nudge", "changes": [{"seg": 0, "start": -0.5, "end": "00:59.20"}]}]}


def normalize(decisions: list[dict], ranked: list[dict]) -> list[dict]:
    by_pid = {f"P{c['rank']:02d}": c["cand_id"] for c in ranked}
    out = []
    for d in decisions or []:
        cid = d.get("cand_id") or by_pid.get(str(d.get("promo_id", "")).upper())
        if cid:
            out.append({**d, "cand_id": cid, "action": str(d.get("action", "approve")).lower()})
    return out


def apply_nudges(show: Show, feats: dict, ranked: list[dict], decisions: list[dict], cfg: dict) -> tuple[dict, list[dict]]:
    """Validate each nudge through assemble.nudge (snap to the nearest legal cut + full validators)."""
    by = {c["cand_id"]: c for c in ranked}
    accepted: dict[str, dict] = {}
    log: list[dict] = []
    for d in decisions:
        if d["action"] != "nudge" or d["cand_id"] not in by:
            continue
        new, errs = nudge(show, feats, by[d["cand_id"]], d.get("changes") or [], cfg)
        if new is None:
            log.append({"cand_id": d["cand_id"], "nudge": "rejected_by_validators", "errors": errs})
        else:
            accepted[d["cand_id"]] = {"segments": new["segments"], "duration": new["duration"],
                                      "hook": new["hook"], "cliff": new["cliff"], "line_ids": new["line_ids"],
                                      "notes": new["notes"]}
            log.append({"cand_id": d["cand_id"], "nudge": "applied",
                        "segments": [(s["ep"], fmt_ts(s["start"]), fmt_ts(s["end"])) for s in new["segments"]]})
    return accepted, log


def with_nudges(ranked: list[dict], nudges: dict) -> list[dict]:
    return [{**c, **nudges[c["cand_id"]]} if c["cand_id"] in nudges else c for c in ranked]


def log_decisions(out_dir: str | Path, rnd: int, decisions: list[dict], nudge_log: list[dict]) -> None:
    p = Path(out_dir) / "decisions.jsonl"
    seen = set()
    if p.exists():  # idempotent: a re-executed editor node does not double-log
        for line in p.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            seen.add((r.get("round"), r.get("cand_id"), r.get("action")))
    with p.open("a", encoding="utf-8") as f:
        for d in decisions:
            key = (rnd, d["cand_id"], d["action"])
            if key in seen:
                continue
            extra = next((x for x in nudge_log if x["cand_id"] == d["cand_id"]), {})
            f.write(json.dumps({"round": rnd, **d, **({"nudge_result": extra} if extra else {}),
                                "at": time.strftime("%Y-%m-%dT%H:%M:%S")}, ensure_ascii=False) + "\n")


def cli_review(payload: dict) -> list[dict]:
    """Terminal review: a = approve, r = reject (asks why), n = nudge, A = approve all remaining."""
    print(f"\n=== Editor review, round {payload['round']} ===")
    decisions: list[dict] = []
    approve_rest = False
    for p in payload["shortlist"]:
        if approve_rest:
            decisions.append({"cand_id": p["cand_id"], "action": "approve"})
            continue
        segs = " + ".join(f"[{s['seg']}] E{s['episode']:02d} {s['start']}-{s['end']}" for s in p["segments"])
        print(f"\n{p['promo_id']} ({p['template']}, {p['duration_s']:.1f}s, score {p['final_score']:.2f}) {segs}")
        print(f"  hook : {p['hook']}\n  cliff: {p['cliff']}\n  Q    : {p['open_question']}\n  why  : {p['why']}")
        if p["flags"]:
            print(f"  flags: {', '.join(p['flags'])}")
        ans = input("  [a]pprove / [r]eject / [n]udge / [A]pprove all remaining > ").strip()
        if ans == "A":
            approve_rest = True
            decisions.append({"cand_id": p["cand_id"], "action": "approve"})
        elif ans.lower().startswith("r"):
            decisions.append({"cand_id": p["cand_id"], "action": "reject", "reason": input("  reason > ").strip()})
        elif ans.lower().startswith("n"):
            raw = input("  seg start end (deltas like -0.5 / +1, absolute MM:SS.ss, or _ to keep) > ").split()
            if len(raw) == 3:
                ch = {"seg": int(raw[0])}
                for side, v in zip(("start", "end"), raw[1:]):
                    if v != "_":
                        ch[side] = v if ":" in v else float(v)
                decisions.append({"cand_id": p["cand_id"], "action": "nudge", "changes": [ch]})
            else:
                decisions.append({"cand_id": p["cand_id"], "action": "approve"})
        else:
            decisions.append({"cand_id": p["cand_id"], "action": "approve"})
    return decisions
