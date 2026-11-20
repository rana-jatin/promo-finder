# Plan: Promo Finder

This is the plan I worked from, cleaned up after the build so it matches what actually happened.
Where the plan changed along the way, I've said what changed and why. The README has the full
write-up and results. This file is the thinking behind it.

---

## What I'm building

A tool that reads a folder of microdrama screenplays and hands back a ranked list of promos:
20 to 60 second clips that open on a hook, end on a cliffhanger, and make sense to someone who has
never seen the show. For each promo it gives the episode and exact start and end of every segment,
the hook, the cliffhanger, one line on why it works, and a reason for posting it early or late.

It doesn't edit video or add music. It only picks the moments and the cut points.

## What the brief is really asking for

Read literally, it asks for "find the promos and rank them". Read again, a few more requirements
show up:

- **They'll run it live on a new show in the interview.** So it has to be one command: folder in,
  ranked list out. No notebook cells, no manual steps.
- **They may plug in their own, stronger models.** So the model can't be hard-coded anywhere. One
  adapter, the model name in config, and a flag to override it.
- **Free tiers should be enough.** So it has to survive rate limits, daily caps and the odd
  refusal without falling over, and it has to resume when it stops.
- **"Where does it pick badly?"** They want honest failures, not a demo. That means I need
  something to measure against, which means hand-labelled promos, made before I look at the output.
- **"One place AI got it wrong."** I'd need to keep notes while building, not reconstruct them at
  the end.

## What I learned from the data before writing code

I read one episode closely (Ep 8) and then audited all 50 with a script before trusting any of it.

- The format is very regular: a header, then scenes, then timed blocks of four kinds (camera,
  action, sound, dialogue). Every block has a start and end time. That means code can own every
  timestamp, and the LLM never has to produce one.
- Dialogue and emotion tags and camera directions are free signal. A cheap keyword pass can
  say which episodes are "hot" before any LLM call.
- **Some episodes don't have a good promo in them.** Ep 8 is quiet and ends on a resolution. The tool
  has to be allowed to say "nothing here", or it will force bad picks.
- **The best tension is often short.** In Ep 8 the tense stretch is about 10 seconds, well under
  the 20-second floor. That makes joining moments (across a cut, or across episodes) a core feature,
  not an extra.
- **My first assumption about the timeline was wrong.** In Ep 8 the camera shots cover the
  timeline end to end, so I assumed that held for every episode. The audit across all 50 found 112
  gaps between shots, 5 overlaps and 9 scenes where camera coverage stops early. About 11% of lines
  run across a shot cut. So the parser now repairs the timeline into a gap-free grid and logs every
  fix, and "never cut inside a line" became a hard rule.

## The core idea

**The LLM judges drama. Code owns time and the rules. An editor makes the final call.**

LLMs are good at reading tension and subtext and bad at arithmetic on timestamps. So the model
only ever points at shot and line IDs (like `E08.S12` or `E08.L07`), and code turns those into
times, snaps them to legal cut points and checks every rule in the brief. If a promo breaks a
rule, it never reaches the list, whatever the model thought of it.

The second idea is the **cold judge**. A promo has to work for a stranger scrolling past, so the
judge sees only the promo itself: no story summary, no character list, no other episodes. In
visual descriptions, character names are swapped for what you'd see ("the scarred soldier"),
because a stranger sees faces, not names.

## The plan, step by step

| Step | What | Status |
| --- | --- | --- |
| 0 | Audit the screenplay format across all 50 files | Done. Found the timeline gaps above, which changed the parser |
| 1 | Parser: stable IDs for every shot and line, plus a gap-free cut grid | Done |
| 2 | Cheap features: keyword, emotion and camera tags, an intensity score per shot, a "heat" score per episode | Done |
| 3 | LLM adapter: one function, model from config, fallbacks, disk cache, retry on bad JSON | Done, and it grew (see below) |
| 4 | Show bible: one LLM call over the 50 episode headers (premise, characters, tropes, late spoilers) | Done |
| 5 | Mining: one call per episode for hooks, cliffhangers, peaks, setups and payoffs, or "no promo" | Done |
| 6 | Linking: one call over the whole season to join setups and payoffs into multi-episode promos | Done |
| 7 | Assembly in code: 4 templates, boundary rules, hard validators | Done |
| 8 | Cold judge plus a repair loop: judge, fix the cut in code, re-judge, keep the best | Done |
| 9 | Ranking: weighted score, dedupe, caps, pairwise duels, posting order | Done |
| 10 | Editor review: approve, reject or nudge, then backfill | Done |
| 11 | Gold set and evaluation script, labelled before looking at output | Done (10 promos, see limits) |
| 12 | Full 50-episode run, ablations, raw outputs folder | Done (three versions) |
| 13 | Hosted demo | Built and tested locally. Deploy pending |
| 14 | Two-page solution doc | Pending |

