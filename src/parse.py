"""Stage 0: parse timestamped screenplays into episodes with stable IDs and a cut grid.

IDs are what the LLM cites; code converts them to times:
    E08.S12  shot = one cell of the cut grid (synthetic filler cells cover undescribed time)
    E08.L07  dialogue line
    E08.A03  action
    E08.X01  sound effect
Times are seconds on the episode clock, which runs continuously across scenes.

Format rules come from audit_pack.py over all 50 files, not from one episode:
  * 4 block labels: CAMERA, ACTION, SOUND FX, and dialogue as NAME (modifiers) (Emotion, ...)
  * dialogue modifiers stack before emotions: (O.S.), (Off-screen), (V.O.), (O.S. / Continues)
  * scene keys can be upper case ("SCENE HEADING:" in Ep 6)
  * camera shots do NOT always tile the timeline: gaps (some with dialogue inside),
    overlaps, and scenes whose last shot ends early. build_grid() repairs these and logs each fix.
"""
from __future__ import annotations

import argparse
import bisect
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

EPS = 1e-6
MIN_CELL = 0.5  # undescribed gaps shorter than this are absorbed by the previous shot

# --------------------------------------------------------------------------- timestamps
_TS = re.compile(r"^\s*(?:(\d+):)?(\d{1,3}):(\d{1,2}(?:\.\d+)?)\s*$")


def parse_ts(s: str) -> float:
    m = _TS.match(s)
    if not m:
        raise ValueError(f"bad timestamp: {s!r}")
    h, mi, se = m.groups()
    return round(int(h or 0) * 3600 + int(mi) * 60 + float(se), 2)


def fmt_ts(t: float) -> str:
    """Seconds -> MM:SS.ss (the format the screenplays use)."""
    cs = int(round(max(t, 0.0) * 100))
    m, rem = divmod(cs, 6000)
    return f"{m:02d}:{rem // 100:02d}.{rem % 100:02d}"


# --------------------------------------------------------------------------- format patterns
_HEADER = re.compile(r"^(EPISODE NUMBER|EPISODE LOGLINE|CONTINUITY NOTES)\s*:\s*(.*)$", re.I)
_SCENE = re.compile(r"^SCENE\s+(\d+)\s*$", re.I)
_SCENE_META = re.compile(r"^SCENE\s+(HEADING|START|END)\s*:\s*(.*)$", re.I)
_SECTION = re.compile(r"^(-+\s*SCRIPT\s+(START|END)\s*-+|ACTIONS AND DIALOGUE\s*:?)$", re.I)
_LABEL_BOLD = re.compile(r"^\*\*(.+?):\*\*\s*$")
_LABEL_PLAIN = re.compile(r"^([A-Z][A-Z0-9 .'&/\-]*(?:\s*\([^()]*\))*)\s*:\s*$")
_TIMING = re.compile(
    r"^START\s*:\s*\(?\s*([\d:.]+)\s*\)?\s*\|\s*END\s*:\s*\(?\s*([\d:.]+)\s*\)?\s*$", re.I
)
_PAREN = re.compile(r"\(([^()]*)\)")

KIND_BY_LABEL = {
    "CAMERA": "shot", "SHOT": "shot",
    "ACTION": "action",
    "SOUND FX": "sfx", "SFX": "sfx", "SOUND": "sfx",
}
# Labels we have not seen in this pack but a new show might use. Parsed as "other" and
# reported by the audit instead of being mistaken for a character name.
OTHER_LABELS = {"MUSIC", "TITLE CARD", "TEXT ON SCREEN", "ON-SCREEN TEXT", "SUPER", "TRANSITION",
                "INSERT", "NOTE", "VFX", "GRAPHIC"}
_MODIFIERS = {
    "O.S.": "O.S.", "O.S": "O.S.", "OS": "O.S.", "OFF-SCREEN": "O.S.", "OFF SCREEN": "O.S.",
    "OFFSCREEN": "O.S.", "O.C.": "O.S.",
    "V.O.": "V.O.", "V.O": "V.O.", "VO": "V.O.", "VOICE-OVER": "V.O.", "VOICEOVER": "V.O.",
    "VOICE OVER": "V.O.",
    "CONT'D": "CONT'D", "CONTD": "CONT'D", "CONT.": "CONT'D", "CONTINUES": "CONT'D",
    "CONTINUED": "CONT'D", "CONTINUING": "CONT'D",
    "ON PHONE": "PHONE", "INTO PHONE": "PHONE", "FILTERED": "PHONE",
}


