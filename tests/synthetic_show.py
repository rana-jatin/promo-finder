"""Generates a small invented show (no Kuku content) so the full graph can be tested anywhere."""
from __future__ import annotations

from pathlib import Path

from src.parse import fmt_ts

BEATS = [
    ("MARA", "Gentle", "We should go home now."),
    ("TOMAS", "Cold, Mocking", "Home? You have no home, beggar."),
    ("MARA", "Defiant", "You will regret those words."),
    ("TOMAS", "Smug", "Will I? Who would believe you?"),
    ("MARA", "Trembling", "I know what you did to my father."),
    ("TOMAS", "Furious", "Say that again and you will pay for this!"),
    ("MARA", "Steely", "The guards are already on their way."),
    ("TOMAS", "Menacing", "Then they will find you dead first."),
    ("MARA", "Desperate", "Who are you really?"),
    ("TOMAS", "Calm", "Someone you should never have crossed."),
]


def episode_text(n: int) -> str:
    out = [f"EPISODE NUMBER: Episode {n}",
           f"EPISODE LOGLINE: Mara confronts Tomas, round {n}, and a secret starts to surface.",
           f"CONTINUITY NOTES: Picks up after Episode {n - 1}." if n > 1 else "CONTINUITY NOTES: Series opener.",
           "", "--- SCRIPT START ---", "", "SCENE 01", "Scene heading: INT. GREAT HALL – NIGHT",
           "Scene start: 00:00.00", "Scene end: 01:00.00", "", "Actions and dialogue:", ""]
    t = 0.0
    for k in range(10):
        spk, emo, line = BEATS[(k + n) % len(BEATS)]
        out += ["**CAMERA:**", f"Start: ({fmt_ts(t)}) | End: ({fmt_ts(t + 6.0)})",
                f"Close-up of {spk.title()} in the great hall.", ""]
        if k == 3:
            out += ["**SOUND FX:**", f"Start: ({fmt_ts(t + 0.5)}) | End: ({fmt_ts(t + 1.0)})",
                    "A sharp slap echoes.", "",
                    "**ACTION:**", f"Start: ({fmt_ts(t + 0.4)}) | End: ({fmt_ts(t + 1.2)})",
                    f"{spk.title()} slaps the other across the face.", ""]
        out += [f"**{spk} ({emo}):**", f"Start: ({fmt_ts(t + 1.5)}) | End: ({fmt_ts(t + 5.0)})", f'"{line}"', ""]
        t += 6.0
    out += ["--- SCRIPT END ---", ""]
    return "\n".join(out)


def make_show(root: Path, n_eps: int = 3) -> Path:
    d = root / "synthshow"
    d.mkdir(parents=True, exist_ok=True)
    for n in range(1, n_eps + 1):
        (d / f"episode-{n:03d}-synthetic.txt").write_text(episode_text(n), encoding="utf-8")
    return d
