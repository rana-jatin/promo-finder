"""LangGraph orchestrates the four runtime decisions: fan-out, the refine loop, quiet-show escalation
and the editor pause. Every stage inside a node is a plain Python function from src/*. The cold
judge never sees show context, and the LLM never writes a timestamp.

Durability is two-layer: the SQLite checkpointer resumes from the last completed superstep, and the
LLM adapter's prompt-hash disk cache replays any call that finished inside an interrupted fan-out.
Send payloads carry IDs and paths only; the parsed show lives once on disk (outputs/<show>/parsed.json)
and is loaded through an mtime-keyed lru_cache.
"""
from __future__ import annotations

import operator
import sqlite3
import time
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime, get_runtime
from langgraph.types import Command, Send, interrupt

from .assemble import assemble_pool
from .bible import clip_cast, run_bible
from .duel import run_duels
from .editor import apply_nudges, log_decisions, normalize, review_payload, with_nudges
from .features import load_features
from .io_utils import read_json, write_json, write_jsonl
from .judge import cold_judge, hard_fail, passes
from .link import link_arcs
from .llm import LLMOutputError
from .mine import mine_episode
from .parse import fmt_ts, load_show, parse_show, save_show
from .rank import order, score_all, select
from .repair import REPAIRABLE, repair
from .schemas import MineResult
from .write import write_candidates, write_outputs


# --------------------------------------------------------------------------- state + context
class RunContext(TypedDict):
    show_dir: str
    out_dir: str
    config: dict
    run_duels: bool
    enable_review: bool
    fast: bool
    episodes: Optional[list[int]]
    llm: Any  # src.llm.LLM; context is never checkpointed, so a live object is fine here


def _merge(a: dict | None, b: dict | None) -> dict:
    return {**(a or {}), **(b or {})}


class ShowState(TypedDict, total=False):
    show_id: str
    parsed_path: str
    episode_ids: list[int]
    bible: dict
    beats: Annotated[list[dict], operator.add]  # fan-in from mine (xN) + link
    pool: list[dict]  # pre-ranked assembled candidates
    judged: Annotated[list[dict], operator.add]  # fan-in from the refine subgraph
    batch: int  # 0 -> 1 (first batch) -> 2 (escalated batch)
    short: bool  # fewer than `shortlist` candidates clear the floor
    shortlist: list[dict]
    reserve: list[dict]
    duels: list[dict]
    ranked: list[dict]
    rejected_ids: Annotated[list[str], operator.add]
    decisions: Annotated[list[dict], operator.add]
    nudges: Annotated[dict, _merge]  # editor boundary nudges survive backfill rounds
    review_round: int
    review_done: bool
    started: float
    held_back: dict  # eligible candidates kept out of shortlist + reserve, by reason (latest select)


class RefineInput(TypedDict):
    cand: dict
    show_id: str
    parsed_path: str
    cast: list


class RefineState(TypedDict, total=False):
    cand: dict
    show_id: str
    parsed_path: str
    cast: list  # bible.clip_cast: who may be named in the cold render
    verdict: Optional[dict]
    best: Optional[dict]  # {"cand", "verdict", "round"}
    round: int
    improved: bool
    repairable: bool
    history: list[dict]
    judged: list[dict]


class RefineOutput(TypedDict):
    judged: list[dict]  # merges into ShowState.judged through operator.add


def _context() -> RunContext:
    return get_runtime(RunContext).context


def _batch_size() -> int:
    ctx = _context()
    run = ctx["config"]["run"]
    return run["fast_judge"] if ctx.get("fast") else run["judge_batch"]


def _out(ctx: RunContext) -> Path:
    return Path(ctx["out_dir"])


# --------------------------------------------------------------------------- main-graph nodes
def parse_node(state: ShowState, runtime: Runtime[RunContext]) -> dict:
    ctx, cfg = runtime.context, runtime.context["config"]
    show = parse_show(ctx["show_dir"])
    _out(ctx).mkdir(parents=True, exist_ok=True)
    parsed_path = save_show(show, _out(ctx) / "parsed.json")
    feats = load_features(parsed_path)
    write_json(_out(ctx) / "features.json", {str(k): {"heat": v["heat"]} for k, v in feats.items()})
    eps = show.episode_numbers
    if ctx.get("episodes"):
        eps = [e for e in eps if e in set(ctx["episodes"])]
    if ctx.get("fast"):
        eps = sorted(sorted(eps, key=lambda e: -feats[e]["heat"])[: cfg["run"]["fast_episodes"]])
    return {"show_id": show.show_id, "parsed_path": parsed_path, "episode_ids": eps, "batch": 0,
            "short": False, "review_round": 0, "review_done": False, "started": time.time()}