# --------------------------------------------------------------------------- data model
@dataclass
class Block:
    id: str
    kind: str  # shot | line | action | sfx | other
    ep: int
    scene: int  # 1-based scene index
    start: float
    end: float
    text: str = ""
    speaker: str = ""
    emotions: list[str] = field(default_factory=list)
    modifiers: list[str] = field(default_factory=list)
    label: str = ""
    synthetic: bool = False  # filler cell created by build_grid (no camera description)
    src_line: int = 0

    @property
    def dur(self) -> float:
        return round(self.end - self.start, 2)


@dataclass
class Scene:
    index: int
    heading: str
    start: float
    end: float
    int_ext: str = ""
    time_of_day: str = ""


@dataclass
class Episode:
    ep: int
    logline: str
    continuity: str
    source: str
    duration: float
    scenes: list[Scene]
    shots: list[Block]  # contiguous cells tiling [0, duration]
    lines: list[Block]
    actions: list[Block]
    sfx: list[Block]
    others: list[Block] = field(default_factory=list)
    grid_fixes: list[dict] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._by_id = {b.id: b for b in self.all_blocks()}
        self._shot_starts = [s.start for s in self.shots]
        self._cuts = self._shot_starts + [self.duration]

    # ---- lookups used by the boundary rules
    def all_blocks(self) -> list[Block]:
        return [*self.shots, *self.lines, *self.actions, *self.sfx, *self.others]

    @property
    def beats(self) -> list[Block]:
        """Dialogue, action and sound rows: what a viewer reacts to (no camera rows)."""
        return [*self.lines, *self.actions, *self.sfx]

    def block(self, bid: str) -> Block | None:
        return self._by_id.get(bid)

    def shot_at(self, t: float) -> Block:
        """Cell with start <= t < end (the last cell for t == duration)."""
        i = bisect.bisect_right(self._shot_starts, t + EPS) - 1
        return self.shots[min(max(i, 0), len(self.shots) - 1)]

    def shot_ending_at(self, t: float) -> Block:
        """Cell with start < t <= end: the cell a beat ending at t ends in."""
        return self.shot_at(t - 10 * EPS)

    def cut_points(self) -> list[float]:
        return list(self._cuts)

    def is_cut(self, t: float, tol: float = 0.005) -> bool:
        i = bisect.bisect_left(self._cuts, t - tol)
        return i < len(self._cuts) and abs(self._cuts[i] - t) <= tol

    def lines_crossing(self, t: float) -> list[Block]:
        return [ln for ln in self.lines if ln.start + EPS < t < ln.end - EPS]

    def prev_line_end(self, t: float) -> float:
        return max((ln.end for ln in self.lines if ln.end <= t + EPS), default=0.0)

    def next_line_start(self, t: float) -> float:
        return min((ln.start for ln in self.lines if ln.start >= t - EPS), default=self.duration)

    def lines_within(self, a: float, b: float) -> list[Block]:
        return [ln for ln in self.lines if ln.start >= a - EPS and ln.end <= b + EPS]

    def blocks_overlapping(self, a: float, b: float, kinds=("shot", "line", "action", "sfx")) -> list[Block]:
        out = [x for x in self.all_blocks() if x.kind in kinds and x.start < b - EPS and x.end > a + EPS]
        order = {"shot": 0, "action": 1, "sfx": 2, "line": 3, "other": 4}
        return sorted(out, key=lambda x: (x.start, order.get(x.kind, 9)))

    def scene_at(self, t: float) -> Scene:
        for sc in self.scenes:
            if sc.start - EPS <= t < sc.end - EPS:
                return sc
        return self.scenes[-1]


