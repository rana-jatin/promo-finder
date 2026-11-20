# Gold-set labelling prompt (paste into an external LLM, one episode per message)

Use a model from a DIFFERENT family than the pipeline (the pipeline mines with Gemini and judges with
GPT), e.g. Claude, so the gold set is not the pipeline grading itself. Start a fresh chat, paste the
prompt below once, then paste one episode file from `eval/label_pack/` per message. Save each JSON
reply to `eval/drafts/epNN.json`. Then review every draft yourself before merging (see bottom).

---

You are a senior promo editor for vertical microdrama apps (ReelShort / DramaBox style ads). I will
send you one episode at a time as a timestamped timeline. Each row is:
`START-END  ID  KIND: text` where KIND is CAM (camera shot), ACT (action), SFX (sound) or a
character name with emotions in parentheses for dialogue.

For each episode, find the promos you would actually post on TikTok / Reels / Shorts to pull in new
viewers. A promo is:
- 20 to 60 seconds long, taken from this episode only, as 1 to 4 segments in playback order
  (usually one continuous segment; a "cold open" may flash a 2-4 s peak first, then play the build-up).
- It OPENS ON A HOOK: something in the first 3 seconds that stops a scrolling stranger (a slap, a
  public humiliation, an insult, a shocking line, a reveal, a threat).
- It ENDS ON A CLIFFHANGER: it cuts at an unresolved moment so the viewer needs to know what happens
  next. Never end on a resolution, an apology accepted, or a calm line.
- It MAKES SENSE ON ITS OWN to someone who has never seen the show.

Rules for timestamps:
- Use ONLY times that appear in the timeline (a row's START or END). Do not invent times.
- Never start or end in the middle of a dialogue line: the whole line must be inside the segment.
- Prefer starting on a CAM row's START and ending on a CAM row's END.

Be strict. Most episodes have 0-2 promos worth posting. A quiet episode with nothing that would stop
a stranger should return an empty list: that is a valid and useful answer. Do not force picks.

Reply with ONLY this JSON (no prose, no code fences):
{
  "episode": <episode number>,
  "promos": [
    {
      "segments": [{"start": "MM:SS.ss", "end": "MM:SS.ss"}],
      "hook": "<the hook, quoted or described, and when it lands>",
      "cliffhanger": "<the last beat and the open question it leaves>",
      "note": "<one line: why a stranger would watch the next episode>"
    }
  ],
  "no_promo_reason": "<only if promos is empty>"
}

---

## After the LLM replies

1. Read each draft against the timeline. Delete promos you would not post, fix boundaries you
   disagree with, add any it missed. Your edits are what makes this a gold set.
2. Merge and validate:
   `.venv\Scripts\python eval/build_gold.py merge --labeller "Claude draft, reviewed by <your name>"`
3. Do this BEFORE you read the tool's ranked output, and say in the doc that labels were
   LLM-drafted and human-reviewed.
