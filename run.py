"""One command: screenplay folder in, ranked promos out.

    python run.py --show data/show1                        full run, no pause (interview default)
    python run.py --show data/show1 --fast                 top-10 heat episodes, judge 15, 1 repair round, no duels
    python run.py --show data/show1 --no-duels             rank on the rubric score alone
    python run.py --show data/show1 --max-repair-rounds 0  ablation: no refine loop
    python run.py --show data/show1 --review               pause for the editor in the terminal
    python run.py --show data/show1 --review --decisions d.json   non-interactive review
    python run.py --show data/show1 --resume               continue the last run (crash, quota cap, review)
    python run.py --show data/show1 --model offline        no API at all: heuristic baseline + pipeline check
    python run.py --show data/show1 --model groq/openai/gpt-oss-120b   any LiteLLM model string
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from langgraph.types import Command

from src.config import load_config
from src.editor import cli_review
from src.graph import prepare_run
from src.llm import LLM, PROVIDER_HINTS, QuotaExhausted


def parse_episodes(spec: str | None) -> list[int] | None:
    if not spec:
        return None
    out: list[int] = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            out += list(range(int(a), int(b) + 1))
        elif part.strip():
            out.append(int(part))
    return out


def preflight(llm: LLM) -> None:
    """Fail fast on a missing key for a route's PRIMARY model; only warn for fallbacks."""
    import litellm

    problems, warnings = [], []
    for r in llm.routes:
        for i, m in enumerate(r.models):
            if "$" in m["name"] or "$" in (m.get("api_base") or ""):
                msg = f"route '{r.name}': model '{m['name']}' uses an unset variable (put it in .env)"
            elif m.get("api_key_env"):
                if os.environ.get(m["api_key_env"]):
                    continue
                msg = f"route '{r.name}': {m['name']} needs {m['api_key_env']}"
            else:
                try:
                    env = litellm.validate_environment(model=m["name"])
                except Exception:
                    continue  # unknown provider: the first call will report it
                if env.get("keys_in_environment", True) or m.get("api_base"):
                    continue
                msg = f"route '{r.name}': {m['name']} needs {', '.join(env.get('missing_keys', []))}"
            (problems if i == 0 else warnings).append(msg)
    for w in warnings:
        print(f"  warning (fallback unavailable): {w}")
    if problems:
        sys.exit("Cannot start:\n  " + "\n  ".join(problems) + "\nFill in .env (see .env.example), or run "
                 "with --model offline for a no-API dry run.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Find, judge and rank promos in a folder of timestamped screenplays.")
    ap.add_argument("--show", required=True, help="folder of episode .txt screenplays")
    ap.add_argument("--out", help="output folder (default outputs/<show>, or outputs/<show>-offline)")
    ap.add_argument("--config", help="config file (default config.yaml)")
    ap.add_argument("--model", help="LiteLLM model string, or 'offline'")
    ap.add_argument("--rpm", type=float, help="client-side requests/minute (match your key's limit)")
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--no-duels", action="store_true")
    ap.add_argument("--max-repair-rounds", type=int)
    ap.add_argument("--episodes", help="e.g. 1-10 or 3,7,8")
    ap.add_argument("--review", action="store_true", help="pause for editor review")
    ap.add_argument("--decisions", help="JSON list of editor decisions (implies --review, no prompts)")
    ap.add_argument("--resume", action="store_true", help="continue the last run in this output folder")
    ap.add_argument("--thread-id")
    args = ap.parse_args()

    load_dotenv()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    show_dir = Path(args.show)
    if not show_dir.is_dir():
        sys.exit(f"--show {show_dir} is not a folder")
    if args.resume:
        guess = Path(args.out) if args.out else Path("outputs") / show_dir.name
        if not (guess / "run_args.json").exists() and (Path("outputs") / f"{show_dir.name}-offline" / "run_args.json").exists():
            guess = Path("outputs") / f"{show_dir.name}-offline"
        saved = json.loads((guess / "run_args.json").read_text(encoding="utf-8"))
        out_dir, thread_id, settings = guess, args.thread_id or saved["thread_id"], saved["settings"]
        settings["review"] = settings["review"] or args.review or bool(args.decisions)
    else:
        offline = args.model == "offline"
        out_dir = Path(args.out or Path("outputs") / (show_dir.name + ("-offline" if offline else "")))
        settings = {"model": args.model, "rpm": args.rpm, "config": args.config, "fast": args.fast,
                    "duels": not (args.no_duels or args.fast),
                    "max_repair_rounds": args.max_repair_rounds if args.max_repair_rounds is not None
                    else (1 if args.fast else None),
                    "episodes": parse_episodes(args.episodes), "review": args.review or bool(args.decisions)}
        thread_id = args.thread_id or f"{show_dir.name}-{time.strftime('%Y%m%d-%H%M%S')}"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "run_args.json").write_text(json.dumps({"thread_id": thread_id, "settings": settings,
                                                           "argv": sys.argv[1:]}, indent=2), encoding="utf-8")

    cfg = load_config(settings["config"], {"llm.model": settings["model"], "llm.rpm": settings["rpm"],
                                           "score.max_repair_rounds": settings["max_repair_rounds"]})
    llm = LLM(cfg, call_log=out_dir / "llm_calls.jsonl")
    if not llm.offline:
        preflight(llm)
    app, config, ctx = prepare_run(show_dir, out_dir, cfg, llm, thread_id, duels=settings["duels"],
                                   review=settings["review"], fast=settings["fast"], episodes=settings["episodes"])
    try:
        (out_dir / "graph.mmd").write_text(app.get_graph(xray=1).draw_mermaid(), encoding="utf-8")
    except Exception:
        pass
    print(f"Promo Finder | show {show_dir} | thread {thread_id}\n  models: "
          + ("offline (heuristic baseline)" if llm.offline else "\n          ".join(llm.describe())) + "\n"
          f"  mode: fast={settings['fast']} duels={settings['duels']} review={settings['review']} "
          f"repair_rounds={cfg['score']['max_repair_rounds']} episodes={settings['episodes'] or 'all'} -> {out_dir}")
    t0 = time.time()
    try:
        result = app.invoke(None if args.resume else {}, config, context=ctx)
        # --decisions is non-interactive for every round: round 0 uses the file as given; backfills
        # shown in later rounds get their file decision if it names them, else approve. Each
        # cand_id is decided once, so a nudged promo is not nudged again.
        file_decisions = (json.loads(Path(args.decisions).read_text(encoding="utf-8-sig"))  # PS 5.1 writes a BOM
                          if args.decisions else None)
        decided: set[str] = set()
        by_id: dict[str, dict] | None = None
        while result.get("__interrupt__"):
            payload = result["__interrupt__"][0].value
            if file_decisions is not None:
                if by_id is None:
                    # promo_id (P01..) refers to the ranking the editor first sees: resolve it once, on round 0,
                    # because ranks shift after a rejection backfills the shortlist
                    pid = {p["promo_id"]: p["cand_id"] for p in payload["shortlist"]}
                    by_id = {}
                    for d in file_decisions:
                        cid = d.get("cand_id") or pid.get(str(d.get("promo_id", "")).upper())
                        if cid is None:
                            sys.exit(f"--decisions: {d} names no cand_id and no promo_id in the shortlist")
                        by_id[cid] = {**d, "cand_id": cid}
                decisions = [by_id[p["cand_id"]] if p["cand_id"] in by_id and p["cand_id"] not in decided
                             else {"cand_id": p["cand_id"], "action": "approve"}
                             for p in payload["shortlist"]]
                decided.update(p["cand_id"] for p in payload["shortlist"])
            else:
                decisions = cli_review(payload)
            result = app.invoke(Command(resume=decisions), config, context=ctx)
    except QuotaExhausted as e:
        print(f"\nStopped: {e}\nEverything finished so far is checkpointed and cached. "
              f"Re-run with: python run.py --show {args.show} --resume")
        sys.exit(2)
    except KeyboardInterrupt:
        print(f"\nInterrupted. Re-run with --resume to continue thread {thread_id}.")
        sys.exit(130)
    except Exception as e:  # turn provider errors into one actionable line
        kind = type(e).__name__
        where = {"AuthenticationError": " (in .env)",
                 "NotFoundError": " (llm.routes / --model; for Azure: AZURE_DEPLOYMENT and AZURE_API_BASE)"}
        if kind in PROVIDER_HINTS:
            print(f"\nStopped ({kind}): {PROVIDER_HINTS[kind]}{where.get(kind, '')}.\n{str(e)[:300]}\n"
                  f"Progress so far is saved; fix and run again with --resume.")
            sys.exit(3)
        raise

    ranked = json.loads((out_dir / "ranked.json").read_text(encoding="utf-8"))
    stats = llm.snapshot()["total"]
    print(f"\nDone in {time.time() - t0:.0f}s. {len(ranked['promos'])} promos ranked "
          f"({int(stats.get('calls', 0))} API calls, {int(stats.get('cache_hits', 0))} cache hits, "
          f"{int(stats.get('offline', 0))} offline).")
    for p in ranked["promos"][:10]:
        segs = " + ".join(f"E{s['episode']:02d} {s['start']}-{s['end']}" for s in p["segments"])
        print(f"  {p['promo_id']} [{p['template']:<10}] {segs:<38} {p['duration_s']:5.1f}s  "
              f"final {p['scores']['final']:.2f}  hook@{p['hook']['at_s']:.1f}s")
    print(f"\nOutputs: {out_dir / 'ranked.json'}, {out_dir / 'report.md'}, {out_dir / 'candidates.jsonl'}")


if __name__ == "__main__":
    main()