def bible_node(state: ShowState, runtime: Runtime[RunContext]) -> dict:
    ctx = runtime.context
    bible = run_bible(ctx["llm"], load_show(state["parsed_path"]), ctx["config"])
    write_json(_out(ctx) / "bible.json", bible)
    return {"bible": bible}


def fanout_mining(state: ShowState):
    all_eps = set(load_show(state["parsed_path"]).episode_numbers)
    eps = state.get("episode_ids") or []
    if not eps:
        return "link"
    return [Send("mine", {"ep_id": e, "next_ep_id": e + 1 if e + 1 in all_eps else None,
                          "parsed_path": state["parsed_path"]}) for e in eps]


def mine_node(payload: dict, runtime: Runtime[RunContext]) -> dict:
    ctx = runtime.context
    show, feats = load_show(payload["parsed_path"]), load_features(payload["parsed_path"])
    bible = read_json(_out(ctx) / "bible.json")
    try:
        rec = mine_episode(ctx["llm"], show, payload["ep_id"], payload["next_ep_id"], bible, feats, ctx["config"])
    except LLMOutputError as e:  # dropped and logged, never fatal
        rec = {"kind": "mine", "ep": payload["ep_id"], "next_ep": payload["next_ep_id"], "error": str(e)[:300],
               "result": MineResult(no_promo=True, no_promo_reason="invalid LLM output").model_dump(),
               "dropped": []}
    return {"beats": [rec]}


def link_node(state: ShowState, runtime: Runtime[RunContext]) -> dict:
    ctx = runtime.context
    show = load_show(state["parsed_path"])
    mined = sorted((b for b in state.get("beats") or [] if b.get("kind") == "mine"), key=lambda b: b["ep"])
    try:
        rec = link_arcs(ctx["llm"], show, mined, state.get("bible") or {}, ctx["config"])
    except LLMOutputError as e:
        rec = {"kind": "link", "result": {"stitches": []}, "dropped": [], "error": str(e)[:300]}
    write_jsonl(_out(ctx) / "beats.jsonl", [*mined, rec])
    return {"beats": [rec]}


def assemble_node(state: ShowState, runtime: Runtime[RunContext]) -> dict:
    ctx = runtime.context
    show, feats = load_show(state["parsed_path"]), load_features(state["parsed_path"])
    pool, rejected = assemble_pool(show, feats, state.get("beats") or [], ctx["config"])
    write_jsonl(_out(ctx) / "pool.jsonl", [
        {"pool_rank": c["pool_rank"], "cand_id": c["cand_id"], "template": c["template"], "source": c["source"],
         "segments": [(s["ep"], fmt_ts(s["start"]), fmt_ts(s["end"]), s["role"]) for s in c["segments"]],
         "duration": c["duration"], "hook": c["hook"], "cliff": c["cliff"], "confidence": c["confidence"],
         "peak_intensity": c["peak_intensity"], "prerank": c["prerank"], "notes": c["notes"]} for c in pool])
    write_jsonl(_out(ctx) / "rejected_by_code.jsonl", rejected)
    return {"pool": pool}


def _sends(state: ShowState, cands: list[dict]) -> list[Send]:
    cast = clip_cast(state.get("bible") or {})
    return [Send("refine", {"cand": c, "show_id": state["show_id"], "parsed_path": state["parsed_path"],
                            "cast": cast}) for c in cands]


def fanout_judging(state: ShowState):
    first = (state.get("pool") or [])[: _batch_size()]
    return _sends(state, first) if first else "coverage"


def coverage_node(state: ShowState, runtime: Runtime[RunContext]) -> dict:
    cfg = runtime.context["config"]
    clear = [j for j in state.get("judged") or []
             if j["verdict"]["score"] >= cfg["score"]["min_score"] and not hard_fail(j["verdict"], cfg)]
    return {"batch": state.get("batch", 0) + 1, "short": len(clear) < cfg["select"]["shortlist"]}


def after_coverage(state: ShowState):
    n = _batch_size()
    if state.get("short") and state.get("batch") == 1:  # escalate exactly once
        nxt = (state.get("pool") or [])[n: 2 * n]
        if nxt:
            return _sends(state, nxt)
    return "select"


def select_node(state: ShowState, runtime: Runtime[RunContext]) -> dict:
    ctx = runtime.context
    scored = score_all(load_show(state["parsed_path"]), state.get("judged") or [], state.get("bible") or {},
                       ctx["config"])
    write_candidates(ctx["out_dir"], scored)
    why: dict[str, int] = {}
    shortlist, reserve = select(scored, state.get("rejected_ids") or [], ctx["config"], why=why)
    return {"shortlist": shortlist, "reserve": reserve, "held_back": why}


