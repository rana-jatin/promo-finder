"""Pydantic schemas for every LLM output. Before-validators absorb cosmetic slips (casing, 'None',
a score of 0 or 6) so they do not cost a corrective retry; anything structural still fails.

Field order follows the prompts (evidence before scores, reason before winner): the JSON Schema shown
to the model lists properties in this order. Closed vocabularies (tropes, hook types) are per show and
per config, so the mining and linking schemas are built at runtime by `mine_schema` / `link_schema`."""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal, Optional

from pydantic import AliasChoices, BaseModel, Field, create_model, field_validator, model_validator


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


def _opt_id(v: object) -> Optional[str]:
    s = str(v or "").strip()
    return None if s.lower() in ("", "null", "none", "n/a") else s


# --------------------------------------------------------------------------- bible
class Character(BaseModel):
    name: str
    aliases: list[str] = []
    role: str = ""
    relationship: str = ""
    visual_descriptor: str = ""


class Spoiler(BaseModel):
    episode: int
    reveal: str
    keywords: list[str] = []

    @field_validator("keywords", mode="before")
    @classmethod
    def _kw(cls, v):
        return [" ".join(str(k).lower().split()) for k in (v or []) if str(k).strip()]


class Arc(BaseModel):
    id: str = ""
    name: str
    episodes: list[int] = []
    summary: str = ""

    @model_validator(mode="after")
    def _arc_id(self):
        self.id = _snake(self.id or self.name, "arc")
        return self


class Bible(BaseModel):
    premise: str
    logline_for_strangers: str = ""
    characters: list[Character] = []
    tropes: list[str] = []
    spoilers: list[Spoiler] = []
    arcs: list[Arc] = []

    @field_validator("tropes", mode="before")
    @classmethod
    def _tropes(cls, v):
        return [_snake(t) for t in (v or [])]


# --------------------------------------------------------------------------- mining
_QUESTION = AliasChoices("intended_question", "open_question")  # v1 prompts and cached records say open_question


class Moment(BaseModel):
    template: Literal["continuous", "cold_open", "bridge"] = "continuous"
    start_id: str
    hook_id: str = ""  # continuous: defaults to start_id
    cliff_id: str = ""  # empty for a bridge
    peak_id: Optional[str] = None  # cold_open only
    next_start_id: Optional[str] = None  # bridge only
    next_cliff_id: Optional[str] = None  # bridge only
    hook_type: str = "other"
    trope: str = "other"
    intended_question: str = Field("", validation_alias=_QUESTION)
    why: str = ""
    confidence: float = 0.5

    @field_validator("template", mode="before")
    @classmethod
    def _tpl(cls, v):
        v = _snake(v, "continuous")
        return v if v in ("continuous", "cold_open", "bridge") else "continuous"

    @field_validator("hook_id", "cliff_id", mode="before")
    @classmethod
    def _ids(cls, v):
        return _opt_id(v) or ""

    @field_validator("peak_id", "next_start_id", "next_cliff_id", mode="before")
    @classmethod
    def _opt_ids(cls, v):
        return _opt_id(v)

    @field_validator("hook_type", "trope", mode="before")
    @classmethod
    def _labels(cls, v):
        return _snake(v)

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v):
        return _clamp01(v)


class Bridge(BaseModel):
    """v1 prompts returned the bridge as its own field; v3 returns it as a moment with template 'bridge'."""
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
    arc: str = "new"

    @field_validator("arc", mode="before")
    @classmethod
    def _arc(cls, v):
        return _snake(v, "new")


class MineResult(BaseModel):
    no_promo: bool = False
    no_promo_reason: str = ""
    moments: list[Moment] = []
    bridge: Optional[Bridge] = None  # legacy (v1); validate_mine turns it into a bridge moment
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
    hook_type: str = "other"
    trope: str = "other"
    arc: str = "new"
    intended_question: str = Field("", validation_alias=_QUESTION)
    why: str = ""
    confidence: float = 0.5

    @field_validator("hook_type", "trope", mode="before")
    @classmethod
    def _labels(cls, v):
        return _snake(v)

    @field_validator("arc", mode="before")
    @classmethod
    def _arc(cls, v):
        return _snake(v, "new")

    @field_validator("confidence", mode="before")
    @classmethod
    def norm_conf(cls, v):
        return _clamp01(v)


