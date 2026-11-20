"""Stage 1: cheap heuristic features, no LLM.

Wired into four places:
  * mining prompt: inline tags beside IDs, e.g. [SFX:slap] [ACT:grabs] [EMO:furious]
  * --fast mode: episode heat picks which episodes to mine
  * pre-rank: mining confidence x peak intensity decides which candidates reach the judge
  * editor flag: high heuristic intensity + low judge score = "check this one"
Also computes P (pace, 1-5), the only rubric dimension owned by code.

Emotion tiers were built from the 615 distinct emotion tags found by audit_pack.py.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from .parse import EPS, Block, Episode, Show, load_show

EMO_TIERS: dict[float, set[str]] = {
    1.0: {"furious", "enraged", "screaming", "shouting", "roaring", "shrieking", "hysterical", "horrified",
          "terrified", "panicked", "menacing", "threatening", "venomous", "deadly", "vicious", "outraged",
          "frantic", "savage", "livid", "seething", "murderous", "thunderous", "agonized", "anguished",
          "desperate", "sobbing", "crying out", "explosive", "snarl", "snarling", "violent"},
    0.75: {"cruel", "mocking", "taunting", "sneering", "scornful", "gloating", "spiteful", "contemptuous",
           "cold", "icy", "steely", "defiant", "accusing", "accusatory", "condemning", "sinister", "chilling",
           "ruthless", "vengeful", "vindictive", "malicious", "smug", "arrogant", "condescending",
           "dismissive", "derisive", "scathing", "cutting", "biting", "harsh", "shocked", "stunned",
           "weeping", "heartbroken", "tearful", "pleading", "begging", "trembling", "shaking", "breathless",
           "gasping", "choked", "broken", "shattered", "devastated", "betrayed", "disgusted", "indignant",
           "callous", "dangerous", "unforgiving", "booming", "commanding", "demanding", "warning", "ominous",
           "alarmed", "fearful", "imperious", "haughty", "humiliated", "triumphant", "victorious", "snide",
           "cutting", "gleeful", "boastful", "scoffing", "vicious", "brutal", "aggressive", "crying"},
    0.5: {"firm", "stern", "urgent", "grave", "authoritative", "sharp", "resolute", "fierce", "intense",
          "tense", "strained", "suspicious", "skeptical", "anxious", "nervous", "hesitant", "whispering",
          "hushed", "dark", "grim", "solemn", "determined", "unyielding", "unwavering", "protective",
          "challenging", "vowing", "final", "bitter", "resentful", "incredulous", "disbelieving", "confused",
          "startled", "surprised", "calculating", "scheming", "piercing", "powerful", "steadfast",
          "uncompromising", "unflinching", "decisive", "relentless", "pained", "emotional", "vulnerable"},
    0.2: {"gentle", "soft", "tender", "warm", "fond", "playful", "amused", "calm", "reassuring",
          "nostalgic", "loving", "sincere", "earnest", "relieved", "grateful", "peaceful", "comforting",
          "caring", "compassionate", "reflective", "curious", "inquiring", "pleased", "humble", "intimate",
          "devoted", "quiet", "softly", "content", "dry", "teasing", "droll", "matter-of-fact", "steady"},
}
_EMO_WEIGHT = {w: tier for tier, words in EMO_TIERS.items() for w in words}

SFX_WORDS = {"slap": 1.0, "punch": 1.0, "gunshot": 1.0, "smash": 0.9, "shatter": 0.9, "scream": 0.9,
             "crash": 0.8, "whip": 0.8, "kick": 0.8, "sword": 0.8, "blade": 0.8, "steel": 0.7, "slam": 0.7,
             "bang": 0.7, "crack": 0.7, "thud": 0.6, "gasp": 0.6, "glass": 0.6, "rip": 0.6, "tear": 0.5,
             "clatter": 0.5, "thunder": 0.5, "gavel": 0.6, "bell": 0.4, "door": 0.3}
ACTION_WORDS = {"slap": 1.0, "punch": 1.0, "choke": 1.0, "throttl": 1.0, "stab": 1.0, "strike": 0.9,
                "smash": 0.9, "shatter": 0.9, "draws a sword": 0.9, "draws a dagger": 0.9, "unsheath": 0.9,
                "shove": 0.8, "drag": 0.8, "pin": 0.8, "spit": 0.8, "collapse": 0.8, "faint": 0.8,
                "kiss": 0.8, "blood": 0.8, "arrest": 0.8, "seize": 0.7, "grab": 0.7, "yank": 0.7,
                "hurl": 0.8, "throw": 0.7, "rip": 0.7, "restrain": 0.7, "stomp": 0.7, "push": 0.6,
                "kneel": 0.6, "falls": 0.6, "tears": 0.5, "pours": 0.6, "raises his hand": 0.6,
                "raises her hand": 0.6, "steps on": 0.6, "grinds": 0.6, "embrace": 0.5, "toss": 0.5,
                "catch": 0.4, "slams": 0.7}
CAMERA_WORDS = {"extreme close-up": 0.5, "extreme close up": 0.5, "crash zoom": 0.5, "snap zoom": 0.5,
                "dutch": 0.4, "whip pan": 0.4, "slow motion": 0.4, "low-angle": 0.3, "low angle": 0.3,
                "push in": 0.2, "pushes in": 0.2, "handheld": 0.2}
INSULT_WORDS = {"beggar", "pathetic", "worthless", "trash", "filth", "filthy", "peasant", "wretch",
                "disgrace", "shameless", "cripple", "monster", "beast", "freak", "hideous", "ugly", "vermin",
                "rat", "harlot", "witch", "poisoner", "thief", "liar", "traitor", "nobody", "fool", "pauper",
                "useless", "scum", "dog", "brute", "savage", "commoner", "lowly", "ruined", "mud"}
REVEAL_WORDS = {"duke", "your grace", "his grace", "kneel", "bow", "arrest", "guards", "kill", "die", "dead",
                "murder", "poison", "divorce", "annul", "betray", "truth", "secret", "lied", "pregnant",
                "heir", "disinherit", "banish", "exile", "hang", "execute", "revenge", "pay for this",
                "how dare", "who are you", "dead to", "never", "confess", "proof", "evidence", "trial"}


def _contains(text: str, word: str) -> bool:
    return re.search(r"(?<![a-z])" + re.escape(word), text) is not None


def _best(text: str, table: dict[str, float]) -> tuple[float, str]:
    low = text.lower()
    hits = [(w, k) for k, w in table.items() if _contains(low, k)]
    return max(hits) if hits else (0.0, "")


def emotion_weight(emotions: list[str]) -> tuple[float, str]:
    best = (0.35 if emotions else 0.3, "")
    for e in emotions:
        if e in _EMO_WEIGHT:
            w = _EMO_WEIGHT[e]
        else:  # multi-word tags such as "quietly resolute", "choked with tears"
            ws = [_EMO_WEIGHT[x] for x in re.split(r"[\s\-]+", e) if x in _EMO_WEIGHT]
            w = max(ws) if ws else 0.35
        if w > best[0]:
            best = (w, e)
    return best


def block_intensity(b: Block) -> tuple[float, list[str]]:
    """Intensity in [0, 1] plus short tags for one block."""
    tags: list[str] = []
    if b.kind == "line":
        w, emo = emotion_weight(b.emotions)
        if emo and w >= 0.75:
            tags.append(f"EMO:{emo}")
        low = b.text.lower()
        insult = [x for x in INSULT_WORDS if _contains(low, x)]
        reveal = [x for x in REVEAL_WORDS if _contains(low, x)]
        if insult:
            tags.append("INSULT")
            w = max(w, 0.7)
        if reveal:
            tags.append("STAKES")
            w = max(w, 0.65)
        if b.text.rstrip().endswith("!"):
            w = min(1.0, w + 0.1)
        return min(w, 1.0), tags
    if b.kind == "sfx":
        w, k = _best(b.text, SFX_WORDS)
        if k:
            tags.append(f"SFX:{k}")
        return max(w, 0.3), tags
    if b.kind == "action":
        w, k = _best(b.text, ACTION_WORDS)
        if k:
            tags.append(f"ACT:{k.split()[0]}")
        return max(w, 0.2), tags
    if b.kind == "shot":
        w, k = _best(b.text, CAMERA_WORDS)
        if k:
            tags.append("CAM:" + ("ECU" if "extreme" in k else k.replace(" ", "-")))
        return w, tags
    return 0.0, tags


def episode_features(ep: Episode) -> dict:
    blocks = [*ep.lines, *ep.actions, *ep.sfx, *ep.shots]
    per_block = {b.id: block_intensity(b) for b in blocks}
    cells = {}
    for c in ep.shots:
        inside = [b for b in blocks if b.kind != "shot" and b.start < c.end - EPS and b.end > c.start + EPS]
        w = max([per_block[b.id][0] for b in inside] + [0.0])
        w = min(1.0, w + 0.5 * per_block[c.id][0])  # camera grammar boosts what is on screen
        cells[c.id] = round(w, 3)
    top = sorted(cells.values(), reverse=True)[:5]
    tail = [cells[c.id] for c in ep.shots if c.end > ep.duration - 15.0]
    header = (ep.logline + " " + ep.continuity).lower()
    header_hits = sum(_contains(header, w) for w in
                      ("slap", "humiliat", "reveal", "confront", "betray", "expose", "attack", "arrest",
                       "threat", "duel", "kiss", "accus", "mock", "fight", "wedding", "trial", "poison"))
    hot_share = sum(v >= 0.75 for v in cells.values()) / max(len(cells), 1)
    heat = (0.35 * (sum(top) / max(len(top), 1)) + 0.30 * min(1.0, hot_share / 0.5)
            + 0.20 * max(tail + [0.0]) + 0.15 * min(1.0, header_hits / 3))
    return {"ep": ep.ep, "heat": round(heat, 3), "cells": cells,
            "blocks": {k: {"w": round(v[0], 3), "tags": v[1]} for k, v in per_block.items()}}


def show_features(show: Show) -> dict[int, dict]:
    return {n: episode_features(show.ep(n)) for n in show.episode_numbers}


def load_features(parsed_path: str) -> dict[int, dict]:
    p = Path(parsed_path).resolve()
    return _features_cached(str(p), p.stat().st_mtime_ns)


@lru_cache(maxsize=4)
def _features_cached(path: str, _mtime: int) -> dict[int, dict]:
    return show_features(load_show(path))


def peak_intensity(feats: dict[int, dict], show: Show, segments: list[dict]) -> float:
    best = 0.0
    for s in segments:
        ep = show.ep(s["ep"])
        for c in ep.shots:
            if c.start < s["end"] - EPS and c.end > s["start"] + EPS:
                best = max(best, feats[s["ep"]]["cells"].get(c.id, 0.0))
    return round(best, 3)


def pace(show: Show, cand: dict) -> tuple[float, dict]:
    """P on 1-5: hook latency, dialogue density, longest dead-air stretch (no line/action/sfx)."""
    hook_at = cand["hook"]["at_s"]
    covered: list[tuple[float, float]] = []
    n_lines = 0
    offset = 0.0
    for s in cand["segments"]:
        ep = show.ep(s["ep"])
        for b in [*ep.lines, *ep.actions, *ep.sfx]:
            a, z = max(b.start, s["start"]), min(b.end, s["end"])
            if z > a + EPS:
                covered.append((offset + a - s["start"], offset + z - s["start"]))
                n_lines += b.kind == "line"
        offset += s["end"] - s["start"]
    covered.sort()
    dead, cursor = 0.0, 0.0
    for a, z in covered:
        dead = max(dead, a - cursor)
        cursor = max(cursor, z)
    dead = max(dead, offset - cursor)
    per10 = n_lines / max(offset, 1e-6) * 10
    p = 5.0
    p -= 0.0 if hook_at <= 1.0 else 0.5 if hook_at <= 2.0 else 1.0
    p -= 0.0 if dead <= 2.0 else min(1.5, 0.5 * ((dead - 2.0) // 2 + 1))
    p -= 0.0 if per10 >= 2.0 else 0.5 if per10 >= 1.0 else 1.0
    return max(1.0, round(p, 2)), {"hook_at_s": hook_at, "dead_air_s": round(dead, 2),
                                   "lines_per_10s": round(per10, 2)}
