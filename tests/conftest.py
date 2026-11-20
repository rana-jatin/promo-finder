import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PACK = ROOT / "data" / "show1"

# Invented screenplay (not from the confidential pack) that exercises the format quirks the
# audit found: upper-case scene keys, stacked dialogue modifiers, a shot gap with a line inside,
# an overlapping shot, a line crossing a cut, and a scene whose camera coverage ends early.
SYNTHETIC = """EPISODE NUMBER: Episode 3
EPISODE LOGLINE: A test logline.
CONTINUITY NOTES: Test continuity.

--- SCRIPT START ---

SCENE 01
SCENE HEADING: INT. HALL – NIGHT
SCENE START: 00:00.00
SCENE END: 00:20.00

Actions and dialogue:

**CAMERA:**
Start: (00:00.00) | End: (00:04.00)
Wide shot of a hall.

**MARA (O.S. / Continues):**
Start: (00:00.50) | End: (00:03.00)
"Open the door."

**CAMERA:**
Start: (00:04.00) | End: (00:08.00)
Close-up of a door.

**SOUND FX:**
Start: (00:04.20) | End: (00:04.60)
A sharp slap echoes.

**TOMAS (V.O.) (Cold, Mocking):**
Start: (00:06.50) | End: (00:09.50)
"You never belonged here."

**CAMERA:**
Start: (00:09.00) | End: (00:12.00)
Medium shot of Mara.

**ACTION:**
Start: (00:09.00) | End: (00:11.00)
Mara grabs the letter.

**CAMERA:**
Start: (00:15.00) | End: (00:18.00)
Close-up of the letter.

**MARA (Furious):**
Start: (00:12.50) | End: (00:14.50)
"Who wrote this?"

SCENE 02
Scene heading: EXT. ROAD – DAY
Scene start: 00:20.00
Scene end: 00:30.00

**CAMERA:**
Start: (00:20.00) | End: (00:30.00)
Wide shot of a road.

**TOMAS (Gentle):**
Start: (00:21.00) | End: (00:23.00)
"Come home."

--- SCRIPT END ---
"""


@pytest.fixture
def synthetic_episode():
    from src.parse import parse_episode_text
    return parse_episode_text(SYNTHETIC, source="synthetic.txt")


@pytest.fixture(scope="session")
def pack_show():
    if not PACK.exists():
        pytest.skip("confidential pack not present (data/show1)")
    from src.parse import parse_show
    return parse_show(PACK)


@pytest.fixture(scope="session")
def cfg():
    from src.config import load_config
    return load_config()