class LinkResult(BaseModel):
    stitches: list[Stitch] = []


# --------------------------------------------------------------------------- closed vocabularies
def _vocab_validator(field: str, allowed: tuple[str, ...]):
    def coerce(cls, v):  # an off-list label becomes "other" instead of costing a corrective retry
        s = _snake(v)
        return s if s in allowed else "other"
    return field_validator(field, mode="before")(coerce)


def _labels_plus_other(values: tuple[str, ...]) -> tuple[str, ...]:
    """Given order kept (the bible lists tropes strongest first), 'other' last, duplicates dropped."""
    return tuple(dict.fromkeys([*(_snake(v) for v in values if v), "other"]))


@lru_cache(maxsize=32)
def _vocab_models(tropes: tuple[str, ...], hook_types: tuple[str, ...]):
    tr, ht = _labels_plus_other(tropes), _labels_plus_other(hook_types)
    fields = dict(hook_type=(Literal[ht], "other"), trope=(Literal[tr], "other"))  # type: ignore[valid-type]
    validators = {"_v_hook_type": _vocab_validator("hook_type", ht), "_v_trope": _vocab_validator("trope", tr)}
    moment = create_model("Moment", __base__=Moment, __validators__=validators, **fields)
    mine = create_model("MineResult", __base__=MineResult, moments=(list[moment], []))
    stitch = create_model("Stitch", __base__=Stitch, __validators__=validators, **fields)
    link = create_model("LinkResult", __base__=LinkResult, stitches=(list[stitch], []))
    return mine, link


def mine_schema(tropes: list[str], hook_types: list[str]) -> type[MineResult]:
    """MineResult whose hook_type / trope are JSON Schema enums of exactly these labels (plus 'other')."""
    return _vocab_models(tuple(tropes), tuple(hook_types))[0]


def link_schema(tropes: list[str], hook_types: list[str]) -> type[LinkResult]:
    return _vocab_models(tuple(tropes), tuple(hook_types))[1]


# --------------------------------------------------------------------------- cold judge
# The judge's labels, in the prompt's priority order. repair.REPAIRS is keyed on exactly these (minus "none").
FAILURES = ("answered_in_clip", "ends_on_replay", "hook_late", "hook_weak", "no_open_question", "needs_context",
            "unclear_stakes", "none")


class JudgeVerdict(BaseModel):
    # evidence first
    who_wants_what: str = ""  # cold comprehension check
    open_question: str = ""  # empty = no cliffhanger
    answered_in_clip: bool  # hard check against cutting past the resolution
    resolution_line: Optional[int] = None  # [n] of the first row that answers the clip's question
    best_hook_line: Optional[int] = None  # [n] of the most scroll-stopping row, if after the 3.0 s marker
    # then the scores
    hook_score: float = Field(ge=1.0, le=5.0)
    cliffhanger_score: float = Field(ge=1.0, le=5.0)
    standalone_score: float = Field(ge=1.0, le=5.0)
    trope_pull_score: float = Field(ge=1.0, le=5.0)
    # then the diagnosis
    main_failure: Literal["answered_in_clip", "ends_on_replay", "hook_late", "hook_weak", "no_open_question",
                          "needs_context", "unclear_stakes", "none"]
    missing_entity: Optional[str] = None  # only when main_failure == needs_context
    why_it_works: str = ""

    @field_validator("hook_score", "cliffhanger_score", "standalone_score", "trope_pull_score", mode="before")
    @classmethod
    def _scores(cls, v):
        return _clamp_score(v)

    @field_validator("main_failure", mode="before")
    @classmethod
    def _failure(cls, v):
        s = re.sub(r"^\d+_", "", _snake(v, "none"))  # "1. answered_in_clip" -> answered_in_clip
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
    reason: str = ""
    winner: Literal["A", "B"]
    margin: Literal["slight", "clear", "decisive"] = "clear"

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
