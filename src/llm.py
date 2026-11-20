"""One LLM adapter for every stage: LiteLLM + prompt-hash disk cache + rate limiter + JSON validation.

    llm.complete_json(stage=..., system=..., user=..., schema=PydanticModel, offline_fn=...)

* Stages are routed to ordered model lists in config.yaml (llm.routes): long show-context stages on
  Gemini, short cold-judge stages on Azure OpenAI with Groq as fallback. Swapping models is config.
* Every response is cached by sha256(model, temperature, prompt, schema). Reruns and resumes replay
  completed calls from disk at 0 API cost (the second durability layer under the SQLite checkpointer).
* Output is schema-validated; bad JSON gets one corrective retry, then LLMOutputError (callers log + drop).
* Per-minute 429s wait (honouring the provider's retry delay); a daily-quota 429 moves the route to its
  next model for good, else raises QuotaExhausted so the run can be resumed later with --resume.
  A content-filter refusal (Azure's filter on a slap or a threat, a Gemini safety block) skips to the
  next model for that one call only.
* model "offline" never touches the network: each stage supplies a heuristic offline_fn instead.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


class LLMOutputError(RuntimeError):
    pass


class QuotaExhausted(RuntimeError):
    pass


# --------------------------------------------------------------------------- prompts
def load_prompt(name: str) -> tuple[str, str]:
    """prompts/<name>.md holds '# system' and '# user' sections; returns (system, user_template)."""
    text = (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")
    parts = re.split(r"^# (system|user)\s*$", text, flags=re.M)
    sections = {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    return sections["system"], sections["user"]


def fill(template: str, **values: Any) -> str:
    out = template
    for k, v in values.items():
        out = out.replace("{{" + k + "}}", str(v))
    missing = re.findall(r"\{\{(\w+)\}\}", out)
    if missing:
        raise KeyError(f"unfilled prompt variables: {missing}")
    return out


# --------------------------------------------------------------------------- helpers
def extract_json(text: str) -> Any:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    a, b = t.find("{"), t.rfind("}")
    if a >= 0 and b > a:
        return json.loads(t[a:b + 1])
    raise ValueError("no JSON object in response")


def _schema_hint(schema: type[BaseModel]) -> str:
    js = json.dumps(schema.model_json_schema(), separators=(",", ":"))
    return ("\n\nReply with ONE JSON object and nothing else. It must validate against this JSON Schema:\n"
            + js)


_DAILY = re.compile(r"per ?day|perday|per_day|daily|RPD", re.I)
_RETRY_DELAY = re.compile(r"retry[_ ]?delay\W+(\d+(?:\.\d+)?)s|retry in (\d+(?:\.\d+)?)\s*s|"
                          r"try again in (\d+(?:\.\d+)?)s|retry after (\d+(?:\.\d+)?) sec", re.I)


def _retry_delay(msg: str) -> float | None:
    m = _RETRY_DELAY.search(msg)
    if not m:
        return None
    return float(next(g for g in m.groups() if g))


class RateLimiter:
    """Spaces requests 60/rpm seconds apart (thread-safe), so a burst of fan-out workers queues politely."""

    def __init__(self, rpm: float | None):
        self.interval = 60.0 / rpm if rpm else 0.0
        self.next_t = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        if not self.interval:
            return
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next_t)
            self.next_t = slot + self.interval
        if slot > now:
            time.sleep(slot - now)


_FILTERED = re.compile(r"content[_ ]?filter|ResponsibleAIPolicyViolation|content management policy|"
                       r"content_policy|safety|blocked", re.I)


class ContentFiltered(LLMOutputError):
    """Every model on the stage's route refused or blocked the prompt (a per-call problem, not a quota)."""


def _expand(name: str) -> str:
    return os.path.expandvars(name)


class Route:
    """An ordered model list for a group of stages. idx moves forward permanently on a daily quota;
    a content-filter refusal only skips ahead for that one call."""

    def __init__(self, name: str, stages: list[str], models: list, default_rpm: float | None):
        self.name, self.stages, self.idx = name, stages, 0
        self.models: list[dict] = []
        for m in models:
            m = {"name": m} if isinstance(m, str) else dict(m)
            m["name"] = _expand(m["name"])
            if m.get("api_base"):
                m["api_base"] = _expand(m["api_base"])
            m.setdefault("rpm", default_rpm)
            self.models.append(m)

    @property
    def active(self) -> dict:
        return self.models[self.idx]

    def unresolved(self) -> list[str]:
        return [m["name"] for m in self.models if "$" in m["name"] or "$" in (m.get("api_base") or "")]


