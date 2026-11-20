from src.boundaries import flash_window, snap_end, snap_start, tier1_start
from src.parse import EPS, fmt_ts, parse_ts


def test_timestamps_round_trip():
    for s in ["00:00.00", "00:29.90", "01:18.00", "03:20.75"]:
        assert fmt_ts(parse_ts(s)) == s
    assert fmt_ts(59.999) == "01:00.00"


def test_synthetic_format_quirks(synthetic_episode):
    ep = synthetic_episode
    assert ep.ep == 3 and len(ep.scenes) == 2
    assert ep.scenes[0].int_ext == "INT" and ep.scenes[0].time_of_day == "NIGHT"
    first = ep.lines[0]
    assert first.speaker == "MARA" and first.modifiers == ["O.S.", "CONT'D"] and first.emotions == []
    tomas = ep.lines[1]
    assert tomas.modifiers == ["V.O."] and tomas.emotions == ["cold", "mocking"]
    assert not ep.issues


def test_synthetic_cut_grid_tiles_timeline(synthetic_episode):
    ep = synthetic_episode
    cells = ep.shots
    assert cells[0].start == 0.0 and cells[-1].end == ep.duration == 30.0
    for a, b in zip(cells, cells[1:]):
        assert abs(a.end - b.start) <= EPS  # contiguous: no gaps, no overlaps
    fixes = {f["fix"] for f in ep.grid_fixes}
    assert "mid_gap_filled" in fixes      # 12.0 -> 15.0 holds a line but no camera block
    assert "tail_gap_filled" in fixes     # scene 1 camera ends at 18.0, scene ends at 20.0
    gap_cell = ep.shot_at(13.0)
    assert gap_cell.synthetic and (gap_cell.start, gap_cell.end) == (12.0, 15.0)


def test_tier1_never_cuts_inside_a_line(synthetic_episode):
    ep = synthetic_episode
    # "You never belonged here." runs 6.5-9.5 across the 8.0/9.0 cuts: a start at 9.0 must move back
    assert tier1_start(ep, 9.0) == 4.0


def _ep8(pack_show):
    ep = pack_show.ep(8)
    line_at = {ln.start: ln for ln in ep.lines}
    shot_at = {c.start: c for c in ep.shots}
    return ep, line_at, shot_at


def test_ep8_shape_matches_hand_count(pack_show):
    ep = pack_show.ep(8)
    assert ep.duration == 78.0 and len(ep.shots) == 33 and len(ep.lines) == 26 and not ep.grid_fixes


def test_ep8_a1_auto_repair(pack_show, cfg):
    """Plan's worked example: tier 1 puts the hook at 3.8s (A0, rejected); tier 2 trims the start to
    00:29.90 so the wrist-catch shot lands at 2.0s; the end stays on the 00:51.30 shot boundary."""
    P = cfg["promo"]
    ep, line_at, shot_at = _ep8(pack_show)
    start_line = line_at[30.10]          # first line of the beat (starts 00:30.10)
    hook_shot = shot_at[31.90]           # wrist-catch shot (starts 00:31.90)
    cliff_line = line_at[50.10]          # last line before the cut (ends 00:51.20)

    a0 = tier1_start(ep, start_line.start)
    assert a0 == 28.10 and round(hook_shot.start - a0, 2) == 3.8

    s = snap_start(ep, start_line.start, P, hook_t=hook_shot.start)
    e = snap_end(ep, cliff_line.end, P)
    assert (fmt_ts(s.t), s.tier) == ("00:29.90", 2)
    assert (fmt_ts(e.t), e.tier) == ("00:51.30", 1)
    assert round(hook_shot.start - s.t, 2) == 2.0 and round(e.t - s.t, 2) == 21.4


def test_ep8_line_crossing_cut_extends_end(pack_show, cfg):
    """A line that crosses a cut (01:09.00-01:12.10 over the 01:10.90 cut) is never split."""
    ep, line_at, _ = _ep8(pack_show)
    ln = line_at[69.00]
    assert ep.lines_crossing(70.90) == [ln]
    e = snap_end(ep, ln.end, cfg["promo"])
    assert e.t >= ln.end and not ep.lines_crossing(e.t)


def test_ep8_flash_window(pack_show, cfg):
    ep, _, shot_at = _ep8(pack_show)
    w = flash_window(ep, shot_at[31.90], cfg["promo"])
    assert w is not None
    a, b = w
    lo, hi = cfg["promo"]["cold_open_flash"]
    assert lo - EPS <= b - a <= hi + EPS and not ep.lines_crossing(a) and not ep.lines_crossing(b)
