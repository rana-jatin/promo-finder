"""Stage 3: moment mining, one LLM call per episode (fan-out). Sees Ep N + the first ~20 s of Ep N+1 +
the bible + Stage 1 tags; returns hooks, cliffhangers, an optional bridge, setups and payoffs (tagged with
the bible's arc ids), or no_promo. The LLM cites IDs only; every cited ID is checked here and bad ones are
dropped + logged. hook_type and trope are schema enums built from config.vocab and the bible's tropes."""
from __future__ import annotations

from typing import get_args

from .bible import arc_ids, bible_brief, vocab_vars
from .features import block_intensity, ends_open
from .llm import LLM, ask, provenance
from .parse import EPS, Block, Episode, Show, ep_of
from .schemas import MineResult, Moment, mine_schema


def render_timeline(ep: Episode, feats: dict | None, lo: float = 0.0, hi: float | None = None,
                    indent: str = "", kinds: tuple[str, ...] = ("shot", "line", "action", "sfx")) -> str:
    """Rows: ID [seconds from episode start] content [tags]."""
    hi = ep.duration if hi is None else hi
    rows = []
    for b in ep.blocks_overlapping(lo, hi, kinds=kinds):
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
        rows.append(f"{indent}{b.id} [{b.start:.1f}] {body}{tag_s}")
    return "\n".join(rows)


def _ok(show: Show, bid: str | None, eps: set[int]) -> bool:
    return bool(bid) and ep_of(bid) in eps and show.block(bid) is not None


def _legacy_bridge(res: MineResult) -> None:
    """v1 prompts returned the bridge as its own field: fold it into the moments list as template 'bridge'."""
    b = res.bridge
    if b is not None:
        cls = get_args(type(res).model_fields["moments"].annotation)[0]  # the show's vocab-enum Moment
        res.moments.append(cls(template="bridge", start_id=b.start_id, hook_id=b.hook_id or "",
                               next_start_id=b.next_start_id, next_cliff_id=b.next_cliff_id, trope=b.trope,
                               intended_question=b.open_question, confidence=b.confidence))
        res.bridge = None


def _check_bridge(show: Show, m: Moment, ep_n: int, next_n: int | None) -> str | None:
    if next_n is None:
        return "bridge_without_next_episode"
    m.hook_id = m.hook_id or m.start_id
    m.cliff_id, m.peak_id = "", None
    if not (_ok(show, m.start_id, {ep_n}) and _ok(show, m.hook_id, {ep_n}) and _ok(show, m.next_cliff_id, {next_n})
            and (m.next_start_id is None or _ok(show, m.next_start_id, {next_n}))):
        return "bad_bridge_ids"
    if show.block(m.hook_id).start < show.block(m.start_id).start - EPS:
        return "beats_out_of_order"
    if m.next_start_id and show.block(m.next_start_id).start >= show.block(m.next_cliff_id).end - EPS:
        return "beats_out_of_order"
    return None


def _check_moment(show: Show, m: Moment, ep_n: int) -> str | None:
    if m.template == "cold_open" and not m.peak_id:
        m.template = "continuous"
    if m.template == "cold_open":
        m.hook_id = m.peak_id  # the flashed peak IS the hook
    else:
        m.peak_id = None
        m.hook_id = m.hook_id or m.start_id
    m.next_start_id = m.next_cliff_id = None
    ids = [m.start_id, m.hook_id, m.cliff_id]
    if not all(_ok(show, i, {ep_n}) for i in ids):
        return "unknown_or_foreign_id"
    s, h, c = (show.block(i) for i in ids)
    if not (s.start <= h.start + EPS and h.start <= c.end + EPS and s.start < c.end):
        return "beats_out_of_order"
    if m.template == "cold_open" and h.start >= c.start - EPS:  # start <= peak < cliff
        return "cold_open_cliff_not_after_peak"
    return None


def validate_mine(show: Show, ep_n: int, next_n: int | None, res: MineResult,
                  arcs: list[str] | None = None) -> tuple[MineResult, list[dict]]:
    """Keep only beats whose IDs exist and sit in the right episode and order; at most one bridge."""
    _legacy_bridge(res)
    dropped: list[dict] = []
    keep: list[Moment] = []
    bridges = 0
    for m in res.moments:
        if m.template == "bridge":
            why = "extra_bridge" if bridges else _check_bridge(show, m, ep_n, next_n)
        else:
            why = _check_moment(show, m, ep_n)
        if why:
            dropped.append({"why": why, "moment": m.model_dump()})
            continue
        bridges += m.template == "bridge"
        keep.append(m)
    res.moments = keep
    known = set(arcs or [])
    for kind in ("setups", "payoffs"):
        notes = [x for x in getattr(res, kind) if _ok(show, x.id, {ep_n})]
        for x in notes:
            if x.arc not in known:
                x.arc = "new"
        setattr(res, kind, notes)
    if not res.moments and not res.no_promo:
        res.no_promo_reason = res.no_promo_reason or "all proposed moments failed ID checks"
    return res, dropped


def offline_mine(show: Show, ep: Episode, feats: dict, cfg: dict) -> MineResult:
    """No-LLM baseline: strongest keyword beat as hook, the most 'open' line 18-45 s later as cliff."""
    P = cfg["promo"]
    scored = sorted(((block_intensity(b)[0], b) for b in ep.beats),
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
            return block_intensity(ln)[0] + (0.3 if ends_open(ln.text) else 0.0)

        if not cands:
            continue
        cliff = max(cands, key=lambda ln: (openness(ln), ln.end))
        moments.append(Moment(template="continuous", start_id=hook.id, hook_id=hook.id, cliff_id=cliff.id,
                              hook_type="other", trope="other", intended_question="",
                              confidence=round(min(1.0, w * 0.8), 2)))
        used.append((hook.start, cliff.end))
    return MineResult(no_promo=not moments, no_promo_reason="" if moments else "no beat above 0.75 intensity",
                      moments=moments)


def mine_episode(llm: LLM, show: Show, ep_n: int, next_n: int | None, bible: dict, feats: dict,
                 cfg: dict) -> dict:
    ep = show.ep(ep_n)
    preview = cfg["promo"]["next_ep_preview_s"]
    tropes, hook_types, vocab = vocab_vars(bible, cfg)
    nxt = show.ep(next_n) if next_n is not None and next_n in show.episodes else None
    user = {"bible": bible_brief(bible), "ep": ep_n, "duration": f"{ep.duration:.1f}",
            "timeline": render_timeline(ep, feats.get(ep_n)), "preview_s": int(preview),
            "next_ep": next_n if nxt else "N/A",
            "next_timeline": render_timeline(nxt, feats.get(next_n), 0.0, preview) if nxt else "NONE"}
    res = ask(llm, cfg, "mine", mine_schema(tropes, hook_types),
              lambda: offline_mine(show, ep, feats.get(ep_n, {}), cfg), system_vars=vocab, user_vars=user)
    res, dropped = validate_mine(show, ep_n, next_n if nxt else None, res, arc_ids(bible))
    return {"kind": "mine", "ep": ep_n, "next_ep": next_n if nxt else None, "result": res.model_dump(),
            "dropped": dropped, **provenance(llm, cfg, "mine")}
