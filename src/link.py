"""Stage 4 (template 4): link mined setups and payoffs across the whole season into arc stitches
(1 global LLM call; a 5-episode window would never see Ep 8 and Ep 22 together)."""
from __future__ import annotations

from .bible import bible_brief
from .llm import LLM, fill, load_prompt
from .parse import EPS, Show, ep_of
from .schemas import LinkResult

MAX_BEATS = 80
NEAR_S = 8.0


def _context_rows(show: Show, bid: str) -> str:
    b = show.block(bid)
    ep = show.ep(b.ep)
    rows = []
    for ln in ep.lines:
        if b.start - NEAR_S <= ln.start <= b.end + NEAR_S:
            rows.append(f'    {ln.id} [{ln.start:.1f}] {ln.speaker}: "{ln.text}"')
    if b.kind != "line":
        rows.insert(0, f"    {b.id} [{b.start:.1f}] {b.kind.upper()}: {b.text}")
    return "\n".join(rows)


def collect_beats(beats: list[dict]) -> list[dict]:
    out = []
    for rec in beats:
        if rec.get("kind") != "mine":
            continue
        for kind in ("setups", "payoffs"):
            for x in rec["result"].get(kind) or []:
                out.append({"id": x["id"], "ep": rec["ep"], "kind": kind[:-1], "what": x.get("what", "")})
    out.sort(key=lambda x: (x["ep"], x["id"]))
    return out[:MAX_BEATS]


def validate_stitches(show: Show, res: LinkResult) -> tuple[LinkResult, list[dict]]:
    keep, dropped = [], []
    for st in res.stitches:
        segs = st.segments
        ok = all(show.block(s.start_id) and show.block(s.end_id) and ep_of(s.start_id) == ep_of(s.end_id)
                 and show.block(s.start_id).start < show.block(s.end_id).end - EPS for s in segs)
        ok = ok and len({ep_of(s.start_id) for s in segs}) >= 2
        if ok:
            first, last = segs[0], segs[-1]
            h, c = show.block(st.hook_id), show.block(st.cliff_id)
            ok = (h is not None and c is not None and ep_of(st.hook_id) == ep_of(first.start_id)
                  and ep_of(st.cliff_id) == ep_of(last.end_id)
                  and show.block(first.start_id).start - EPS <= h.start <= show.block(first.end_id).end + EPS
                  and show.block(last.start_id).start - EPS <= c.start <= show.block(last.end_id).end + EPS)
        (keep if ok else dropped).append(st if ok else {"why": "bad_stitch_ids", "stitch": st.model_dump()})
    res.stitches = keep
    return res, dropped


def link_arcs(llm: LLM, show: Show, beats: list[dict], bible: dict, cfg: dict, max_stitches: int = 6) -> dict:
    items = collect_beats(beats)
    if len({x["ep"] for x in items}) < 2:
        return {"kind": "link", "result": LinkResult().model_dump(), "dropped": [],
                "skipped": "fewer than 2 episodes with setups/payoffs", "model": llm.model_for("link")}
    text = "\n".join(f"{x['id']} (Ep {x['ep']}, {x['kind']}): {x['what']}\n{_context_rows(show, x['id'])}"
                     for x in items)
    system, user_t = load_prompt(cfg["prompts"]["link"])
    system = fill(system, max_stitches=max_stitches)
    user = fill(user_t, bible=bible_brief(bible), beats=text)
    res = llm.complete_json(stage="link", system=system, user=user, schema=LinkResult, offline_fn=LinkResult)
    res, dropped = validate_stitches(show, res)
    return {"kind": "link", "result": res.model_dump(), "dropped": dropped, "model": llm.model_for("link"),
            "prompt_version": cfg["prompts"]["link"]}
