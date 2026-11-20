"""Stage 5B, pass 2: pairwise duels on the shortlist. 1-5 rubric scores bunch up; "which would you post?"
is more reliable. 3 Swiss rounds x 5 pairs x both orders = 30 calls; both orders cancel position bias."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .llm import LLM, LLMOutputError, ask
from .parse import Show
from .judge import render_clip
from .schemas import DuelVerdict


def swiss_pairs(ids: list[str], wins: dict[str, int], seed_order: list[str], played: set[frozenset],
                rnd: int) -> list[tuple[str, str]]:
    if rnd == 0:  # fold pairing: 1 v 6, 2 v 7, ... on the seed order
        h = len(seed_order) // 2
        return [(seed_order[i], seed_order[i + h]) for i in range(h)]
    pool = sorted(ids, key=lambda i: (-wins[i], seed_order.index(i)))
    pairs: list[tuple[str, str]] = []
    while len(pool) >= 2:
        a = pool.pop(0)
        j = next((k for k, b in enumerate(pool) if frozenset((a, b)) not in played), 0)
        pairs.append((a, pool.pop(j)))
    return pairs


def _duel_once(llm: LLM, show: Show, a: dict, b: dict, cfg: dict, cast) -> DuelVerdict:
    hook_s = cfg["promo"]["hook_max_s"]
    # separate speaker maps per clip, prefixed so the model never merges A_SPEAKER_1 with B_SPEAKER_1
    ta = render_clip(show, a, cast, prefix="A_", hook_s=hook_s)[0]
    tb = render_clip(show, b, cast, prefix="B_", hook_s=hook_s)[0]

    def offline() -> DuelVerdict:
        sa, sb = a["final_score"], b["final_score"]
        return DuelVerdict(winner="A" if sa >= sb else "B", margin="slight", reason="(offline: higher score)")

    return ask(llm, cfg, "duel", DuelVerdict, offline, user_vars={"a": ta, "b": tb})


def run_duels(llm: LLM, show: Show, shortlist: list[dict], cfg: dict, cast, workers: int = 4) -> list[dict]:
    if len(shortlist) < 2:
        return []
    by = {c["cand_id"]: c for c in shortlist}
    pv = cfg["prompts"]["duel"]
    seed = [c["cand_id"] for c in shortlist]
    wins = {i: 0 for i in seed}
    played: set[frozenset] = set()
    games: list[dict] = []
    for rnd in range(cfg["rank"]["duel_rounds"]):
        pairs = swiss_pairs(seed, wins, seed, played, rnd)
        jobs = [(x, y, "AB") for x, y in pairs] + [(y, x, "BA") for x, y in pairs]

        def play(job):
            first, second, order = job
            try:
                v = _duel_once(llm, show, by[first], by[second], cfg, cast)
            except LLMOutputError as e:
                return {"round": rnd + 1, "a": first, "b": second, "error": str(e)[:200], "prompt_version": pv}
            w, l = (first, second) if v.winner == "A" else (second, first)
            return {"round": rnd + 1, "a": first, "b": second, "shown_first": first, "winner_id": w,
                    "loser_id": l, "margin": v.margin, "reason": v.reason, "prompt_version": pv}

        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(play, jobs))
        for r in results:
            if "winner_id" in r:
                wins[r["winner_id"]] += 1
                games.append(r)
        for x, y in pairs:
            played.add(frozenset((x, y)))
    return games
