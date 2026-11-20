"""Stage 3: moment mining, one LLM call per episode (fan-out). Sees Ep N + the first ~20 s of Ep N+1 +
the bible + Stage 1 tags; returns hooks, cliffhangers, an optional bridge, setups and payoffs, or
no_promo. The LLM cites IDs only; every cited ID is checked here and bad ones are dropped + logged."""
from __future__ import annotations

from .bible import bible_brief
from .features import block_intensity
from .llm import LLM, fill, load_prompt
from .parse import EPS, Block, Episode, Show, ep_of
from .schemas import MineResult, Moment


def render_timeline(ep: Episode, feats: dict | None, lo: float = 0.0, hi: float | None = None) -> str:
    hi = ep.duration if hi is None else hi
    rows = []
    for b in ep.blocks_overlapping(lo, hi):
        if b.start >= hi - EPS:
            continue
        tags = (feats or {}).get("blocks", {}).get(b.id, {}).get("tags") or []
        tag_s = (" " + " ".join(f"[{t}]" for t in tags)) if tags else ""
        if b.kind == "line":
            emo = f" ({', '.join(b.emotions)})" if b.emotions else ""
            mod = f" ({', '.join(b.modifiers)})" if b.modifiers else ""
            body = f'{b.speaker}{mod}{emo}: "{b.text}"'
        elif b.kind == "shot":
            body = "CAM: " + ("(no description)" if b.synthetic else b.text)
        else:
            body = {"action": "ACT: ", "sfx": "SFX: "}.get(b.kind, "NOTE: ") + b.text
        rows.append(f"{b.id} [{b.start:.1f}] {body}{tag_s}")
    return "\n".join(rows)


def _ok(show: Show, bid: str | None, eps: set[int]) -> bool:
    return bool(bid) and ep_of(bid) in eps and show.block(bid) is not None


def validate_mine(show: Show, ep_n: int, next_n: int | None, res: MineResult) -> tuple[MineResult, list[dict]]:
    """Keep only beats whose IDs exist and sit in the right episode and order."""
    dropped: list[dict] = []
    keep: list[Moment] = []
    for m in res.moments:
        ids = [m.start_id, m.hook_id, m.cliff_id] + ([m.peak_id] if m.template == "cold_open" else [])
        if m.template == "cold_open" and not m.peak_id:
            m.template, m.peak_id = "continuous", None
            ids = ids[:3]
        if not all(_ok(show, i, {ep_n}) for i in ids):
            dropped.append({"why": "unknown_or_foreign_id", "moment": m.model_dump()})
            continue
        s, h, c = (show.block(i) for i in (m.start_id, m.hook_id, m.cliff_id))
        if not (s.start <= h.start + EPS and h.start <= c.end + EPS and s.start < c.end):
            dropped.append({"why": "beats_out_of_order", "moment": m.model_dump()})
            continue
        if m.template == "cold_open" and show.block(m.peak_id).start >= c.start - EPS:
            dropped.append({"why": "cold_open_cliff_not_after_peak", "moment": m.model_dump()})
            continue
        keep.append(m)
    res.moments = keep
    if res.bridge is not None:
        b = res.bridge
        b.hook_id = b.hook_id or b.start_id
        ok = (next_n is not None and _ok(show, b.start_id, {ep_n}) and _ok(show, b.hook_id, {ep_n})
              and _ok(show, b.next_cliff_id, {next_n})
              and (b.next_start_id is None or _ok(show, b.next_start_id, {next_n})))
        if not ok:
            dropped.append({"why": "bad_bridge_ids", "bridge": b.model_dump()})
            res.bridge = None
    res.setups = [x for x in res.setups if _ok(show, x.id, {ep_n})]
    res.payoffs = [x for x in res.payoffs if _ok(show, x.id, {ep_n})]
    if not res.moments and res.bridge is None and not res.no_promo:
        res.no_promo_reason = res.no_promo_reason or "all proposed moments failed ID checks"
    return res, dropped


def offline_mine(show: Show, ep: Episode, feats: dict, cfg: dict) -> MineResult:
    """No-LLM baseline: strongest keyword beat as hook, the most 'open' line 18-45 s later as cliff."""
    P = cfg["promo"]
    scored = sorted(((block_intensity(b)[0], b) for b in [*ep.lines, *ep.actions, *ep.sfx]),
                    key=lambda x: (-x[0], x[1].start))
    moments: list[Moment] = []
    used: list[tuple[float, float]] = []
    for w, hook in scored:
        if w < 0.75 or len(moments) >= 2:
            break
        if any(a - 5 <= hook.start <= b for a, b in used) or hook.start > ep.duration - P["min_dur"]:
            continue
        cands = [ln for ln in ep.lines
                 if hook.start + P["min_dur"] - 3 <= ln.end <= hook.start + P["max_dur"] - 15]

        def openness(ln: Block) -> float:
            t = ln.text.rstrip()
            return block_intensity(ln)[0] + (0.3 if t.endswith(("?", "!", "...", "—", "-")) else 0.0)

        if not cands:
            continue
        cliff = max(cands, key=lambda ln: (openness(ln), ln.end))
        moments.append(Moment(template="continuous", start_id=hook.id, hook_id=hook.id, cliff_id=cliff.id,
                              hook_type="other", trope="other", open_question="",
                              confidence=round(min(1.0, w * 0.8), 2)))
        used.append((hook.start, cliff.end))
    return MineResult(no_promo=not moments, no_promo_reason="" if moments else "no beat above 0.75 intensity",
                      moments=moments)


def mine_episode(llm: LLM, show: Show, ep_n: int, next_n: int | None, bible: dict, feats: dict,
                 cfg: dict) -> dict:
    ep = show.ep(ep_n)
    preview = cfg["promo"]["next_ep_preview_s"]
    system, user_t = load_prompt(cfg["prompts"]["mine"])
    nxt = show.ep(next_n) if next_n is not None else None
    user = fill(user_t, bible=bible_brief(bible), ep=ep_n, duration=f"{ep.duration:.1f}",
                timeline=render_timeline(ep, feats.get(ep_n)), preview_s=int(preview),
                next_ep=next_n if nxt else "(none: last episode)",
                next_timeline=render_timeline(nxt, feats.get(next_n), 0.0, preview) if nxt else "(none)")
    res = llm.complete_json(stage="mine", system=system, user=user, schema=MineResult,
                            offline_fn=lambda: offline_mine(show, ep, feats.get(ep_n, {}), cfg))
    res, dropped = validate_mine(show, ep_n, next_n if nxt else None, res)
    return {"kind": "mine", "ep": ep_n, "next_ep": next_n, "result": res.model_dump(), "dropped": dropped,
            "model": llm.model_for("mine"), "prompt_version": cfg["prompts"]["mine"]}
