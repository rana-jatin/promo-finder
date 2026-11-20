"""Writes sample_data/demo_show/: three invented episodes (no Kuku content) in the pack's screenplay format,
so the public demo can run the full pipeline. Timestamps are computed, so every shot tiles its scene.

    python tools/make_sample_show.py
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "sample_data" / "demo_show"
TITLE = "The Ferry Girl Is the Missing Heiress"

# beat kinds: ("L", speaker, emotion, line, seconds[, camera])  dialogue
#             ("A", action, seconds[, camera])                  action
#             ("X", sound, seconds[, camera])                   sound effect (+ a short action shot)
EPISODES = [
    {
        "logline": "Night-ferry deckhand Ines is humiliated by the would-be heiress Bianca, until a locket that "
                   "falls from her collar catches the eye of shipping magnate Damian Castell.",
        "continuity": "Series opener.",
        "scenes": [
            ("EXT. NIGHT FERRY DECK – NIGHT", [
                ("A", "Ines drags a heavy mooring rope across the wet deck, her uniform soaked through.", 3.0,
                 "Wide shot of a rain-slicked ferry deck under yellow lamps; Ines hauls a rope alone."),
                ("L", "BIANCA", "Cold, Mocking", "Careful, deck girl. That rope costs more than you do.", 3.4),
                ("L", "INES", "Tired, Polite", "Mind the edge, miss. The deck is slippery tonight.", 3.0),
                ("A", "Bianca steps closer, her white fur coat brushing the railing, and kicks the rope out of Ines's hands.", 2.6),
                ("L", "BIANCA", "Smug", "Do you know who I am? Tomorrow I become the Castell heiress.", 3.6),
                ("L", "INES", "Quiet", "Then congratulations. Please step back from the railing.", 3.0),
                ("L", "BIANCA", "Furious", "Are you giving me orders?", 2.0),
                ("X", "A sharp slap cracks over the sound of the rain.", 1.6,
                 "Close-up as Bianca's hand strikes Ines across the face."),
                ("A", "Ines stumbles against the railing; a silver locket slips from her collar and swings in the lamplight.", 2.8),
                ("L", "BIANCA", "Disgusted", "Look at that cheap trinket. Stolen, I bet.", 2.8),
                ("A", "Bianca yanks the locket chain; it snaps and the locket clatters onto the deck.", 2.4),
                ("L", "INES", "Desperate", "Give it back! It's all I have of my mother!", 3.0),
                ("L", "BIANCA", "Cruel", "Then fetch it from the sea.", 2.0),
                ("A", "Bianca lifts the locket over the railing; a man's hand closes around her wrist.", 2.6,
                 "Close-up of a gloved hand catching Bianca's wrist above the black water."),
                ("L", "DAMIAN", "Low, Dangerous", "Drop it, and you go in after it.", 2.6),
                ("L", "BIANCA", "Startled, Sweet", "Mr. Castell! I was only teaching the staff some manners.", 3.4),
                ("L", "DAMIAN", "Cold", "I heard. Every word.", 2.0),
            ]),
            ("INT. FERRY CABIN – NIGHT", [
                ("A", "Damian turns the locket over under a cabin lamp while Ines presses a cloth to her cheek.", 3.0,
                 "Medium shot of a cramped cabin; Damian studies the locket, Ines stands by the door."),
                ("L", "INES", "Wary", "Thank you, sir. May I have it back now?", 2.6),
                ("A", "Damian thumbs the locket open; inside, an engraved crest: a ship's wheel crowned with three stars.", 3.0,
                 "Extreme close-up of the open locket: a ship's wheel under three stars."),
                ("L", "DAMIAN", "Stunned", "This crest was destroyed twenty years ago. With the Castell yacht.", 3.6),
                ("L", "INES", "Confused", "It's just my mother's locket. She died when I was a baby.", 3.2),
                ("L", "DAMIAN", "Intense", "What was her name?", 1.8),
                ("L", "INES", "Hesitant", "My aunt never told me. She said it was safer not to know.", 3.4),
                ("X", "The cabin door bangs open against the wall.", 1.4,
                 "Wide shot as the cabin door flies open; Bianca stands in the doorway, eyes blazing."),
                ("L", "BIANCA", "Shrill", "Damian! That locket is mine. She stole it from my family!", 3.2),
                ("A", "Damian closes the locket slowly and steps between the two women.", 2.4),
                ("L", "DAMIAN", "Ice-cold", "Then tell me, Bianca. What is engraved inside it?", 3.0),
                ("A", "Bianca's smile freezes; her eyes dart to Ines.", 2.2,
                 "Close-up of Bianca, her confident smile cracking."),
                ("L", "BIANCA", "Faltering", "It's... my grandmother's initials.", 2.4),
                ("L", "DAMIAN", "Quiet, Lethal", "Wrong. Where did you really get this locket, Ines?", 3.2,
                 "Close-up of Damian turning to Ines, the locket glinting in his fist."),
            ]),
        ],
    },
    {
        "logline": "Ines's aunt Marisol begs her to throw the locket away, while Bianca's mother Vera plots to have "
                   "the ferry girl fired, and a secret about Ines's mother surfaces.",
        "continuity": "Picks up after Episode 1, where Damian found the Castell crest inside Ines's locket.",
        "scenes": [
            ("INT. MARISOL'S HARBOUR FLAT – NIGHT", [
                ("A", "Ines bursts into a tiny flat full of drying fishing nets; Marisol drops a pot in shock.", 2.8,
                 "Wide shot of a cramped harbour flat as Ines rushes in out of the rain."),
                ("L", "MARISOL", "Alarmed", "Your face! Who did this to you?", 2.2),
                ("L", "INES", "Urgent", "Aunt Marisol, Damian Castell saw the crest in Mother's locket.", 3.2),
                ("A", "Marisol goes pale and grips the back of a chair.", 2.0,
                 "Close-up of Marisol's knuckles whitening on the chair."),
                ("L", "MARISOL", "Terrified", "You showed it to a Castell? Throw it in the harbour. Tonight!", 3.4),
                ("L", "INES", "Defiant", "No. You've hidden the truth from me for twenty years.", 3.0),
                ("L", "MARISOL", "Pleading", "I hid it to keep you alive, child.", 2.4),
                ("L", "INES", "Shaken", "Alive? Alive from whom?", 2.0),
                ("X", "Someone hammers on the door.", 1.4,
                 "Close-up of the door shuddering under heavy knocks."),
                ("A", "Marisol pushes Ines toward the back room and puts a finger to her lips.", 2.4),
            ]),
            ("INT. CASTELL MANSION STUDY – NIGHT", [
                ("A", "Vera, in pearls, pours two glasses of wine while Bianca paces in front of the fireplace.", 3.0,
                 "Wide shot of a grand study lit by firelight."),
                ("L", "BIANCA", "Panicked", "Mother, he knows. He asked me what was inside the locket.", 3.2),
                ("L", "VERA", "Calm, Venomous", "Then we make sure the ferry girl disappears before he asks her.", 3.4),
                ("L", "BIANCA", "Uneasy", "Disappears? You mean fired?", 2.2),
                ("A", "Vera slides an envelope of cash across the desk toward a shadowed figure.", 2.6,
                 "Close-up of a thick envelope sliding across polished wood."),
                ("L", "VERA", "Smiling", "I mean what happened to her mother. The sea keeps secrets.", 3.4),
                ("A", "Bianca's glass slips from her hand and shatters on the floor.", 1.8,
                 "Close-up of a wine glass shattering on marble."),
                ("L", "BIANCA", "Horrified", "You said the yacht sank in a storm.", 2.6),
                ("L", "VERA", "Cold", "Storms are very convenient, darling.", 2.4),
            ]),
            ("INT. MARISOL'S HARBOUR FLAT – NIGHT", [
                ("A", "Marisol opens the door a crack: Damian stands outside, rain dripping from his coat.", 2.8,
                 "Medium shot through the door gap: Damian under a streetlamp."),
                ("L", "DAMIAN", "Measured", "I'm not here to hurt her. I'm here because someone already tried.", 3.4),
                ("A", "Ines steps out from the back room, the locket clenched in her hand.", 2.2),
                ("L", "INES", "Steady", "Then tell me the truth. Both of you.", 2.4),
                ("L", "MARISOL", "Breaking", "Your mother didn't drown that night. She was pushed.", 3.2,
                 "Close-up of Marisol, tears running down her face."),
                ("L", "DAMIAN", "Shocked", "Pushed? By whom?", 1.8),
                ("L", "MARISOL", "Whispering", "By the woman who now sleeps in your father's house.", 3.0,
                 "Extreme close-up of Ines's eyes widening."),
            ]),
        ],
    },
    {
        "logline": "At the Castell gala Bianca is presented as the lost heiress, Ines works the floor as a waitress, "
                   "and Damian's DNA result turns the night upside down.",
        "continuity": "Picks up after Episode 2, where Marisol revealed that Ines's mother was pushed overboard.",
        "scenes": [
            ("INT. CASTELL BALLROOM – NIGHT", [
                ("A", "Crystal chandeliers blaze over a packed ballroom; Ines in a waitress uniform carries a tray of champagne.", 3.0,
                 "Wide shot of a glittering ballroom full of guests in evening dress."),
                ("L", "VERA", "Triumphant", "Friends, tonight the Castell family is whole again. Meet my daughter, the lost heiress!", 4.0),
                ("A", "Bianca descends the staircase in a silver gown to applause.", 2.6,
                 "Low-angle shot of Bianca descending the grand staircase."),
                ("L", "BIANCA", "Gracious, Fake", "I never stopped believing I'd come home.", 2.8),
                ("A", "Bianca spots Ines among the waiters and her smile tightens.", 2.0),
                ("L", "BIANCA", "Sweetly Cruel", "Oh, look. The ferry girl. Bring me a glass, would you?", 3.2),
                ("A", "As Ines offers the tray, Bianca tips it; glasses crash and champagne soaks Ines's uniform.", 2.6,
                 "Close-up as the tray tips and glasses smash on the floor."),
                ("X", "Glass shatters; the orchestra stops mid-note.", 1.4),
                ("L", "VERA", "Loud, Contemptuous", "Security! Remove this clumsy girl from my daughter's party.", 3.2),
                ("L", "INES", "Calm, Defiant", "Your daughter? Are you sure about that, madam?", 2.8),
            ]),
            ("INT. CASTELL BALLROOM – NIGHT", [
                ("A", "Damian strides through the crowd holding a sealed envelope; guests part to let him pass.", 2.8,
                 "Medium tracking shot of Damian cutting through the crowd."),
                ("L", "DAMIAN", "Commanding", "Nobody touches her.", 2.0),
                ("L", "VERA", "Nervous Laugh", "Damian, darling, you're making a scene.", 2.6),
                ("L", "DAMIAN", "Cold", "I ran a DNA test on the hair Bianca left in my car. And on Ines's.", 3.6),
                ("A", "A murmur sweeps through the ballroom; Bianca grips the banister.", 2.2,
                 "Wide shot of shocked guests turning toward the staircase."),
                ("L", "BIANCA", "Shrill", "You had no right!", 1.8),
                ("A", "Damian tears open the envelope and reads; his jaw tightens.", 2.6,
                 "Close-up of the lab report: 'Probability of relationship: 99.98%'."),
                ("L", "DAMIAN", "Grim", "The test came back. Bianca isn't a Castell. Not one drop.", 3.4),
                ("X", "A collective gasp; a champagne flute drops and breaks.", 1.4),
                ("L", "VERA", "Desperate", "It's a lie! That girl is a thief and a liar!", 2.8),
                ("L", "DAMIAN", "Quiet", "And Ines... is my father's granddaughter.", 3.0,
                 "Close-up of Ines, champagne still dripping from her sleeve, frozen."),
                ("A", "The lights cut out; in the darkness a woman screams and a balcony door bangs open.", 2.8,
                 "Wide shot as the ballroom plunges into darkness."),
                ("L", "DAMIAN", "Panicked", "Ines? Ines, where are you?", 2.2,
                 "Close-up of Damian in the dark, scanning the room."),
            ]),
        ],
    },
]


def ts(t: float) -> str:
    m, s = divmod(round(t, 2), 60)
    return f"{int(m):02d}:{s:05.2f}"


def default_camera(beat: tuple) -> str:
    kind = beat[0]
    if kind == "L":
        return f"Close-up of {beat[1].title()} as they speak."
    if kind == "X":
        return "Medium shot capturing the moment of the sound."
    return "Medium shot following the action."


def render_episode(n: int, ep: dict) -> str:
    out = [f"EPISODE NUMBER: Episode {n}", f"EPISODE LOGLINE: {ep['logline']}",
           f"CONTINUITY NOTES: {ep['continuity']}", "", "--- SCRIPT START ---", ""]
    t = 0.0
    for k, (heading, beats) in enumerate(ep["scenes"], 1):
        start = t
        body: list[str] = []
        for b in beats:
            kind = b[0]
            dur = b[4] if kind == "L" else b[2]
            cam = (b[5] if kind == "L" and len(b) > 5 else b[3] if kind != "L" and len(b) > 3 else default_camera(b))
            s, e = t, t + dur
            body += ["**CAMERA:**", f"Start: ({ts(s)}) | End: ({ts(e)})", cam, ""]
            if kind == "L":
                body += [f"**{b[1]} ({b[2]}):**", f"Start: ({ts(s + 0.2)}) | End: ({ts(e)})", f'"{b[3]}"', ""]
            elif kind == "A":
                body += ["**ACTION:**", f"Start: ({ts(s)}) | End: ({ts(e)})", b[1], ""]
            else:
                body += ["**SOUND FX:**", f"Start: ({ts(s + 0.1)}) | End: ({ts(min(e, s + 1.1))})", b[1], ""]
            t = e
        out += [f"SCENE {k:02d}", f"Scene heading: {heading}", f"Scene start: {ts(start)}", f"Scene end: {ts(t)}",
                "", "Actions and dialogue:", ""] + body
    out += ["--- SCRIPT END ---", ""]
    return "\n".join(out)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for n, ep in enumerate(EPISODES, 1):
        (OUT / f"episode-{n:03d}-demo.txt").write_text(render_episode(n, ep), encoding="utf-8")
    (OUT / "title.txt").write_text(TITLE + "\n", encoding="utf-8")
    print(f"wrote {len(EPISODES)} episodes to {OUT}")


if __name__ == "__main__":
    main()