### The four promo shapes (step 7)

1. **Continuous:** one stretch, from hook to cliffhanger.
2. **Cold open:** a 2 to 4 second flash of the peak, then the build-up running through it to a new
   unresolved beat. It must not end on the moment it already showed.
3. **Bridge:** the end of one episode plus the start of the next, cut before the answer.
4. **Arc stitch:** a setup from one episode joined to its payoff in a later one.

### Boundary rules (step 7)

These are all in code, and every number lives in `config.yaml`.

- First try to cut on shot boundaries, never inside a line of dialogue.
- If that puts the hook later than 3 seconds into the clip, or the end more than 1.5 seconds after
  the cliffhanger, trim mid-shot instead, with small pads around the line.
- Never leave a sliver of a shot (under 0.5 s) at either edge.
- Every clip: 20 to 60 s, 1 to 4 segments of at least 2 s each.
- Last check: turn the final timestamps back into text and confirm it's exactly what the judge read.

Ep 8 became the unit test for this. A plain shot-boundary cut puts the hook at 3.8 s, which fails.
The trim moves the start to 00:29.90 and the hook lands at 2.0 s, giving 00:29.90 to 00:51.30.

### Ranking (step 9)

Score = 0.30 Hook + 0.25 Cliffhanger + 0.20 Standalone + 0.15 Trope pull + 0.10 Pace, minus
penalties for extra segments, being outside 30 to 45 s, and spoilers.

1-to-5 scores bunch up, so the top 10 also go through pairwise duels ("which would you post?"),
each pair asked in both orders to cancel position bias. The win record gives the final order.

Last, the posting order. Someone who clicks a promo starts at Episode 1. A hook from Ep 3 pays
off in minutes. One from Ep 40 makes them wait a long time and may spoil a twist. So promos inside
the free episodes get a boost, and spoilers get held back.

## What I'd cut if time ran short

In this order: duels, then arc stitches, then the web UI.

Never cut: the audit, the boundary rules, the cold judge, the gold-set check and the raw outputs.
Without those, I'd have no way to say whether any of it works.

## What changed along the way

- **The first draft let the "cold" judge see the story bible** so it could score tropes. A review
  caught it. That's not cold at all, and it would have inflated the standalone score. The judge is
  now fully cold, one promo per call, and the bible is used only by code to flag spoilers.
- **The first draft made 421 LLM calls per show.** Too slow and too expensive for free tiers. Moving
  the stability checks into the evaluation script and judging only the top 40 brought it to about 130.
- **Sliding 5-episode windows can't find cross-season arcs**, because a setup in Ep 8 and its
  payoff in Ep 22 never share a window. I replaced them with tagging in mining plus one
  season-wide linking call.
- **I moved orchestration to LangGraph** once the flow had real branches: a fan-out per episode,
  a judge-repair loop, a "not enough passed, judge another batch" check, and an editor pause that
  has to survive a restart. Every stage inside is still plain Python.
- **Models moved around a lot.** Free Gemini Flash ran out of daily quota partway through the
  first run. One provider's content filter blocked a few judge calls on scripted violence. One
  judge model passed almost everything. The next rejected most good clips. So the adapter now has
  fallback chains per stage group, moves to the next model on a daily cap or a refusal, and records
  which model produced every output.
- **I rewrote the prompts (v3)** so they don't depend on this show, since the interview uses a new
  one. Tropes and hook types now come from fixed lists that are also enforced in the output schema.

## Risks I knew about going in

- **The judge is one model's opinion.** I can measure it against hand labels, but with 10 labels
  that's a rough check, not a calibration.
- **Text isn't footage.** The script can't show acting, music or the real pacing of the video.
- **Names leak context.** A name spoken in dialogue still tells the "cold" judge who someone is.
- **Recall is decided by mining.** If mining misses a moment, nothing later can find it.

All four turned out to matter. The README covers them under "Where it picks badly".

## What's left

- Deploy the web demo on Render and add the link to the README.
- Write the two-page solution doc from the README and the raw outputs.
- Push the repo (without the screenplay pack or anything that quotes it).
- Next real improvement: dedupe by story, not just by timestamps, and fit the score weights to
  editor decisions instead of my guesses.