@dataclass
class Show:
    show_id: str
    title: str
    source_dir: str
    episodes: dict[int, Episode]

    def ep(self, n: int) -> Episode:
        return self.episodes[n]

    def block(self, bid: str) -> Block | None:
        e = ep_of(bid)
        return self.episodes[e].block(bid) if e in self.episodes else None

    @property
    def episode_numbers(self) -> list[int]:
        return sorted(self.episodes)


def ep_of(block_id: str) -> int:
    """'E08.L07' -> 8"""
    try:
        return int(block_id.split(".", 1)[0].lstrip("Ee"))
    except (ValueError, AttributeError):
        return -1


# --------------------------------------------------------------------------- parsing
def _parse_dialogue_label(label: str) -> tuple[str, list[str], list[str]]:
    name = label.split("(", 1)[0].strip()
    mods: list[str] = []
    emos: list[str] = []
    for group in _PAREN.findall(label):
        for part in (p.strip() for p in re.split(r"[/,]", group)):
            if not part:
                continue
            key = part.upper()
            if key in _MODIFIERS:
                if _MODIFIERS[key] not in mods:
                    mods.append(_MODIFIERS[key])
            else:
                emos.append(part.lower())
    return name, mods, emos


def _kind_for(label: str) -> str:
    up = label.strip().upper()
    if up in KIND_BY_LABEL:
        return KIND_BY_LABEL[up]
    if up in OTHER_LABELS:
        return "other"
    return "line"


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] in "\"“'" and s[-1] in "\"”'":
        return s[1:-1].strip()
    return s


def _heading_parts(heading: str) -> tuple[str, str]:
    h = heading.strip()
    m = re.match(r"^(INT\.?/EXT\.?|EXT\.?/INT\.?|INT\.?|EXT\.?|I/E\.?)\s", h, re.I)
    int_ext = m.group(1).upper().replace(".", "") if m else ""
    parts = re.split(r"\s[-–—]\s", h)
    tod = parts[-1].strip().upper() if len(parts) > 1 else ""
    return int_ext, tod


def build_grid(shots: list[Block], lo: float, hi: float, ep: int, scene: int) -> tuple[list[Block], list[dict]]:
    """Turn the scene's camera blocks into contiguous cells tiling [lo, hi].

    overlap -> earlier shot ends where the next one starts (the cut happens when the new shot starts)
    gap < MIN_CELL -> absorbed by the previous shot;  gap >= MIN_CELL -> synthetic filler cell
    """
    fixes: list[dict] = []
    cells = sorted((Block(**{**asdict(s)}) for s in shots), key=lambda b: (b.start, b.end))
    kept: list[Block] = []
    for c in cells:
        if c.end - c.start <= EPS:
            fixes.append({"fix": "drop_zero_length", "at": c.start, "src_line": c.src_line})
            continue
        if c.start < lo - EPS or c.end > hi + EPS:
            fixes.append({"fix": "clamp_to_scene", "at": c.start, "src_line": c.src_line,
                          "was": [c.start, c.end], "scene": [lo, hi]})
            c.start, c.end = max(c.start, lo), min(c.end, hi)
            if c.end - c.start <= EPS:
                continue
        if kept and c.start < kept[-1].end - EPS:  # overlap with previous shot
            prev = kept[-1]
            fixes.append({"fix": "overlap_trim", "at": c.start, "overlap_s": round(prev.end - c.start, 2),
                          "src_line": prev.src_line})
            prev.end = c.start
            if prev.end - prev.start <= EPS:
                kept.pop()
                fixes.append({"fix": "drop_covered_shot", "at": prev.start, "src_line": prev.src_line})
        kept.append(c)

    out: list[Block] = []

    def fill(a: float, b: float, where: str) -> None:
        gap = round(b - a, 2)
        if gap <= EPS:
            return
        if gap < MIN_CELL and out:
            out[-1].end = b
            fixes.append({"fix": f"{where}_gap_absorbed", "at": a, "gap_s": gap})
        elif gap < MIN_CELL and where == "lead" and kept:
            kept[0].start = a  # extend the first shot backwards
            fixes.append({"fix": "lead_gap_absorbed", "at": a, "gap_s": gap})
        else:
            out.append(Block(id="", kind="shot", ep=ep, scene=scene, start=a, end=b, synthetic=True,
                             text="(no camera description in script)"))
            fixes.append({"fix": f"{where}_gap_filled", "at": a, "gap_s": gap})

    cursor = lo
    for c in kept:
        if c.start > cursor + EPS:
            fill(cursor, c.start, "lead" if not out else "mid")
        elif c.start < cursor - EPS:  # cannot happen after overlap trim, kept for safety
            c.start = cursor
        out.append(c)
        cursor = c.end
    if hi > cursor + EPS:
        fill(cursor, hi, "tail")
    if not out:
        out.append(Block(id="", kind="shot", ep=ep, scene=scene, start=lo, end=hi, synthetic=True,
                         text="(no camera description in script)"))
        fixes.append({"fix": "scene_without_shots", "at": lo, "gap_s": round(hi - lo, 2)})
    for c in out:
        c.start, c.end = round(c.start, 2), round(c.end, 2)
    return out, fixes