# --------------------------------------------------------------------------- adapter
class LLM:
    """Stages are routed to model lists (config llm.routes): e.g. long show-context stages on Gemini,
    short cold-judge stages on Azure OpenAI with Groq as fallback. llm.model (or --model) forces a
    single model for every stage, which is how 'offline' and quick swaps work."""

    def __init__(self, cfg: dict, call_log: str | Path | None = None, api_keys: dict[str, str] | None = None):
        self.c = dict(cfg["llm"])
        # per-run keys by provider prefix ("gemini", "groq", "openai", ...): the web app passes a visitor's own key
        # here. It is never written to os.environ (shared by every session in the process) and is scrubbed from logs.
        self.api_keys = {k: v for k, v in (api_keys or {}).items() if v}
        forced = self.c.get("model")
        self.offline = forced == "offline"
        if forced:
            self.routes = [Route("all", ["*"], [forced, *self.c.get("fallbacks", [])], self.c.get("rpm"))]
        else:
            self.routes = [Route(name, r["stages"], r["models"], r.get("rpm", self.c.get("rpm")))
                           for name, r in (self.c.get("routes") or {}).items()]
            if not self.routes:
                raise ValueError("config llm: set either llm.model or llm.routes")
        self.cache_dir = Path(self.c.get("cache_dir", ".cache/llm"))
        self.limiters: dict[str, RateLimiter] = {}
        self.lock = threading.Lock()
        self.stats: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.model_calls: dict[str, int] = defaultdict(int)
        self.call_log = Path(call_log) if call_log else None
        price = self.c.get("price_per_mtok") or {}
        self.price_in, self.price_out = float(price.get("input", 0) or 0), float(price.get("output", 0) or 0)

    # ---- routing
    def route_for(self, stage: str) -> Route:
        for r in self.routes:
            if "*" in r.stages or stage in r.stages:
                return r
        for r in self.routes:  # judge_stability / judge_leak follow the judge route
            if any(stage.startswith(s) for s in r.stages):
                return r
        return self.routes[0]

    def model_for(self, stage: str) -> str:
        return "offline" if self.offline else self.route_for(stage).active["name"]

    @property
    def model(self) -> str:
        if self.offline:
            return "offline"
        return " | ".join(f"{r.name}: {r.active['name']}" for r in self.routes) if len(self.routes) > 1 \
            else self.routes[0].active["name"]

    def describe(self) -> list[str]:
        return [f"{r.name} ({', '.join(r.stages)}): " + " -> ".join(m["name"] for m in r.models) for r in self.routes]

    # ---- bookkeeping
    def _count(self, stage: str, **kv: float) -> None:
        with self.lock:
            for k, v in kv.items():
                self.stats[stage][k] += v

    def scrub(self, text: str) -> str:
        """Remove per-run keys from any text that leaves the adapter (logs, error messages)."""
        for key in self.api_keys.values():
            if len(key) >= 8:
                text = text.replace(key, key[:4] + "…redacted")
        return text

    def _log(self, row: dict) -> None:
        if not self.call_log:
            return
        line = self.scrub(json.dumps(row, ensure_ascii=False))
        with self.lock:
            self.call_log.parent.mkdir(parents=True, exist_ok=True)
            with self.call_log.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def snapshot(self) -> dict:
        with self.lock:
            per_stage = {s: {k: round(v, 4) for k, v in d.items()} for s, d in self.stats.items()}
        tot: dict[str, float] = defaultdict(float)
        for d in per_stage.values():
            for k, v in d.items():
                tot[k] += v
        with self.lock:
            per_model = dict(self.model_calls)
        return {"model": self.model, "routes": self.describe(), "calls_per_model": per_model,
                "per_stage": per_stage, "total": {k: round(v, 4) for k, v in tot.items()}}

    # ---- cache
    @staticmethod
    def _key(model: str, system: str, user: str, schema: type[BaseModel], temperature: float, salt: str) -> str:
        blob = json.dumps({"model": model, "t": temperature, "system": system, "user": user,
                           "schema": schema.__name__, "schema_js": schema.model_json_schema(), "salt": salt},
                          sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _cache_path(self, model: str, key: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", model)
        return self.cache_dir / safe / key[:2] / f"{key}.json"

    def _cache_get(self, model: str, key: str) -> dict | None:
        p = self._cache_path(model, key)
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
        return None

    def _cache_put(self, model: str, key: str, payload: dict) -> None:
        p = self._cache_path(model, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)

    # ---- public
    def complete_json(self, *, stage: str, system: str, user: str, schema: type[BaseModel],
                      offline_fn: Callable[[], Any] | None = None, max_tokens: int | None = None,
                      temperature: float | None = None, cache_salt: str = "") -> BaseModel:
        if self.offline:
            if offline_fn is None:
                raise RuntimeError(f"stage '{stage}' has no offline implementation")
            out = offline_fn()
            obj = schema.model_validate(out.model_dump() if isinstance(out, BaseModel) else out)
            self._count(stage, offline=1)
            return obj

        temp = self.c.get("temperature", 0.2) if temperature is None else temperature
        max_tokens = max_tokens or (self.c.get("max_tokens") or {}).get(stage, 4000)
        route = self.route_for(stage)
        names = [m["name"] for m in route.models]
        keys = {m: self._key(m, system, user, schema, temp, cache_salt) for m in names}
        # reuse cached answers only from models this route has REACHED in this run (active model first). A fresh
        # run starts on the primary, so old fallback answers (e.g. flash-lite after a daily cap) are not silently
        # passed off as the primary's; once the route falls back mid-run, the backup's cached answers replay free.
        reached = names[:route.idx + 1]
        for m in [route.active["name"], *[x for x in reached if x != route.active["name"]]]:
            cached = self._cache_get(m, keys[m])
            if cached is None:
                continue
            try:
                obj = schema.model_validate(cached["parsed"])
                self._count(stage, cache_hits=1)
                return obj
            except (ValidationError, KeyError):
                continue  # schema changed since the entry was written: call again

        messages = [{"role": "system", "content": system + _schema_hint(schema)},
                    {"role": "user", "content": user}]
        err: Exception | None = None
        for attempt in range(2):
            text, meta = self._call(stage, route, messages, max_tokens, temp)
            try:
                obj = schema.model_validate(extract_json(text))
            except (ValueError, ValidationError) as e:
                err = e
                self._log({"stage": stage, "model": meta["model"], "event": "invalid_json", "attempt": attempt,
                           "error": str(e)[:500]})
                messages = messages + [
                    {"role": "assistant", "content": text[:6000]},
                    {"role": "user", "content": "That reply did not validate. Error:\n" + str(e)[:1500]
                     + "\nReply again with ONLY the corrected JSON object."}]
                continue
            used = meta["requested_model"]
            self._cache_put(used, keys.get(used) or self._key(used, system, user, schema, temp, cache_salt),
                            {"parsed": obj.model_dump(), "raw": text, "model": meta["model"], "stage": stage,
                             "usage": meta["usage"], "created": time.strftime("%Y-%m-%dT%H:%M:%S")})
            return obj
        self._count(stage, failures=1)
        raise LLMOutputError(f"{stage}: invalid JSON after one corrective retry: {err}")

    def _call(self, stage: str, route: Route, messages: list[dict], max_tokens: int,
              temperature: float) -> tuple[str, dict]:
        import litellm

        litellm.suppress_debug_info = True
        litellm.drop_params = True
        exc = litellm.exceptions
        transient = tuple(getattr(exc, n) for n in ("Timeout", "APIConnectionError", "ServiceUnavailableError",
                                                     "InternalServerError") if hasattr(exc, n))
        attempts = int(self.c.get("max_retries", 5)) + 1
        skip = 0  # models skipped for THIS call only (content filter), on top of route.idx
        attempt = 0
        while attempt < attempts:
            pos = route.idx + skip
            if pos >= len(route.models):
                raise ContentFiltered(f"{stage}: every model on route '{route.name}' blocked the prompt or answer, "
                                      f"or returned nothing at the max_tokens cap")
            entry = route.models[pos]
            model = entry["name"]
            with self.lock:
                limiter = self.limiters.setdefault(model, RateLimiter(entry.get("rpm")))
            limiter.wait()
            kwargs: dict[str, Any] = dict(model=model, messages=messages, max_tokens=max_tokens,
                                          timeout=entry.get("timeout_s", self.c.get("timeout_s", 180)),
                                          num_retries=0)
            if not entry.get("no_temperature"):
                kwargs["temperature"] = temperature
            if entry.get("api_base"):
                kwargs["api_base"] = entry["api_base"]
            if entry.get("api_key_env"):  # e.g. an Azure key used through the OpenAI-compatible v1 endpoint
                kwargs["api_key"] = os.environ.get(entry["api_key_env"], "")
            if self.api_keys.get(model.split("/", 1)[0]):
                kwargs["api_key"] = self.api_keys[model.split("/", 1)[0]]
            if entry.get("json_mode", self.c.get("json_mode", True)):
                kwargs["response_format"] = {"type": "json_object"}
            kwargs.update(self.c.get("extra") or {})
            kwargs.update(entry.get("extra") or {})
            t0 = time.time()
            try:
                resp = litellm.completion(**kwargs)
            except exc.RateLimitError as e:
                msg = str(e)
                self._log({"stage": stage, "model": model, "event": "rate_limited", "error": msg[:400]})
                if _DAILY.search(msg):  # a per-day cap will not clear by waiting: move the route on for good
                    if self._next_model(route, pos):
                        skip = 0
                        continue
                    raise QuotaExhausted(f"daily quota hit on {model} (route '{route.name}'). Resume later "
                                         f"with --resume (cached calls replay free).") from e
                if attempt + 1 >= attempts and pos + 1 < len(route.models):
                    # per-minute limit still hit after every retry (e.g. Groq free tier: 8k tokens/min):
                    # answer THIS call with the next model instead of failing the run
                    self._log({"stage": stage, "model": model, "event": "fallback_after_rate_limit"})
                    skip += 1
                    attempt = 0
                    continue
                time.sleep((_retry_delay(msg) or min(60.0, 5.0 * 2 ** attempt)) + random.uniform(0, 1.5))
                attempt += 1
                continue
            except transient as e:  # type: ignore[misc]
                self._log({"stage": stage, "model": model, "event": "transient_error", "error": str(e)[:400]})
                time.sleep(min(60.0, 2.0 * 2 ** attempt) + random.uniform(0, 1))
                attempt += 1
                continue
            except exc.BadRequestError as e:
                msg = str(e)
                if _FILTERED.search(msg) and not re.search(r"response_format", msg, re.I):
                    self._log({"stage": stage, "model": model, "event": "content_filtered", "error": msg[:300]})
                    skip += 1  # e.g. Azure's content filter on a slap or a threat: try the fallback, this call only
                    continue
                if re.search(r"temperature", msg, re.I) and not entry.get("no_temperature"):
                    entry["no_temperature"] = True  # reasoning deployments accept only the default
                    continue
                if entry.get("json_mode", True) and re.search(r"response_format|json|mime", msg, re.I):
                    entry["json_mode"] = False  # provider rejects JSON mode: rely on the prompt + validation
                    continue
                raise
            choice = resp.choices[0]
            text = (choice.message.content or "").strip()
            finish = str(getattr(choice, "finish_reason", "") or "")
            usage = getattr(resp, "usage", None)
            pt = int(getattr(usage, "prompt_tokens", 0) or 0)
            ct = int(getattr(usage, "completion_tokens", 0) or 0)
            try:
                cost = float(litellm.completion_cost(completion_response=resp) or 0.0)
            except Exception:
                cost = 0.0
            if not cost and (self.price_in or self.price_out):
                cost = pt / 1e6 * self.price_in + ct / 1e6 * self.price_out
            dt = time.time() - t0
            self._count(stage, calls=1, prompt_tokens=pt, completion_tokens=ct, cost_usd=cost, seconds=dt)
            with self.lock:
                self.model_calls[model] += 1
            if _FILTERED.search(finish):
                # output-side filter (e.g. Azure on a slap or a threat in the ANSWER): the text is empty or cut
                # off mid-JSON. Retrying the same model gets the same block, so use the fallback for this call.
                self._log({"stage": stage, "model": model, "event": "content_filtered", "finish": finish,
                           "partial_chars": len(text)})
                skip += 1
                continue
            if not text and finish == "length":
                # reasoning models (gpt-5-nano, gemini 3) can spend the whole budget thinking and return nothing.
                # Re-asking with the same budget fails the same way: double it once or twice, then fall back.
                cap = int(self.c.get("max_tokens_cap", 32000))
                self._log({"stage": stage, "model": model, "event": "empty_out_of_tokens",
                           "max_tokens": max_tokens, "completion_tokens": ct})
                if max_tokens < cap:
                    max_tokens = min(cap, max_tokens * 2)
                else:
                    skip += 1
                continue
            self._log({"stage": stage, "model": getattr(resp, "model", model) or model, "event": "ok",
                       "finish": finish, "prompt_tokens": pt, "completion_tokens": ct, "seconds": round(dt, 2)})
            return text or "{}", {"model": getattr(resp, "model", None) or model, "requested_model": model,
                                  "usage": {"prompt_tokens": pt, "completion_tokens": ct}}
        raise RuntimeError(f"{stage}: gave up after {attempts} attempts on route '{route.name}'")

    def _next_model(self, route: Route, failed_pos: int) -> bool:
        with self.lock:
            if route.idx <= failed_pos and failed_pos + 1 < len(route.models):
                route.idx = failed_pos + 1
                return True
            return route.idx > failed_pos  # another thread already moved the route on