def route_duels(state: ShowState) -> Literal["duel", "order"]:
    return "duel" if _context().get("run_duels") and len(state.get("shortlist") or []) >= 2 else "order"


def duel_node(state: ShowState, runtime: Runtime[RunContext]) -> dict:
    ctx = runtime.context
    games = run_duels(ctx["llm"], load_show(state["parsed_path"]), state["shortlist"], ctx["config"],
                      clip_cast(state.get("bible") or {}), workers=ctx["config"]["run"]["max_concurrency"])
    write_jsonl(_out(ctx) / "duels.jsonl", games)
    return {"duels": games}


def order_node(state: ShowState, runtime: Runtime[RunContext]) -> dict:
    ctx = runtime.context
    shortlist = with_nudges(state.get("shortlist") or [], state.get("nudges") or {})
    ids = {c["cand_id"] for c in shortlist}
    duels = [d for d in (state.get("duels") or []) if d.get("winner_id") in ids and d.get("loser_id") in ids] \
        if ctx.get("run_duels") else []
    return {"ranked": order(shortlist, duels, ctx["config"])}


def write_node(state: ShowState, runtime: Runtime[RunContext]) -> dict:
    ctx, cfg = runtime.context, runtime.context["config"]
    show = load_show(state["parsed_path"])
    run_meta = {
        "mode": {"fast": bool(ctx.get("fast")), "duels": bool(ctx.get("run_duels")),
                 "review": bool(ctx.get("enable_review")), "max_repair_rounds": cfg["score"]["max_repair_rounds"],
                 "episodes": state.get("episode_ids")},
        "prompt_version": cfg.get("prompt_version"), "prompts": cfg["prompts"], "llm": ctx["llm"].snapshot(),
        "seconds": round(time.time() - state.get("started", time.time()), 1),
        "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
        "review": {"round": state.get("review_round", 0), "done": state.get("review_done", False),
                   "decisions": len(state.get("decisions") or [])},
    }
    write_outputs(_out(ctx), show, state, cfg, clip_cast(state.get("bible") or {}), run_meta)
    write_json(_out(ctx) / "run_meta.json", run_meta)
    return {}


def route_review(state: ShowState):
    ctx = _context()
    if ctx.get("enable_review") and not state.get("review_done") and state.get("ranked"):
        return "editor"
    return END


def editor_node(state: ShowState, runtime: Runtime[RunContext]) -> Command[Literal["select", "write"]]:
    ctx, cfg = runtime.context, runtime.context["config"]
    show = load_show(state["parsed_path"])
    rnd = state.get("review_round", 0)
    # interrupt first: the node re-runs from the top on resume, so nothing above it has side effects
    raw = interrupt(review_payload(show, state["ranked"], rnd))
    decisions = normalize(raw if isinstance(raw, list) else (raw or {}).get("decisions", []), state["ranked"])
    accepted, nudge_log = apply_nudges(show, load_features(state["parsed_path"]), state["ranked"], decisions, cfg)
    log_decisions(ctx["out_dir"], rnd, decisions, nudge_log)
    rejected = [d["cand_id"] for d in decisions if d["action"] == "reject"]
    upd = {"decisions": [{**d, "round": rnd} for d in decisions], "nudges": accepted}
    if rejected and rnd < cfg.get("review", {}).get("max_rounds", 2):
        return Command(goto="select", update={**upd, "rejected_ids": rejected, "review_round": rnd + 1})
    # approved (clean or nudged) or out of rounds: rewrite outputs so files match the final decisions
    keep = [c for c in with_nudges(state["ranked"], accepted) if c["cand_id"] not in set(rejected)]
    ranked = [{**c, "rank": i} for i, c in enumerate(keep, 1)]
    return Command(goto="write", update={**upd, "ranked": ranked, "review_done": True,
                                         "rejected_ids": rejected})


# --------------------------------------------------------------------------- refine subgraph (loop 1)
def judge_node(s: RefineState, runtime: Runtime[RunContext]) -> dict:
    ctx = runtime.context
    rnd = s.get("round", 0)
    try:
        v = cold_judge(ctx["llm"], load_show(s["parsed_path"]), s["cand"], ctx["config"],
                       s.get("cast") or [])
    except LLMOutputError as e:
        return {"verdict": None, "repairable": False, "improved": False, "round": rnd,
                "history": (s.get("history") or []) + [{"round": rnd, "error": str(e)[:200]}]}
    best = s.get("best")
    improved = best is None or v["score"] > best["verdict"]["score"] + 1e-9
    hist = (s.get("history") or []) + [{
        "round": rnd, "score": v["score"], "passed": passes(v, ctx["config"]), "main_failure": v["main_failure"],
        "repairs": s["cand"].get("repairs", []),
        "segments": [(x["ep"], fmt_ts(x["start"]), fmt_ts(x["end"])) for x in s["cand"]["segments"]]}]
    return {"verdict": v, "best": {"cand": s["cand"], "verdict": v, "round": rnd} if improved else best,
            "improved": improved, "repairable": v["main_failure"] in REPAIRABLE, "history": hist, "round": rnd}