def parse_episode_text(text: str, source: str = "", ep_hint: int | None = None) -> Episode:
    lines = text.splitlines()
    header: dict[str, str] = {}
    scenes: list[dict] = []
    raw: list[dict] = []
    issues: list[dict] = []
    n = len(lines)

    def structural(s: str) -> bool:
        return bool(_HEADER.match(s) or _SCENE.match(s) or _SCENE_META.match(s) or _SECTION.match(s)
                    or _LABEL_BOLD.match(s) or _TIMING.match(s))

    i = 0
    while i < n:
        s = lines[i].strip()
        if not s or _SECTION.match(s):
            i += 1
            continue
        if m := _HEADER.match(s):
            header[m.group(1).upper()] = m.group(2).strip()
            i += 1
            continue
        if m := _SCENE.match(s):
            scenes.append({"index": len(scenes) + 1, "label": int(m.group(1)), "heading": "",
                           "start": None, "end": None})
            i += 1
            continue
        if m := _SCENE_META.match(s):
            if not scenes:
                scenes.append({"index": 1, "label": 1, "heading": "", "start": None, "end": None})
            key, val = m.group(1).upper(), m.group(2).strip()
            if key == "HEADING":
                scenes[-1]["heading"] = val
            else:
                try:
                    scenes[-1][key.lower()] = parse_ts(val)
                except ValueError:
                    issues.append({"issue": "bad_scene_time", "line": i + 1, "text": s})
            i += 1
            continue
        lab = _LABEL_BOLD.match(s) or _LABEL_PLAIN.match(s)
        if lab:
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            tm = _TIMING.match(lines[j].strip()) if j < n else None
            if tm:
                try:
                    start, end = parse_ts(tm.group(1)), parse_ts(tm.group(2))
                except ValueError:
                    issues.append({"issue": "bad_block_time", "line": j + 1, "text": lines[j].strip()})
                    i = j + 1
                    continue
                k = j + 1
                body: list[str] = []
                while k < n and lines[k].strip() and not structural(lines[k].strip()):
                    body.append(lines[k].strip())
                    k += 1
                if not scenes:
                    scenes.append({"index": 1, "label": 1, "heading": "", "start": None, "end": None})
                raw.append({"label": lab.group(1).strip(), "start": start, "end": end,
                            "text": " ".join(body), "scene": scenes[-1]["index"], "src_line": i + 1})
                i = k
                continue
            if _LABEL_BOLD.match(s):
                issues.append({"issue": "label_without_timing", "line": i + 1, "text": s})
                i += 1
                continue
        issues.append({"issue": "unparsed_line", "line": i + 1, "text": s[:120]})
        i += 1

    ep_num = ep_hint
    if "EPISODE NUMBER" in header:
        m = re.search(r"(\d+)", header["EPISODE NUMBER"])
        if m:
            ep_num = int(m.group(1))
    if ep_num is None:
        raise ValueError(f"{source}: cannot determine episode number")

    # scene bounds: fall back to block extents when a scene omits start/end
    for sc in scenes:
        mine = [b for b in raw if b["scene"] == sc["index"]]
        if sc["start"] is None:
            sc["start"] = min((b["start"] for b in mine), default=0.0)
            issues.append({"issue": "scene_start_inferred", "scene": sc["index"]})
        if sc["end"] is None:
            sc["end"] = max((b["end"] for b in mine), default=sc["start"])
            issues.append({"issue": "scene_end_inferred", "scene": sc["index"]})
    duration = round(max([sc["end"] for sc in scenes] + [b["end"] for b in raw] + [0.0]), 2)

    # blocks
    prefix = f"E{ep_num:02d}"
    shots_raw: dict[int, list[Block]] = {sc["index"]: [] for sc in scenes}
    lines_b: list[Block] = []
    actions: list[Block] = []
    sfx: list[Block] = []
    others: list[Block] = []
    for b in raw:
        kind = _kind_for(b["label"])
        if b["end"] < b["start"] - EPS:
            issues.append({"issue": "end_before_start", "line": b["src_line"], "label": b["label"]})
            b["start"], b["end"] = b["end"], b["start"]
        blk = Block(id="", kind=kind, ep=ep_num, scene=b["scene"], start=b["start"], end=b["end"],
                    text=b["text"], label=b["label"], src_line=b["src_line"])
        if kind == "line":
            blk.speaker, blk.modifiers, blk.emotions = _parse_dialogue_label(b["label"])
            blk.text = _strip_quotes(b["text"])
            lines_b.append(blk)
        elif kind == "shot":
            shots_raw[b["scene"]].append(blk)
        elif kind == "action":
            actions.append(blk)
        elif kind == "sfx":
            sfx.append(blk)
        else:
            others.append(blk)

    # cut grid, scene by scene, then cover any time between/after scenes
    scene_objs: list[Scene] = []
    cells: list[Block] = []
    fixes: list[dict] = []
    for sc in sorted(scenes, key=lambda x: x["start"]):
        ie, tod = _heading_parts(sc["heading"])
        scene_objs.append(Scene(index=sc["index"], heading=sc["heading"], start=sc["start"], end=sc["end"],
                                int_ext=ie, time_of_day=tod))
        lo = max(sc["start"], cells[-1].end if cells else 0.0)
        if cells and sc["start"] > cells[-1].end + EPS:
            gap_cells, gap_fixes = build_grid([], cells[-1].end, sc["start"], ep_num, sc["index"])
            cells += gap_cells
            fixes += [{**f, "fix": "between_scenes_" + f["fix"]} for f in gap_fixes]
        sc_cells, sc_fixes = build_grid(shots_raw[sc["index"]], lo, max(sc["end"], lo), ep_num, sc["index"])
        cells += sc_cells
        fixes += [{**f, "scene": sc["index"]} for f in sc_fixes]
    if not cells or cells[-1].end < duration - EPS:
        tail_lo = cells[-1].end if cells else 0.0
        tail, tail_fixes = build_grid([], tail_lo, duration, ep_num, scene_objs[-1].index if scene_objs else 1)
        if cells and duration - tail_lo < MIN_CELL:
            cells[-1].end = duration
            fixes.append({"fix": "episode_tail_absorbed", "at": tail_lo, "gap_s": round(duration - tail_lo, 2)})
        else:
            cells += tail
            fixes += [{**f, "fix": "episode_tail_" + f["fix"]} for f in tail_fixes]

    # stable IDs in time order
    for k, c in enumerate(cells, 1):
        c.id = f"{prefix}.S{k:02d}"
    for group, letter in ((lines_b, "L"), (actions, "A"), (sfx, "X"), (others, "O")):
        group.sort(key=lambda x: (x.start, x.src_line))
        for k, x in enumerate(group, 1):
            x.id = f"{prefix}.{letter}{k:02d}"

    return Episode(ep=ep_num, logline=header.get("EPISODE LOGLINE", ""),
                   continuity=header.get("CONTINUITY NOTES", ""), source=source, duration=duration,
                   scenes=scene_objs, shots=cells, lines=lines_b, actions=actions, sfx=sfx, others=others,
                   grid_fixes=fixes, issues=issues)


