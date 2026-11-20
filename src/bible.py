"""Stage 2: story bible from the 50 episode headers (1 LLM call). Feeds mining and linking with show
context (arc ids, tropes, spoilers), the select stage with late-twist spoilers, and the cold clip
renderer with each character's names and an appearance-only descriptor (so a stranger only meets a
name that someone in the clip says aloud)."""
from __future__ import annotations

import re
from collections import Counter

from .llm import LLM, ask, provenance
from .parse import Show
from .schemas import Bible, Character

GENERIC_TITLES = {"lady", "lord", "duke", "duchess", "sir", "father", "mother", "king", "queen", "prince",
                  "princess", "his grace", "her grace", "your grace", "madam", "miss", "master", "the duke"}
# leading forms of address: "Lady Bianca" is also "Bianca", "Father Thomas" is also "Thomas"
HONORIFICS = {"the", "lady", "lord", "sir", "duke", "duchess", "dowager", "madam", "madame", "miss", "mr", "mrs",
              "ms", "master", "prince", "princess", "king", "queen", "father", "sister", "brother", "uncle", "aunt",
              "dr", "doctor", "young", "old"}


def headers_text(show: Show) -> str:
    return "\n".join(f"E{n:02d}: {show.ep(n).logline} | {show.ep(n).continuity}" for n in show.episode_numbers)


def offline_bible(show: Show) -> Bible:
    speakers = Counter(ln.speaker for n in show.episode_numbers for ln in show.ep(n).lines)
    chars = [Character(name=s.title(), role="(offline: frequent speaker)") for s, _ in speakers.most_common(8)]
    first = show.ep(show.episode_numbers[0])
    return Bible(premise=first.logline, logline_for_strangers=first.logline[:160], characters=chars)


def run_bible(llm: LLM, show: Show, cfg: dict) -> dict:
    vocab = cfg["vocab"]["tropes"]
    res = ask(llm, cfg, "bible", Bible, lambda: offline_bible(show),
              system_vars={"free_window": cfg["rank"]["free_window"], "trope_vocab": ", ".join(vocab)},
              user_vars={"title": show.title, "n_episodes": len(show.episodes), "headers": headers_text(show)})
    out = res.model_dump()
    # tropes are a closed list: anything off-list is dropped here (and kept on record), never passed downstream
    out["tropes"] = list(dict.fromkeys(t for t in out["tropes"] if t in set(vocab)))
    off = [t for t in res.tropes if t not in set(vocab)]
    if off:
        out["tropes_off_vocab"] = off
    seen: set[str] = set()
    for a in out["arcs"]:  # arc ids key the setup/payoff grouping, so they must be unique
        base, k = a["id"], 2
        while a["id"] in seen:
            a["id"], k = f"{base}_{k}", k + 1
        seen.add(a["id"])
    out.update(provenance(llm, cfg, "bible"))
    return out


def show_tropes(bible: dict, cfg: dict) -> list[str]:
    """{{tropes}} for mining and linking: the bible's picks (falls back to the whole list if it picked none)."""
    return list(bible.get("tropes") or []) or list(cfg["vocab"]["tropes"])


def vocab_vars(bible: dict, cfg: dict) -> tuple[list[str], list[str], dict]:
    """(tropes, hook_types, prompt vars) shared by mining and linking: the schema enums and the prompt lists
    must name the same labels."""
    tropes, hook_types = show_tropes(bible, cfg), list(cfg["vocab"]["hook_types"])
    return tropes, hook_types, {"hook_types": ", ".join(hook_types), "tropes": ", ".join([*tropes, "other"])}


def arc_ids(bible: dict) -> list[str]:
    return [a["id"] for a in bible.get("arcs") or [] if a.get("id")]


def bible_brief(bible: dict, with_spoilers: bool = False) -> str:
    """Compact show context for the mining and linking prompts (never for the judge)."""
    lines = [f"Premise: {bible.get('premise', '')}"]
    chars = bible.get("characters") or []
    if chars:
        lines.append("Characters:")
        for c in chars[:12]:
            bits = [c["name"] + (f" (aka {', '.join(c['aliases'])})" if c.get("aliases") else "")]
            bits += [x for x in (c.get("role"), c.get("relationship")) if x]
            lines.append("- " + "; ".join(bits))
    if bible.get("tropes"):
        lines.append("Tropes: " + ", ".join(bible["tropes"]))
    arcs = (bible.get("arcs") or [])[:8]
    if arcs:
        lines.append("Arcs:")
        for a in arcs:
            lines.append(f"- {a.get('id', '')}: {a['name']} (eps {', '.join(map(str, a.get('episodes', [])))}): "
                         f"{a.get('summary', '')}")
    if with_spoilers and bible.get("spoilers"):
        lines.append("Spoilers (never show these reveals):")
        for s in bible["spoilers"]:
            lines.append(f"- Ep {s['episode']}: {s['reveal']}")
    return "\n".join(lines)


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9' ]+", " ", (s or "").lower()).split())


def strip_honorifics(s: str) -> str:
    words = _norm(s).split()
    while len(words) > 1 and words[0] in HONORIFICS:
        words = words[1:]
    return " ".join(words)


def clip_cast(bible: dict) -> list[dict]:
    """One entry per bible character for the cold clip renderer (JSON-safe, so it rides in graph payloads):
      labels  every name and alias, normalised: matches a script speaker label to this person
      names   the forms that count as this person's name being SAID ALOUD (generic titles such as
              "Your Grace" excluded), longest first; also what gets replaced in VISUAL rows
      desc    appearance-only descriptor, without a leading article ("" if the bible gave none)"""
    cast = []
    for c in bible.get("characters") or []:
        raw = [c.get("name", ""), *(c.get("aliases") or [])]
        labels = list(dict.fromkeys(x for x in (_norm(r) for r in raw) if x))
        names = set()
        for r in raw:
            for form in (_norm(r), strip_honorifics(r)):
                if len(form) >= 3 and form not in GENERIC_TITLES:
                    names.add(form)
        desc = re.sub(r"^(the|a|an)\s+", "", (c.get("visual_descriptor") or "").strip(), flags=re.I)
        cast.append({"labels": labels, "names": sorted(names, key=lambda x: (-len(x), x)), "desc": desc})
    return cast
