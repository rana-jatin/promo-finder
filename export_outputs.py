"""Build the shareable `raw_outputs/` folder from local run folders.

    python export_outputs.py                 rebuild raw_outputs/ from the runs listed in RUNS
    python export_outputs.py --out other/    write somewhere else

Files are copied byte for byte (the brief asks for unedited outputs). Two kinds of file never leave
the machine, because they hold the whole confidential screenplay pack:
  parsed.json  every shot, line, action and sound cue of all 50 episodes
  runs.db*     LangGraph checkpoints, which store the same parsed state
Before it finishes, the script checks the folder: no API key from .env, no excluded file, and how much
screenplay text it quotes verbatim (it fails above MAX_SCRIPT_SHARE).
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"

# run folder -> what it is (the first one is the submitted answer)
RUNS = {
    "show1": "FINAL ANSWER. Full 50-episode run, current model routes, duels on, 2 repair rounds.",
    "show1-r0": "Ablation of the final run: same models, refine loop off (--max-repair-rounds 0).",
    "show1-v1": "Earlier full run on the first routes (Gemini Flash / flash-lite mining, gpt-5-nano judge). "
                "Original run: 343 s, 115 API calls (logs/full_run.log); this folder is its cached rerun.",
    "show1-v1-r0": "Ablation of the earlier run: refine loop off.",
    "show1-offline": "No-LLM baseline (--model offline): keyword heuristics only. A pipeline check, not real picks.",
    "show1-reviewtest": "Editor-in-the-loop demo: scripted approve/reject decisions, backfill, second review round.",
}
LOG_FILES = ["*.log", "review_test_decisions.json"]
EXTRA = {"eval/gold.json": "eval/gold.json"}  # hand-checked gold set that evaluate.py scores against
EXCLUDE = ["parsed.json", "runs.db*", "llm_cache"]
MAX_SCRIPT_SHARE = 0.10  # fail if more than 10% of the pack's text items appear verbatim
MARKER = "MANIFEST.sha256"


def excluded(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in EXCLUDE)


def copy_runs(out: Path) -> list[Path]:
    copied: list[Path] = []
    for run in RUNS:
        src = OUTPUTS / run
        if not src.is_dir():
            sys.exit(f"missing run folder: {src}")
        for f in sorted(src.iterdir()):
            if f.is_file() and not excluded(f.name):
                dst = out / run / f.name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst)
                copied.append(dst)
    for pat in LOG_FILES:
        for f in sorted(OUTPUTS.glob(pat)):
            dst = out / "logs" / f.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
            copied.append(dst)
    for src_rel, dst_rel in EXTRA.items():
        dst = out / dst_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / src_rel, dst)
        copied.append(dst)
    return copied


def strings_of(path: Path) -> str:
    """All text in a file, with JSON escapes decoded so quotes match the screenplay exactly."""
    raw = path.read_text(encoding="utf-8", errors="ignore")
    out: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            out.append(o)

    try:
        if path.suffix == ".jsonl":
            for line in raw.splitlines():
                if line.strip():
                    walk(json.loads(line))
        elif path.suffix == ".json":
            walk(json.loads(raw))
        else:
            return raw
    except json.JSONDecodeError:
        return raw
    return "\n".join(out)


def pack_items() -> list[str] | None:
    parsed = OUTPUTS / "show1" / "parsed.json"
    if not parsed.exists():
        return None
    data = json.loads(parsed.read_text(encoding="utf-8"))
    items: set[str] = set()
    for ep in data["episodes"]:
        for kind in ("shots", "lines", "actions", "sfx"):
            items |= {x["text"] for x in ep[kind] if len(x["text"]) >= 20}
    return sorted(items)


def env_secrets() -> dict[str, str]:
    env = ROOT / ".env"
    if not env.exists():
        return {}
    found = {}
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            v = v.strip().strip("\"'")
            if len(v) >= 12:  # skip short non-secret values such as a region name
                found[k] = v
    return found


def check(out: Path, files: list[Path]) -> dict:
    texts = {f: strings_of(f) for f in files}
    raw = {f: f.read_text(encoding="utf-8", errors="ignore") for f in files}

    bad = [f for f in files if excluded(f.name)]
    if bad:
        sys.exit(f"excluded files were copied: {bad}")

    secrets = env_secrets()
    leaks = [(f, k) for f in files for k, v in secrets.items() if v in raw[f]]
    if leaks:
        sys.exit("API key found in: " + ", ".join(f"{f.relative_to(out)} ({k})" for f, k in leaks))

    items = pack_items()
    script = {"checked": items is not None}
    if items:
        per_file, union = {}, set()
        for f, t in texts.items():
            hit = {i for i in items if i in t}
            union |= hit
            if hit:
                per_file[str(f.relative_to(out).as_posix())] = len(hit)
        share = len(union) / len(items)
        script.update(pack_items=len(items), quoted=len(union), share=round(share, 4), per_file=per_file)
        if share > MAX_SCRIPT_SHARE:
            sys.exit(f"{share:.1%} of the screenplay appears verbatim (limit {MAX_SCRIPT_SHARE:.0%})")
    return {"secrets_checked": len(secrets), "script": script}


def run_line(run: str) -> str:
    meta_path = OUTPUTS / run / "run_meta.json"
    ranked_path = OUTPUTS / run / "ranked.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    n = "?"
    if ranked_path.exists():
        ranked = json.loads(ranked_path.read_text(encoding="utf-8"))
        promos = ranked.get("promos", ranked) if isinstance(ranked, dict) else ranked
        n = len(promos) if isinstance(promos, list) else "?"
    llm = meta.get("llm", {})
    model = llm.get("model", "?").replace(" | ", "<br>")  # a bare | would split the table cell
    total = llm.get("total", {})
    calls = f"{int(total.get('calls', 0))} / {int(total.get('cache_hits', 0))}" if total else "0 / 0"
    cost = total.get("cost_usd")
    cost_s = f"${cost:.2f}" if isinstance(cost, (int, float)) else "$0"
    secs = f"{round(meta['seconds'])} s" if "seconds" in meta else "?"
    return f"| `{run}/` | {RUNS[run]} | {n} | {model} | {calls} | {secs}, {cost_s} | {meta.get('finished', '?')} |"


def write_readme(out: Path, report: dict) -> None:
    s = report["script"]
    if s["checked"]:
        top = sorted(s["per_file"].items(), key=lambda kv: -kv[1])[:4]
        script_line = (f"{s['quoted']} of the pack's {s['pack_items']} text items ({s['share']:.1%}) appear verbatim, "
                       f"all as quoted hooks, cliffhangers and promo excerpts (most in: "
                       + ", ".join(f"`{k}` {v}" for k, v in top) + ").")
    else:
        script_line = "not measured (the local parsed.json was not found)."
    lines = [
        "# Raw outputs",
        "",
        "Unedited output of the Promo Finder on the full 50-episode input pack. Every file is a byte-for-byte",
        "copy of what `run.py` / `evaluate.py` wrote; this README and `MANIFEST.sha256` are the only added files,",
        "both generated by `export_outputs.py`.",
        "",
        "**Start here:** [`show1/report.md`](show1/report.md) (readable ranking) and "
        "[`show1/ranked.json`](show1/ranked.json) (the deliverable).",
        "",
        "## Runs",
        "",
        "| Folder | What it is | Promos | Models | API calls / cache hits | Wall-clock, est. cost | Finished |",
        "| --- | --- | --- | --- | --- | --- | --- |",
        *[run_line(r) for r in RUNS],
        "",
        "Cache hits replay an earlier identical prompt from the disk cache at zero cost, so the ablations and",
        "the review demo reuse the full run's mining and only pay for the calls that changed. Time and cost",
        "above are for the run that wrote the folder, not for the work it replayed.",
        "",
        "`show1/eval_report.json` scores the four LLM runs against the gold set in `eval/gold.json`.",
        "`logs/` holds each run's terminal output: `full_run_v2.log` → `show1/`, `r0_run_v2.log` → `show1-r0/`,",
        "`full_run.log` → the original v1 run, `full_run2.log` → its cached rerun that wrote `show1-v1/`,",
        "`r0_run.log` → `show1-v1-r0/`, `reviewtest.log` + `review_test_decisions.json` → `show1-reviewtest/`.",
        "`fast_run.log` is an early `--fast` run whose folder was later overwritten by a full run.",
        "",
        "## Files in each run folder",
        "",
        "| File | Contents |",
        "| --- | --- |",
        "| `ranked.json` | ranked promos: segments (episode, start, end), hook, cliffhanger, why it works, scores, reserve list |",
        "| `report.md` | the same ranking for people, \"post these first\", run stats, \"where it picks badly\" |",
        "| `candidates.jsonl` | every candidate the cold judge scored, with verdict, penalties and refine history |",
        "| `pool.jsonl` | every code-valid candidate before judging, with its pre-rank |",
        "| `rejected_by_code.jsonl` | candidates the hard validators threw out, with the rule that failed |",
        "| `beats.jsonl` | raw per-episode mining results (moment IDs, not text) and cross-episode links |",
        "| `bible.json` | season premise, appearance-only character descriptors, tropes, spoiler list |",
        "| `duels.jsonl` | pairwise \"which would you post?\" judgements behind the final order |",
        "| `features.json`, `graph.mmd`, `run_args.json`, `run_meta.json`, `llm_calls.jsonl` | stage-1 stats, pipeline graph, CLI args, per-stage tokens/cost/time, per-call log |",
        "| `audit.json` | parser audit of the screenplay format (counts only) |",
        "",
        "## Left out, on purpose",
        "",
        "`parsed.json` and `runs.db*` are not here: they contain the whole screenplay pack, which is Kuku's",
        "confidential content. Everything above can be regenerated from the pack with `python run.py --show data/show1`.",
        "",
        "Checks run on this folder by `export_outputs.py`:",
        f"- no API key from `.env` appears in any file ({report['secrets_checked']} keys checked);",
        "- no excluded file was copied;",
        f"- screenplay text quoted: {script_line}",
        "",
    ]
    (out / "README.md").write_text("\n".join(lines), encoding="utf-8")


def write_manifest(out: Path) -> None:
    rows = []
    for f in sorted(p for p in out.rglob("*") if p.is_file() and p.name != MARKER):
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        rows.append(f"{digest}  {f.relative_to(out).as_posix()}")
    (out / MARKER).write_text("\n".join(rows) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="raw_outputs")
    args = ap.parse_args()
    out = (ROOT / args.out).resolve()

    if out.exists():
        if not (out / MARKER).exists():
            sys.exit(f"{out} exists and was not made by this script; refusing to overwrite it")
        shutil.rmtree(out)
    out.mkdir(parents=True)

    files = copy_runs(out)
    report = check(out, files)
    write_readme(out, report)
    write_manifest(out)

    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    s = report["script"]
    print(f"Wrote {len(files)} files ({size / 1e6:.2f} MB) to {out}")
    print(f"  API keys: none found ({report['secrets_checked']} checked)")
    if s["checked"]:
        print(f"  screenplay quoted verbatim: {s['quoted']}/{s['pack_items']} text items ({s['share']:.1%})")


if __name__ == "__main__":
    main()