def _ep_hint_from_name(name: str) -> int | None:
    m = re.search(r"episode[-_ ]?(\d+)", name, re.I)
    return int(m.group(1)) if m else None


def parse_show(show_dir: str | Path, title: str | None = None) -> Show:
    d = Path(show_dir)
    files = sorted(d.glob("*.txt"))
    files = [f for f in files if f.name.lower() != "title.txt"]
    if not files:
        raise FileNotFoundError(f"no .txt screenplays in {d}")
    episodes: dict[int, Episode] = {}
    for k, f in enumerate(files, 1):
        ep = parse_episode_text(f.read_text(encoding="utf-8-sig"), source=f.name,
                                ep_hint=_ep_hint_from_name(f.name) or k)
        if ep.ep in episodes:
            raise ValueError(f"duplicate episode number {ep.ep}: {episodes[ep.ep].source} and {f.name}")
        episodes[ep.ep] = ep
    if title is None:
        tf = d / "title.txt"
        title = tf.read_text(encoding="utf-8-sig").strip() if tf.exists() else d.name
    return Show(show_id=d.name, title=title, source_dir=str(d), episodes=episodes)


# --------------------------------------------------------------------------- persistence
def show_to_dict(show: Show) -> dict:
    return {"show_id": show.show_id, "title": show.title, "source_dir": show.source_dir,
            "episodes": [asdict(show.episodes[k]) for k in sorted(show.episodes)]}


