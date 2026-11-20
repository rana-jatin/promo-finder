"""Stage 4 (template 4): link mined setups and payoffs across the whole season into arc stitches
(1 global LLM call; a 5-episode window would never see Ep 8 and Ep 22 together). Beats are shown grouped
by the bible's arc ids, each with its nearby rows; the show context includes the spoiler list."""
from __future__ import annotations

from .bible import arc_ids, bible_brief, show_tropes
from .llm import LLM, fill, load_prompt
from .mine import render_timeline
from .parse import EPS, Show, ep_of
from .schemas import LinkResult, link_schema

MAX_BEATS = 80
NEAR_S = 8.0
# Camera rows are left out: on show1 they were ~25k of a ~33k-token prompt (long shot prose), and segment
# edges snap to the shot grid in code anyway. Lines, actions and sound effects carry who does what.
CONTEXT_KINDS = ("line", "action", "sfx")


def _context_rows(show: Show, bid: str) -> str:
    """Rows within NEAR_S of the beat, as ID [seconds from episode start] content."""
    b = show.block(bid)
    return render_timeline(show.ep(b.ep), None, max(0.0, b.start - NEAR_S), b.end + NEAR_S, indent="    ",
                           kinds=CONTEXT_KINDS)


def collect_beats(beats: list[dict]) -> list[dict]:
    out = []
    for rec in beats:
        if rec.get("kind") != "mine":
            continue
        for kind in ("setups", "payoffs"):
            for x in rec["result"].get(kind) or []:
                out.append({"id": x["id"], "ep": rec["ep"], "kind": kind[:-1], "what": x.get("what", ""),
                            "arc": x.get("arc") or "new"})
    out.sort(key=lambda x: (x["ep"], x["id"]))
    return out[:MAX_BEATS]


def beats_text(show: Show, items: list[dict], bible: dict) -> str:
    """Grouped by arc id in the bible's arc order; beats with no known arc last, under 'new'."""
    arcs = {a["id"]: a for a in bible.get("arcs") or [] if a.get("id")}
    groups: dict[str, list[dict]] = {}
    for x in items:
        groups.setdefault(x["arc"] if x["arc"] in arcs else "new", []).append(x)
    order = [a for a in arcs if a in groups] + (["new"] if "new" in groups else [])
    out = []
    for gid in order:
        a = arcs.get(gid)
        out.append(f"ARC {gid}: {a['name']} (eps {', '.join(map(str, a.get('episodes', [])))})" if a
                   else "ARC new (threads not in the bible's arc list)")
        for x in groups[gid]:
            out.append(f"  {x['id']} (Ep {x['ep']}, {x['kind']}): {x['what']}\n{_context_rows(show, x['id'])}")
        out.append("")
    return "\n".join(out).rstrip()


def validate_stitches(show: Show, res: LinkResult, arcs: list[str] | None = None) -> tuple[LinkResult, list[dict]]:
    keep, dropped = [], []
    known = set(arcs or [])
    for st in res.stitches:
        segs = st.segments
        ok = all(show.block(s.start_id) and show.block(s.end_id) and ep_of(s.start_id) == ep_of(s.end_id)
                 and show.block(s.start_id).start < show.block(s.end_id).end - EPS for s in segs)
        why = "bad_stitch_ids"
        if ok and len({ep_of(s.start_id) for s in segs}) != len(segs):
            ok, why = False, "two_segments_from_one_episode"
        if ok:
            first, last = segs[0], segs[-1]
            h, c = show.block(st.hook_id), show.block(st.cliff_id)
            ok = (h is not None and c is not None and ep_of(st.hook_id) == ep_of(first.start_id)
                  and ep_of(st.cliff_id) == ep_of(last.end_id)
                  and show.block(first.start_id).start - EPS <= h.start <= show.block(first.end_id).end + EPS
                  and show.block(last.start_id).start - EPS <= c.start <= show.block(last.end_id).end + EPS)
        if ok:
            if st.arc not in known:
                st.arc = "new"
            keep.append(st)
        else:
            dropped.append({"why": why, "stitch": st.model_dump()})
    res.stitches = keep
    return res, dropped


def link_arcs(llm: LLM, show: Show, beats: list[dict], bible: dict, cfg: dict, max_stitches: int = 6) -> dict:
    items = collect_beats(beats)
    if len({x["ep"] for x in items}) < 2:
        return {"kind": "link", "result": LinkResult().model_dump(), "dropped": [],
                "skipped": "fewer than 2 episodes with setups/payoffs", "model": llm.model_for("link"),
                "prompt_version": cfg["prompts"]["link"]}
    tropes, hook_types = show_tropes(bible, cfg), list(cfg["vocab"]["hook_types"])
    system, user_t = load_prompt(cfg["prompts"]["link"])
    system = fill(system, max_stitches=max_stitches, free_window=cfg["rank"]["free_window"],
                  hook_types=", ".join(hook_types), tropes=", ".join([*tropes, "other"]))
    user = fill(user_t, bible=bible_brief(bible, with_spoilers=True), beats=beats_text(show, items, bible))
    res = llm.complete_json(stage="link", system=system, user=user, schema=link_schema(tropes, hook_types),
                            offline_fn=LinkResult)
    res, dropped = validate_stitches(show, res, arc_ids(bible))
    return {"kind": "link", "result": res.model_dump(), "dropped": dropped, "model": llm.model_for("link"),
            "prompt_version": cfg["prompts"]["link"]}
