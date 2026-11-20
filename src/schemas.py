"""Pydantic schemas for every LLM output. Before-validators absorb cosmetic slips (casing, 'None',
a score of 0 or 6) so they do not cost a corrective retry; anything structural still fails."""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


def _snake(v: object, default: str = "other") -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(v or "").strip().lower()).strip("_")
    return s or default


def _clamp_score(v: object) -> float:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1.0
    return min(5.0, max(1.0, x))


def _clamp01(v: object) -> float:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.5
    if x > 1.0:  # some models answer on 0-10 or 0-100
        x = x / 10 if x <= 10 else x / 100
    return min(1.0, max(0.0, x))


# --------------------------------------------------------------------------- bible
class Character(BaseModel):
    name: str
    aliases: list[str] = []
    role: str = ""
    visual_descriptor: str = ""


class Spoiler(BaseModel):
    episode: int
    reveal: str
    keywords: list[str] = []


class Arc(BaseModel):
    name: str
    episodes: list[int] = []
    summary: str = ""


class Bible(BaseModel):
    premise: str
    logline_for_strangers: str = ""
    characters: list[Character] = []
    tropes: list[str] = []
    spoilers: list[Spoiler] = []
    arcs: list[Arc] = []


# --------------------------------------------------------------------------- mining
HOOK_TYPES = {"slap", "violence", "insult", "humiliation", "reveal", "threat", "confrontation", "kiss",
              "rescue", "betrayal", "shocking_line", "other"}


class Moment(BaseModel):
    template: Literal["continuous", "cold_open"] = "continuous"
    start_id: str
    hook_id: str
    peak_id: Optional[str] = None
    cliff_id: str
    hook_type: str = "other"
    trope: str = "other"
    open_question: str = ""
    confidence: float = 0.5

    @field_validator("template", mode="before")
    @classmethod
    def _tpl(cls, v):
        v = _snake(v, "continuous")
        return v if v in ("continuous", "cold_open") else "continuous"

    @field_validator("hook_type", mode="before")
    @classmethod
    def _ht(cls, v):
        v = _snake(v)
        return v if v in HOOK_TYPES else "other"

    @field_validator("trope", mode="before")
    @classmethod
    def _tr(cls, v):
        return _snake(v)

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v):
        return _clamp01(v)


class Bridge(BaseModel):
    start_id: str
    hook_id: Optional[str] = None
    next_start_id: Optional[str] = None
    next_cliff_id: str
    trope: str = "other"
    open_question: str = ""
    confidence: float = 0.5

    @field_validator("trope", mode="before")
    @classmethod
    def norm_trope(cls, v):
        return _snake(v)

    @field_validator("confidence", mode="before")
    @classmethod
    def norm_conf(cls, v):
        return _clamp01(v)


class BeatNote(BaseModel):
    id: str
    what: str = ""


class MineResult(BaseModel):
    no_promo: bool = False
    no_promo_reason: str = ""
    moments: list[Moment] = []
    bridge: Optional[Bridge] = None
    setups: list[BeatNote] = []
    payoffs: list[BeatNote] = []


# --------------------------------------------------------------------------- linking
class StitchSegment(BaseModel):
    start_id: str
    end_id: str


class Stitch(BaseModel):
    segments: list[StitchSegment] = Field(min_length=2, max_length=4)
    hook_id: str
    cliff_id: str
    trope: str = "other"
    open_question: str = ""
    why: str = ""
    confidence: float = 0.5

    @field_validator("trope", mode="before")
    @classmethod
    def norm_trope(cls, v):
        return _snake(v)

    @field_validator("confidence", mode="before")
    @classmethod
    def norm_conf(cls, v):
        return _clamp01(v)


class LinkResult(BaseModel):
    stitches: list[Stitch] = []


# --------------------------------------------------------------------------- cold judge
FAILURES = ("none", "hook_late", "hook_weak", "answered_in_clip", "no_open_question", "needs_context",
            "unclear_stakes", "too_short")


class JudgeVerdict(BaseModel):
    hook_score: float = Field(ge=1.0, le=5.0)
    cliffhanger_score: float = Field(ge=1.0, le=5.0)
    standalone_score: float = Field(ge=1.0, le=5.0)
    trope_pull_score: float = Field(ge=1.0, le=5.0)
    open_question: str = ""  # empty = no cliffhanger
    answered_in_clip: bool  # hard check against cutting past the resolution
    who_wants_what: str = ""  # cold comprehension check
    why_it_works: str = ""
    main_failure: Literal["none", "hook_late", "hook_weak", "answered_in_clip", "no_open_question",
                          "needs_context", "unclear_stakes", "too_short"]
    missing_entity: Optional[str] = None  # only when main_failure == needs_context
    resolution_line: Optional[int] = None  # [n] of the row that answers the clip's question
    best_hook_line: Optional[int] = None  # [n] of the most scroll-stopping row, if not in the first 3 s

    @field_validator("hook_score", "cliffhanger_score", "standalone_score", "trope_pull_score", mode="before")
    @classmethod
    def _scores(cls, v):
        return _clamp_score(v)

    @field_validator("main_failure", mode="before")
    @classmethod
    def _failure(cls, v):
        s = _snake(v, "none")
        return "none" if s in ("", "null", "n_a", "na", "no_failure", "nothing") else s

    @field_validator("resolution_line", "best_hook_line", mode="before")
    @classmethod
    def _line(cls, v):
        if v in (None, "", "null"):
            return None
        m = re.search(r"\d+", str(v))
        return int(m.group()) if m else None


# --------------------------------------------------------------------------- duels
class DuelVerdict(BaseModel):
    winner: Literal["A", "B"]
    margin: Literal["slight", "clear", "decisive"] = "clear"
    reason: str = ""

    @field_validator("winner", mode="before")
    @classmethod
    def _w(cls, v):
        s = str(v).strip().upper()
        return "B" if s.startswith("B") or s.endswith(" B") else "A" if "A" in s[:9] else s

    @field_validator("margin", mode="before")
    @classmethod
    def _m(cls, v):
        s = _snake(v, "clear")
        return s if s in ("slight", "clear", "decisive") else "clear"