def show_from_dict(d: dict) -> Show:
    eps = {}
    for e in d["episodes"]:
        e = dict(e)
        e["scenes"] = [Scene(**s) for s in e["scenes"]]
        for key in ("shots", "lines", "actions", "sfx", "others"):
            e[key] = [Block(**b) for b in e.get(key, [])]
        eps[e["ep"]] = Episode(**e)
    return Show(show_id=d["show_id"], title=d["title"], source_dir=d["source_dir"], episodes=eps)


def save_show(show: Show, path: str | Path) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(show_to_dict(show), ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    return str(p)


def load_show(path: str | Path) -> Show:
    """Parsed show from disk, cached in memory. Keyed by mtime so a re-parse is picked up."""
    p = Path(path).resolve()
    return _load_show_cached(str(p), p.stat().st_mtime_ns)


@lru_cache(maxsize=4)
def _load_show_cached(path: str, _mtime: int) -> Show:
    return show_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# --------------------------------------------------------------------------- CLI: print a timeline
def print_episode(ep: Episode, out=sys.stdout) -> None:
    """Readable timeline, handy for hand-labelling the gold set."""
    out.write(f"EPISODE {ep.ep}  ({ep.duration:.2f}s, {len(ep.scenes)} scene(s), {len(ep.shots)} cells, "
              f"{len(ep.lines)} lines)\n{ep.logline}\n")
    for sc in ep.scenes:
        out.write(f"\n== {sc.heading}  [{fmt_ts(sc.start)}-{fmt_ts(sc.end)}]\n")
        for b in ep.blocks_overlapping(sc.start, sc.end):
            if b.scene != sc.index and b.kind != "shot":
                continue
            if b.kind == "shot" and not (sc.start - EPS <= b.start < sc.end - EPS):
                continue
            who = {"shot": "CAM", "action": "ACT", "sfx": "SFX", "line": b.speaker, "other": b.label}[b.kind]
            emo = f" ({', '.join(b.emotions)})" if b.emotions else ""
            body = f'"{b.text}"' if b.kind == "line" else b.text
            out.write(f"{fmt_ts(b.start)}-{fmt_ts(b.end)} {b.id.split('.')[1]:>4} {who}{emo}: {body}\n")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Parse a show folder; optionally print one episode's timeline.")
    ap.add_argument("show_dir")
    ap.add_argument("--episode", type=int, help="print this episode's timeline (for gold labelling)")
    ap.add_argument("--out", help="write parsed.json here")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    show = parse_show(args.show_dir)
    if args.out:
        save_show(show, args.out)
        print(f"wrote {args.out}")
    if args.episode is not None:
        print_episode(show.ep(args.episode))
    elif not args.out:
        for k in show.episode_numbers:
            e = show.ep(k)
            print(f"E{k:02d} {e.duration:7.2f}s scenes={len(e.scenes)} cells={len(e.shots)} lines={len(e.lines)} "
                  f"fixes={len(e.grid_fixes)} issues={len(e.issues)}")


if __name__ == "__main__":
    main()
