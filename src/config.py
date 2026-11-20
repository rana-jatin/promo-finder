"""Config loading: config.yaml + CLI overrides (dotted keys)."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def deep_merge(base: dict, over: dict) -> dict:
    """Dicts merge key by key; anything else (lists included) is replaced by the overlay's value."""
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def load_config(path: str | Path | None = None, overrides: dict[str, Any] | None = None,
                overlay: str | Path | None = None) -> dict:
    cfg = yaml.safe_load(Path(path or DEFAULT_PATH).read_text(encoding="utf-8"))
    if overlay:  # e.g. config.cloud.yaml: only the keys the hosted demo changes
        cfg = deep_merge(cfg, yaml.safe_load(Path(overlay).read_text(encoding="utf-8")) or {})
    for dotted, value in (overrides or {}).items():
        if value is None:
            continue
        node = cfg
        *parents, leaf = dotted.split(".")
        for p in parents:
            node = node.setdefault(p, {})
        node[leaf] = value
    return resolve_prompts(cfg)


STAGES = ("bible", "mine", "link", "judge", "duel")


def resolve_prompts(cfg: dict) -> dict:
    """cfg['prompts'][stage] = the prompt file name: an explicit per-stage override, else <stage>_<prompt_version>."""
    explicit = dict(cfg.get("prompts") or {})
    version = cfg.get("prompt_version")
    for stage in STAGES:
        if not explicit.get(stage):
            if not version:
                raise KeyError(f"config sets neither prompt_version nor prompts.{stage}")
            explicit[stage] = f"{stage}_{version}"
    cfg["prompts"] = explicit
    return cfg


def with_overrides(cfg: dict, overrides: dict[str, Any]) -> dict:
    out = copy.deepcopy(cfg)
    for dotted, value in overrides.items():
        node = out
        *parents, leaf = dotted.split(".")
        for p in parents:
            node = node.setdefault(p, {})
        node[leaf] = value
    return out
