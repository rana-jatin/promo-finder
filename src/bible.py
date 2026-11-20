"""Stage 2: story bible from the 50 episode headers (1 LLM call). Feeds mining and linking with show
context, the select stage with late-twist spoilers, and the cold judge with appearance-only
descriptors (so visual rows say "the scarred soldier", never a name a stranger was not told)."""
from __future__ import annotations

import re
from collections import Counter

from .llm import LLM, fill, load_prompt
from .parse import Show
from .schemas import Bible, Character

GENERIC_TITLES = {"lady", "lord", "duke", "duchess", "sir", "father", "mother", "king", "queen", "prince",
                  "princess", "his grace", "her grace", "your grace", "madam", "miss", "master", "the duke"}


def headers_text(show: Show) -> str:
    return "\n".join(f"E{n:02d}: {show.ep(n).logline} | {show.ep(n).continuity}" for n in show.episode_numbers)


def offline_bible(show: Show) -> Bible:
    speakers = Counter(ln.speaker for n in show.episode_numbers for ln in show.ep(n).lines)
    chars = [Character(name=s.title(), role="(offline: frequent speaker)") for s, _ in speakers.most_common(8)]
    first = show.ep(show.episode_numbers[0])
    return Bible(premise=first.logline, logline_for_strangers=first.logline[:160], characters=chars)


def run_bible(llm: LLM, show: Show, cfg: dict) -> dict:
    system, user_t = load_prompt(cfg["prompts"]["bible"])
    system = fill(system, free_window=cfg["rank"]["free_window"])
    user = fill(user_t, title=show.title, n_episodes=len(show.episodes), headers=headers_text(show))
    res = llm.complete_json(stage="bible", system=system, user=user, schema=Bible,
                            offline_fn=lambda: offline_bible(show))
    out = res.model_dump()
    out["prompt_version"] = cfg["prompts"]["bible"]
    out["model"] = llm.model_for("bible")
    return out


def bible_brief(bible: dict, with_spoilers: bool = False) -> str:
    """Compact show context for the mining and linking prompts (never for the judge)."""
    lines = [f"Premise: {bible.get('premise', '')}"]
    chars = bible.get("characters") or []
    if chars:
        lines.append("Characters: " + "; ".join(
            f"{c['name']}" + (f" (aka {', '.join(c['aliases'])})" if c.get("aliases") else "")
            + (f": {c['role']}" if c.get("role") else "") for c in chars[:12]))
    if bible.get("tropes"):
        lines.append("Tropes: " + ", ".join(bible["tropes"]))
    for a in (bible.get("arcs") or [])[:8]:
        lines.append(f"Arc '{a['name']}' (eps {', '.join(map(str, a.get('episodes', [])))}): {a.get('summary', '')}")
    if with_spoilers:
        for s in bible.get("spoilers") or []:
            lines.append(f"Late twist (Ep {s['episode']}): {s['reveal']}")
    return "\n".join(lines)


def visual_aliases(bible: dict) -> list[tuple[str, str]]:
    """(name or alias, appearance-only descriptor) pairs, longest name first, for the cold render."""
    pairs: list[tuple[str, str]] = []
    for c in bible.get("characters") or []:
        desc = (c.get("visual_descriptor") or "").strip()
        if not desc:
            continue
        desc = re.sub(r"^(the|a|an)\s+", "", desc, flags=re.I)
        for name in [c.get("name", ""), *(c.get("aliases") or [])]:
            name = name.strip()
            if len(name) >= 3 and name.lower() not in GENERIC_TITLES:
                pairs.append((name, desc))
    pairs.sort(key=lambda p: -len(p[0]))
    return pairs
