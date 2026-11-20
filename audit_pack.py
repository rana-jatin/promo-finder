"""Step 0: audit a screenplay pack before trusting the parser.

    python audit_pack.py --show data/show1

Checks every file for: block-label inventory (raw text, independent of the parser), header
presence, timing-line format, parser coverage, scene timing, shot tiling (gaps / overlaps /
uncovered tails), lines crossing cuts, overlapping dialogue, blocks outside their scene.
Writes outputs/<show>/audit.json. Exits 1 if parser coverage is below 95% of content lines.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

from src.parse import EPS, parse_episode_text, _ep_hint_from_name

BOLD_LABEL = re.compile(r"^\*\*(.+?):\*\*\s*$")
TIMING = re.compile(r"^Start: \((\d\d:\d\d\.\d\d)\) \| End: \((\d\d:\d\d\.\d\d)\)$")


def audit(show_dir: Path) -> dict:
    files = sorted(f for f in show_dir.glob("*.txt") if f.name.lower() != "title.txt")
    labels = collections.Counter()
    timing_ok = timing_bad = 0
    per_ep = []
    totals = collections.Counter()
    fixes = collections.Counter()
    examples = collections.defaultdict(list)
    emotions = collections.Counter()
    speakers = collections.Counter()

    for f in files:
        text = f.read_text(encoding="utf-8-sig")
        content_lines = 0
        for ln in text.splitlines():
            s = ln.strip()
            if not s:
                continue
            content_lines += 1
            if m := BOLD_LABEL.match(s):
                lab = m.group(1)
                labels[lab if lab in ("CAMERA", "ACTION", "SOUND FX") else
                       ("<DIALOGUE>" if "(" in lab else f"<OTHER:{lab}>")] += 1
            if s.lower().startswith("start:"):
                if TIMING.match(s):
                    timing_ok += 1
                else:
                    timing_bad += 1
                    examples["timing_format"].append(f"{f.name}: {s[:80]}")

        ep = parse_episode_text(text, source=f.name, ep_hint=_ep_hint_from_name(f.name))
        tag = f"E{ep.ep:02d}"
        unparsed = [i for i in ep.issues if i["issue"] == "unparsed_line"]
        totals["unparsed_lines"] += len(unparsed)
        totals["content_lines"] += content_lines
        for i in ep.issues:
            totals[f"issue:{i['issue']}"] += 1
            examples[i["issue"]].append(f"{tag} line {i.get('line')}: {i.get('text', '')[:60]}")
        for fx in ep.grid_fixes:
            fixes[fx["fix"]] += 1
            if len(examples[fx["fix"]]) < 6:
                examples[fx["fix"]].append(f"{tag} at {fx['at']:.2f}s " +
                                           (f"gap {fx['gap_s']}s" if "gap_s" in fx else
                                            f"overlap {fx.get('overlap_s')}s" if "overlap_s" in fx else ""))
        cuts = ep.cut_points()[1:-1]
        crossing = [ln for ln in ep.lines if any(ln.start + EPS < c < ln.end - EPS for c in cuts)]
        lines_sorted = sorted(ep.lines, key=lambda x: x.start)
        overlaps = [(a, b) for a, b in zip(lines_sorted, lines_sorted[1:]) if b.start < a.end - EPS]
        outside = [b for b in [*ep.lines, *ep.actions, *ep.sfx]
                   if b.start < ep.scenes[b.scene - 1].start - EPS or b.end > ep.scenes[b.scene - 1].end + EPS]
        contiguous = all(abs(b.start - a.end) <= EPS for a, b in zip(ep.scenes, ep.scenes[1:]))
        totals["lines"] += len(ep.lines)
        totals["lines_crossing_cut"] += len(crossing)
        totals["dialogue_overlaps"] += len(overlaps)
        totals["blocks_outside_scene"] += len(outside)
        totals["episodes_with_scene_gaps"] += (not contiguous)
        totals["scenes"] += len(ep.scenes)
        totals["cells"] += len(ep.shots)
        totals["synthetic_cells"] += sum(c.synthetic for c in ep.shots)
        totals["short_cells_lt_0.5s"] += sum(c.dur < 0.5 for c in ep.shots)
        for a, b in overlaps[:2]:
            examples["dialogue_overlap"].append(f"{tag} {a.id} {a.start}-{a.end} / {b.id} {b.start}-{b.end}")
        for b in outside[:2]:
            examples["outside_scene"].append(f"{tag} {b.id} {b.start}-{b.end} scene {b.scene}")
        for ln in ep.lines:
            speakers[ln.speaker] += 1
            emotions.update(ln.emotions)
        per_ep.append({"ep": ep.ep, "file": f.name, "duration_s": ep.duration, "scenes": len(ep.scenes),
                       "cells": len(ep.shots), "synthetic_cells": sum(c.synthetic for c in ep.shots),
                       "lines": len(ep.lines), "actions": len(ep.actions), "sfx": len(ep.sfx),
                       "grid_fixes": len(ep.grid_fixes), "lines_crossing_cut": len(crossing),
                       "dialogue_overlaps": len(overlaps), "unparsed": len(unparsed),
                       "has_logline": bool(ep.logline), "has_continuity": bool(ep.continuity)})

    coverage = 1 - totals["unparsed_lines"] / max(totals["content_lines"], 1)
    durations = sorted(e["duration_s"] for e in per_ep)
    return {
        "show_dir": str(show_dir), "files": len(files),
        "episode_numbers_contiguous": [e["ep"] for e in per_ep] == list(range(1, len(per_ep) + 1)),
        "label_inventory": dict(labels.most_common()),
        "timing_lines": {"ok": timing_ok, "nonstandard": timing_bad},
        "parser_coverage": round(coverage, 4),
        "totals": dict(totals),
        "grid_fixes": dict(fixes),
        "duration_s": {"min": durations[0], "median": durations[len(durations) // 2], "max": durations[-1],
                       "total": round(sum(durations), 1)},
        "distinct_speakers": len(speakers), "top_speakers": dict(speakers.most_common(12)),
        "distinct_emotions": len(emotions), "top_emotions": dict(emotions.most_common(25)),
        "missing_headers": [e["ep"] for e in per_ep if not (e["has_logline"] and e["has_continuity"])],
        "examples": {k: v[:6] for k, v in examples.items()},
        "episodes": per_ep,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", required=True)
    ap.add_argument("--out", default=None, help="default: outputs/<show>/audit.json")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    show_dir = Path(args.show)
    rep = audit(show_dir)
    out = Path(args.out or f"outputs/{show_dir.name}/audit.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")

    t = rep["totals"]
    print(f"Audited {rep['files']} files in {show_dir}  ->  {out}")
    print(f"  episodes numbered 1..N without gaps: {rep['episode_numbers_contiguous']}")
    print(f"  labels: {rep['label_inventory']}")
    print(f"  timing lines: {rep['timing_lines']}   parser coverage: {rep['parser_coverage']:.2%}")
    print(f"  durations: {rep['duration_s']}   scenes: {t['scenes']}   lines: {t['lines']}")
    print(f"  cut grid: {t['cells']} cells ({t['synthetic_cells']} synthetic fillers), fixes: {rep['grid_fixes']}")
    print(f"  lines crossing a cut: {t['lines_crossing_cut']} ({t['lines_crossing_cut'] / max(t['lines'], 1):.1%})"
          f"   overlapping dialogue: {t['dialogue_overlaps']}   blocks outside scene: {t['blocks_outside_scene']}")
    print(f"  episodes with gaps between scenes: {t['episodes_with_scene_gaps']}   missing headers: {rep['missing_headers']}")
    print(f"  speakers: {rep['distinct_speakers']}   emotion tags: {rep['distinct_emotions']}")
    if rep["parser_coverage"] < 0.95:
        print("FAIL: parser covers under 95% of content lines; inspect examples in audit.json")
        sys.exit(1)


if __name__ == "__main__":
    main()