def route_refine(s: RefineState) -> Literal["repair", "finalize"]:
    cfg = _context()["config"]
    v = s.get("verdict")
    if v is None or passes(v, cfg):
        return "finalize"
    rnd = s.get("round", 0)
    if rnd >= cfg["score"]["max_repair_rounds"] or not s.get("repairable") or (rnd > 0 and not s.get("improved")):
        return "finalize"  # out of rounds, unfixable, or the last repair did not help
    return "repair"


def repair_node(s: RefineState, runtime: Runtime[RunContext]) -> dict:
    new = repair(load_show(s["parsed_path"]), load_features(s["parsed_path"]), s["cand"], s["verdict"],
                 runtime.context["config"])
    if new is None:
        return {"repairable": False}
    return {"cand": new, "round": s.get("round", 0) + 1}


def route_after_repair(s: RefineState) -> Literal["judge", "finalize"]:
    return "judge" if s.get("repairable") else "finalize"


def finalize_node(s: RefineState) -> dict:
    best = s.get("best")
    if best is None:
        return {"judged": []}
    return {"judged": [{**best["cand"], "verdict": best["verdict"], "rounds": s.get("round", 0),
                        "best_round": best["round"], "refine_history": s.get("history") or []}]}


def build_refine_subgraph():
    sg = StateGraph(RefineState, context_schema=RunContext, input_schema=RefineInput, output_schema=RefineOutput)
    sg.add_node("judge", judge_node)
    sg.add_node("repair", repair_node)
    sg.add_node("finalize", finalize_node)
    sg.add_edge(START, "judge")
    sg.add_conditional_edges("judge", route_refine, {"repair": "repair", "finalize": "finalize"})
    sg.add_conditional_edges("repair", route_after_repair, {"judge": "judge", "finalize": "finalize"})
    sg.add_edge("finalize", END)
    # no per-subgraph checkpoints: pending writes of finished refine tasks are kept by the parent,
    # and an interrupted one replays its judge calls from the disk cache for free
    return sg.compile(checkpointer=False)


# --------------------------------------------------------------------------- main graph
def build_graph() -> StateGraph:
    g = StateGraph(ShowState, context_schema=RunContext)
    g.add_node("parse", parse_node)
    g.add_node("bible", bible_node)
    g.add_node("mine", mine_node)
    g.add_node("link", link_node)
    g.add_node("assemble", assemble_node)
    g.add_node("refine", build_refine_subgraph())
    g.add_node("coverage", coverage_node)
    g.add_node("select", select_node)
    g.add_node("duel", duel_node)
    g.add_node("order", order_node)
    g.add_node("write", write_node)
    g.add_node("editor", editor_node, destinations=("select", "write"))

    g.add_edge(START, "parse")
    g.add_edge("parse", "bible")
    g.add_conditional_edges("bible", fanout_mining, ["mine", "link"])
    g.add_edge("mine", "link")
    g.add_edge("link", "assemble")
    g.add_conditional_edges("assemble", fanout_judging, ["refine", "coverage"])
    g.add_edge("refine", "coverage")
    g.add_conditional_edges("coverage", after_coverage, ["refine", "select"])
    g.add_conditional_edges("select", route_duels, ["duel", "order"])
    g.add_edge("duel", "order")
    g.add_edge("order", "write")
    g.add_conditional_edges("write", route_review, ["editor", END])
    return g


def build_app(db_path: str | Path):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    return build_graph().compile(checkpointer=SqliteSaver(conn))


def prepare_run(show_dir: str | Path, out_dir: str | Path, cfg: dict, llm, thread_id: str, *, duels: bool,
                review: bool, fast: bool, episodes: list[int] | None = None) -> tuple[Any, dict, dict]:
    """(app, run config, runtime context) for one thread: shared by run.py and the web runner."""
    ctx = {"show_dir": str(show_dir), "out_dir": str(out_dir), "config": cfg, "run_duels": duels,
           "enable_review": review, "fast": fast, "episodes": episodes, "llm": llm}
    config = {"configurable": {"thread_id": thread_id}, "max_concurrency": cfg["run"]["max_concurrency"],
              "recursion_limit": 300}
    return build_app(Path(out_dir) / "runs.db"), config, ctx
