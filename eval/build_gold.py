"""Gold-set kit.

    python eval/build_gold.py pack  [--episodes 1,3,5,7,8,24,27,43]   write paste-ready timelines
    python eval/build_gold.py merge --labeller "..."                  validate drafts -> eval/gold.json

pack:  eval/label_pack/epNN.txt (confidential script text: gitignored)
merge: reads eval/drafts/epNN.json (LLM drafts you reviewed), checks every timestamp against the
       parsed timeline, and writes eval/gold.json in the format evaluate.py expects. Episodes with an
       empty promo list become negative_episodes.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.parse import EPS, parse_show, parse_ts, print_episode  # noqa: E402

EVAL = ROOT / "eval"
DEFAULT_EPISODES = [1, 3, 5, 7, 8, 24, 27, 43]  # free-window eps, two cold ones (7, 8), three of the hottest


def pack(show_dir: Path, episodes: list[int]) -> None:
    show = parse_show(show_dir)
    out = EVAL / "label_pack"
    out.mkdir(parents=True, exist_ok=True)
    for n in episodes:
        buf = io.StringIO()
        print_episode(show.ep(n), out=buf)
        (out / f"ep{n:02d}.txt").write_text(buf.getvalue(), encoding="utf-8")
    (EVAL / "drafts").mkdir(exist_ok=True)
    print(f"wrote {len(episodes)} timelines to {out}\npaste eval/labeling_prompt.md into the external LLM, then one "
          f"file per message; save each reply as eval/drafts/epNN.json")


_TEXT_FIELD = re.compile(
    r'("(?:hook|cliffhanger|note|no_promo_reason)"\s*:\s*")(.*?)(")(?=\s*(?:,\s*"[a-z_]+"\s*:|[}\]]))', re.S)


def _repair_text_fields(t: str) -> str:
    """LLMs often quote dialogue inside prose fields without escaping it ("hook": "he says "No!" ...")
    and wrap long strings onto a new line. Escape inner quotes and flatten newlines in those fields."""
    def fix(m: re.Match) -> str:
        body = re.sub(r'\\?"', r'\\"', re.sub(r"\s*[\r\n]+\s*", " ", m.group(2)))
        return m.group(1) + body + m.group(3)
    return _TEXT_FIELD.sub(fix, t)


def _load_draft(p: Path) -> dict:
    t = p.read_text(encoding="utf-8-sig").strip()
    if not t:
        raise ValueError("file is empty (unsaved in the editor?)")
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    a, b = t.find("{"), t.rfind("}")
    t = t[a:b + 1]
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return json.loads(_repair_text_fields(t))


def merge(show_dir: Path, labeller: str) -> None:
    show = parse_show(show_dir)
    drafts = sorted((EVAL / "drafts").glob("ep*.json"))
    if not drafts:
        sys.exit("no drafts in eval/drafts/ (run 'pack' first and save the LLM replies there)")
    gold = {"show": show.show_id, "labelled_by": labeller, "labelled_at": time.strftime("%Y-%m-%d"),
            "labelled_episodes": [], "negative_episodes": [], "promos": []}
    problems, warnings = [], []
    for p in drafts:
        n = int(re.search(r"\d+", p.stem).group())
        try:
            d = _load_draft(p)
        except (ValueError, json.JSONDecodeError) as e:
            problems.append(f"{p.name}: not valid JSON ({e})")
            continue
        ep = show.ep(n)
        gold["labelled_episodes"].append(n)
        promos = d.get("promos") or []
        if not promos:
            gold["negative_episodes"].append(n)
        bounds = sorted({round(b.start, 2) for b in ep.all_blocks()} | {round(b.end, 2) for b in ep.all_blocks()})
        for k, pr in enumerate(promos, 1):
            tag = f"Ep {n} promo {k}"
            segs, dur = [], 0.0
            for s in pr.get("segments") or []:
                try:
                    a, b = parse_ts(s["start"]), parse_ts(s["end"])
                except (KeyError, ValueError):
                    problems.append(f"{tag}: bad timestamp {s}")
                    continue
                if not (0 <= a < b <= ep.duration + EPS):
                    problems.append(f"{tag}: {s['start']}-{s['end']} outside 00:00-{ep.duration:.2f}s or reversed")
                    continue
                for t in (a, b):
                    if not any(abs(t - x) <= 0.05 for x in bounds):
                        warnings.append(f"{tag}: {t:.2f}s is not a time in the timeline (invented?)")
                    if ep.lines_crossing(t):
                        warnings.append(f"{tag}: cut at {t:.2f}s falls inside a dialogue line")
                dur += b - a
                segs.append({"episode": n, "start": s["start"], "end": s["end"]})
            if not segs:
                continue
            if not 20 <= dur <= 60:
                warnings.append(f"{tag}: total {dur:.1f}s is outside 20-60s")
            gold["promos"].append({"id": f"G{len(gold['promos']) + 1}", "segments": segs,
                                   "hook": pr.get("hook", ""), "cliffhanger": pr.get("cliffhanger", ""),
                                   "note": pr.get("note", "")})
    gold["labelled_episodes"].sort()
    for w in warnings:
        print("warning:", w)
    if problems:
        print("\n".join("ERROR: " + x for x in problems))
        sys.exit("fix the drafts above, then merge again")
    (EVAL / "gold.json").write_text(json.dumps(gold, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote eval/gold.json: {len(gold['promos'])} promos over episodes {gold['labelled_episodes']}; "
          f"negative (no promo): {gold['negative_episodes'] or 'none'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["pack", "merge"])
    ap.add_argument("--show", default=str(ROOT / "data" / "show1"))
    ap.add_argument("--episodes", default=",".join(map(str, DEFAULT_EPISODES)))
    ap.add_argument("--labeller", default="LLM draft, human-reviewed")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if args.cmd == "pack":
        pack(Path(args.show), [int(x) for x in args.episodes.split(",") if x.strip()])
    else:
        merge(Path(args.show), args.labeller)


if __name__ == "__main__":
    main()
