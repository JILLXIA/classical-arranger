"""Turn a WAV and a style prompt into a multitrack MIDI on a YuE2 GPU worker."""
import argparse
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
import json
import os
import re
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile

from arranger import Event, default_midi_path, label, note, read_midi, write_midi

PPQ = 960
DEFAULT_HOME = "/workspace/YuE/skills/yue2-music/instrumental"
# Standard GM program numbers, one-based as shown to the musician.
SOUNDS = {
    "guitar": 30,
    "clean guitar": 28,
    "acoustic guitar": 26,
    "distortion guitar": 31,
    "bass": 35,
    "finger bass": 34,
    "upright bass": 33,
    "synth bass": 39,
    "piano": 1,
    "rhodes": 5,
    "organ": 17,
    "strings": 49,
    "violin": 41,
    "viola": 42,
    "cello": 43,
    "harp": 47,
    "flute": 74,
    "clarinet": 72,
    "sax": 66,
    "trumpet": 57,
    "synth": 82,
    "pad": 90,
    "vibraphone": 12,
}
GM_NAMES = (
    "Acoustic Grand Piano|Bright Acoustic Piano|Electric Grand Piano|Honky-tonk Piano|"
    "Electric Piano 1|Electric Piano 2|Harpsichord|Clavinet|"
    "Celesta|Glockenspiel|Music Box|Vibraphone|"
    "Marimba|Xylophone|Tubular Bells|Dulcimer|"
    "Drawbar Organ|Percussive Organ|Rock Organ|Church Organ|"
    "Reed Organ|Accordion|Harmonica|Tango Accordion|"
    "Acoustic Guitar (nylon)|Acoustic Guitar (steel)|Electric Guitar (jazz)|Electric Guitar (clean)|"
    "Electric Guitar (muted)|Overdriven Guitar|Distortion Guitar|Guitar Harmonics|"
    "Acoustic Bass|Electric Bass (finger)|Electric Bass (pick)|Fretless Bass|"
    "Slap Bass 1|Slap Bass 2|Synth Bass 1|Synth Bass 2|"
    "Violin|Viola|Cello|Contrabass|"
    "Tremolo Strings|Pizzicato Strings|Orchestral Harp|Timpani|"
    "String Ensemble 1|String Ensemble 2|Synth Strings 1|Synth Strings 2|"
    "Choir Aahs|Voice Oohs|Synth Choir|Orchestra Hit|"
    "Trumpet|Trombone|Tuba|Muted Trumpet|"
    "French Horn|Brass Section|Synth Brass 1|Synth Brass 2|"
    "Soprano Sax|Alto Sax|Tenor Sax|Baritone Sax|"
    "Oboe|English Horn|Bassoon|Clarinet|"
    "Piccolo|Flute|Recorder|Pan Flute|"
    "Blown bottle|Shakuhachi|Whistle|Ocarina|"
    "Lead 1 (square)|Lead 2 (sawtooth)|Lead 3 (calliope)|Lead 4 chiff|"
    "Lead 5 (charang)|Lead 6 (voice)|Lead 7 (fifths)|Lead 8 (bass + lead)|"
    "Pad 1 (new age)|Pad 2 (warm)|Pad 3 (polysynth)|Pad 4 (choir)|"
    "Pad 5 (bowed)|Pad 6 (metallic)|Pad 7 (halo)|Pad 8 (sweep)|"
    "FX 1 (rain)|FX 2 (soundtrack)|FX 3 (crystal)|FX 4 (atmosphere)|"
    "FX 5 (brightness)|FX 6 (goblins)|FX 7 (echoes)|FX 8 (sci-fi)|"
    "Sitar|Banjo|Shamisen|Koto|"
    "Kalimba|Bagpipe|Fiddle|Shanai|"
    "Tinkle Bell|Agogo|Steel Drums|Woodblock|"
    "Taiko Drum|Melodic Tom|Synth Drum|Reverse Cymbal|"
    "Guitar Fret Noise|Breath Noise|Seashore|Bird Tweet|"
    "Telephone Ring|Helicopter|Applause|Gunshot"
).split("|")
GM_NUMBERS = {name.lower(): i for i, name in enumerate(GM_NAMES, 1)}
ENSEMBLES = {
    "rock": "lead=guitar,bass=bass,drums,chords=synth",
    "swing": "lead=sax,bass=upright bass,drums,chords=rhodes",
    "soft": "lead=violin,bass=cello,chords=strings,arp=harp",
    "dance": "lead=synth,bass=synth bass,drums,arp=pad",
}


@dataclass(frozen=True)
class Part:
    role: str
    sound: str
    channel: int
    program: int


def parse_parts(spec):
    parts = []
    channels = iter(i for i in range(16) if i != 9)
    for item in spec.lower().split(","):
        role, _, sound = (field.strip() for field in item.partition("="))
        if role == "drums" and not sound:
            if any(p.role == role for p in parts):
                raise ValueError("Use one GM drums track.")
            parts.append(Part(role, "Rock Kit", 9, 0))
            continue
        if role not in {"lead", "bass", "chords", "arp"}:
            raise ValueError("Use lead=..., bass=..., chords=..., arp=..., or drums.")
        number = (
            int(sound)
            if sound.isascii() and sound.isdecimal()
            else SOUNDS.get(sound, GM_NUMBERS.get(sound))
        )
        if number is None or not 1 <= number <= 128:
            raise ValueError(f"Unknown sound '{sound}'. Use --list-sounds or a GM number from 1 to 128.")
        channel = next(channels, None)
        if channel is None:
            raise ValueError("A MIDI file supports up to 15 melodic channels plus drums.")
        parts.append(Part(role, GM_NAMES[number - 1], channel, number - 1))
    return parts


def choose_groove(prompt):
    lower = prompt.lower()
    for name, tags in (
        ("swing", ("jazz", "swing")),
        ("dance", ("edm", "house", "disco", "techno")),
        ("soft", ("ballad", "chamber", "orchestral", "ambient")),
    ):
        if any(tag in lower for tag in tags):
            return name
    return "rock"


@dataclass
class Beat:
    time: float
    position: int
    numerator: int
    denominator: int
    tick: int = 0
    tempo: int = 500000

    @property
    def width(self):
        return 4 * PPQ // self.denominator


class BeatMap:
    def __init__(self, path):
        self.beats = []
        for line in path.read_text().splitlines():
            secs, pos, num, den = line.split()
            beat = Beat(float(secs), int(pos), int(num), int(den))
            if beat.denominator not in (1, 2, 4, 8, 16, 32) or not 1 <= beat.numerator <= 255:
                raise ValueError(f"Unsupported transcribed meter {num}/{den}.")
            self.beats.append(beat)
        if len(self.beats) < 2:
            raise ValueError("The recording needs at least two detected beats.")
        self.times = [b.time for b in self.beats]
        for i, (a, b) in enumerate(zip(self.beats, self.beats[1:])):
            a.tempo = round((b.time - a.time) * 1e6 * PPQ / a.width)
            if not 1 <= a.tempo < 2**24:
                raise ValueError("The transcription contains an invalid beat interval.")
            if i == 0:
                a.tick = round(a.time * 1e6 * PPQ / a.tempo)
            b.tick = a.tick + a.width
            b.tempo = a.tempo

    def tick(self, seconds):
        beat = self.beats[max(0, bisect_right(self.times, seconds) - 1)]
        return max(0, beat.tick + round((seconds - beat.time) * 1e6 * PPQ / beat.tempo))

    def conductor(self):
        events = []
        previous = None
        for i, b in enumerate(self.beats):
            tick = b.tick if i else 0
            if not i or b.tempo != self.beats[i - 1].tempo:
                events.append(Event(tick, 0x151, b.tempo.to_bytes(3, "big")))
            if i and b.position != 1:
                continue
            meter = (b.numerator, b.denominator)
            # SheetSage may label the pickup as its own incomplete measure.
            if i == 0 and b.tick == 0:
                count = next(
                    (j for j, x in enumerate(self.beats[1:], 1) if x.position == 1), b.numerator
                )
                meter = (min(count, b.numerator), b.denominator)
            if meter != previous:
                events.append(Event(tick, 0x158, bytes((meter[0], meter[1].bit_length() - 1, 24, 8))))
                previous = meter
        return events


def groove_hits(role, groove, index, beat):
    """(fraction of beat, accent) pulses. Drum accents carry GM percussion pitches."""
    back = beat.position % 2 == 0
    off = 2 / 3 if groove == "swing" else 0.5
    if role == "drums":
        if groove == "soft":
            return [(0, 37 if back else 36), (0.5, 42)] if back or beat.position == 1 else [(0, 42)]
        hits = [(0, 51 if groove == "swing" else 42), (off, 42)]
        if not back or groove == "dance":
            hits.append((0, 36))
        if back:
            hits.append((0, 38))
        if index % 8 == 6:
            hits.append((off, 36))
        if index % 16 == 15:
            hits.extend([(0.25, 50), (0.75, 45)])
        if index % 32 == 0:
            hits.append((0, 49))
        return hits
    if role == "arp":
        return [(0, 0), (off, 1)] if groove != "dance" else [(x / 4, x) for x in range(4)]
    if role == "bass":
        return [(0, 0)] if groove in ("soft", "swing") else [(0, 0), (off, 1)]
    if groove == "soft":
        return [(0, 0)] if beat.position == 1 else []
    return [(off, 0)] if groove != "dance" else [(0, 0), (off, 0)]


def arrangement(folder, parts, groove):
    playback = json.loads((folder / "playback.json").read_text())
    grid = BeatMap(folder / "beat.lab")
    end = grid.tick(playback["duration"])
    voices = {t["name"].lower(): t["notes"] for t in playback["tracks"]}
    melody = voices.get("ins") or voices.get("vocal") or []
    groups = defaultdict(list)
    for n in voices.get("chords", []):
        groups[grid.tick(n["start"])].append((n["pitch"], grid.tick(n["end"])))
    changes = sorted(groups)
    chord_notes = [sorted(groups[t]) for t in changes]
    beat_ticks = [b.tick for b in grid.beats]
    generated = [grid.conductor()]
    for part in parts:
        role, ch = part.role, part.channel
        track = [
            label(f"{role.title()} - {part.sound.title()}"),
            Event(0, 0xC0 + ch, bytes([part.program])),
            Event(
                0,
                0xB0 + ch,
                bytes([7, {"lead": 103, "bass": 96, "drums": 98, "arp": 74, "chords": 76}[role]]),
            ),
        ]
        generated.append(track)
        if role == "lead":
            if not melody:
                raise ValueError("No lead melody was detected in this performance.")
            for n in melody:
                start = grid.tick(n["start"])
                note(track, ch, n["pitch"], start, max(1, grid.tick(n["end"]) - start), n["velocity"])
            continue
        if role != "drums" and not changes:
            raise ValueError("No harmony was detected for the requested accompaniment.")
        pulses = {}
        for i, b in enumerate(grid.beats):
            for fraction, accent in groove_hits(role, groove, i, b):
                t = b.tick + round(fraction * b.width)
                if t >= end:
                    continue
                if role == "drums":
                    velocity = 108 if accent in (36, 38, 49) else 67 if fraction else 80
                    note(track, ch, accent, t, min(end - t, max(1, b.width // 12)), velocity)
                else:
                    pulses[t] = (i, accent, b.width)
        if role == "drums":
            continue
        for t in changes:
            i = max(0, bisect_right(beat_ticks, t) - 1)
            pulses.setdefault(t, (i, 0, grid.beats[i].width))
        strikes = sorted(pulses)
        for j, t in enumerate(strikes):
            k = bisect_right(changes, t) - 1
            if k < 0:
                continue
            i, accent, width = pulses[t]
            active = [p for p, off in chord_notes[k] if off > t]
            if not active:
                continue
            root = active[0]
            upper = active[1:] or active
            if role == "chords":
                pitches = upper
            elif role == "arp":
                pitches = [upper[(i + accent) % len(upper)]]
            else:
                pitches = [root + (12 if accent and i % 4 == 2 else 7 if accent and i % 4 == 3 else 0)]
            stop = min(
                end,
                strikes[j + 1] if j + 1 < len(strikes) else end,
                min(off for p, off in chord_notes[k] if off > t),
            )
            gate = 0.88 if groove == "soft" or role == "bass" and groove == "swing" else 0.72
            length = round(min(stop - t, width * 4 if groove == "soft" else width) * gate)
            for p in pitches:
                if length > 0:
                    note(track, ch, min(127, p), t, length, 89 if role == "bass" else 70)
    return write_midi(PPQ, generated, end)


def run_over_ssh(host, port, src, dest, args):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.@-]*", host) or not 1 <= port <= 65535:
        raise ValueError("Invalid SSH host or port.")
    ssh = ["ssh", "-T", "-o", "ServerAliveInterval=30", "-p", str(port), host]
    root = "/workspace/wav2midi"
    job = subprocess.check_output(
        ssh + ["mktemp -d /workspace/wav2midi/.request.XXXXXXXX"], text=True
    ).strip()
    if not re.fullmatch(r"/workspace/wav2midi/\.request\.[A-Za-z0-9]+", job):
        raise ValueError("The worker returned an unexpected upload directory.")
    try:
        with src.open("rb") as stream:
            subprocess.run(ssh + [f"cat > {job}/input.wav"], stdin=stream, check=True)
        command = [
            "python3",
            root + "/wav2midi.py",
            job + "/input.wav",
            args.prompt,
            "--out",
            job + "/output.mid",
            "--seed",
            str(args.seed),
        ]
        if args.parts is not None:
            command.extend(["--parts", args.parts])
        if args.groove:
            command.extend(["--groove", args.groove])
        subprocess.run(ssh + [shlex.join(command)], check=True)
        data = subprocess.check_output(ssh + ["cat " + job + "/output.mid"])
        read_midi(data)
        with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
            staged = Path(tmp) / "output.mid"
            staged.write_bytes(data)
            os.link(staged, dest)
    except (OSError, ValueError, subprocess.CalledProcessError, KeyboardInterrupt):
        print(f"SSH job files retained at {host}:{job}", file=sys.stderr)
        raise
    subprocess.run(ssh + ["rm -rf -- " + job], check=False)
    print(dest)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("wav", type=Path, nargs="?")
    cli.add_argument("prompt", nargs="?")
    cli.add_argument(
        "--parts", help="Comma-separated role=sound list, e.g. lead=violin,bass=cello,arp=harp,drums"
    )
    cli.add_argument(
        "--groove",
        choices=list(ENSEMBLES),
        help="Symbolic accompaniment rhythm (default: infer from prompt)",
    )
    cli.add_argument("--out", type=Path, help="Destination MIDI (default: project result/ directory)")
    cli.add_argument(
        "--ssh", default=os.getenv("YUE2_SSH"), help="Upload WAV and download MIDI via SSH on a Mac"
    )
    cli.add_argument("--port", type=int, default=os.getenv("YUE2_SSH_PORT", "22"), help="SSH port")
    cli.add_argument("--seed", type=int, default=42)
    cli.add_argument("--list-sounds", action="store_true")
    args = cli.parse_args()
    if args.list_sounds:
        for number, name in enumerate(GM_NAMES, 1):
            print(f"{number:3}  {name}")
        print(
            "Aliases: "
            + ", ".join(SOUNDS)
            + "\nAny GM program 1–128 is also accepted. Drums use channel 10."
        )
        return
    if not args.wav or not args.prompt or not args.prompt.strip():
        cli.error("Supply a WAV path and a quoted arrangement prompt.")
    src = args.wav.absolute()
    if src.suffix.lower() != ".wav" or not src.is_file():
        cli.error("Input must be an existing WAV file.")
    groove = args.groove or choose_groove(args.prompt)
    parts = parse_parts(args.parts if args.parts is not None else ENSEMBLES[groove])
    dest = args.out or default_midi_path(src)
    if dest.suffix.lower() not in {".mid", ".midi"} or dest.exists():
        cli.error("Choose a new .mid output path.")
    if not 0 <= args.seed < 2**63:
        cli.error("Seed must be in 0..2^63-1.")
    dest = dest.absolute()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if args.ssh:
        return run_over_ssh(args.ssh, args.port, src, dest, args)
    home = Path(os.getenv("YUE2_INSTRUMENTAL", DEFAULT_HOME)).resolve()
    exe = home / ".venv/bin/python"
    transcriber = home / ".venv-transcribe/bin/python"
    model = home / "models/SheetSage2"
    base = home / "models/MERT-v2-FullSong"
    for p in (exe, transcriber, model / "model.safetensors", base / "model.safetensors"):
        if not p.is_file():
            raise ValueError(f"Missing GPU worker dependency: {p}")
    print("MIDI parts:", ", ".join(p.role + "=" + p.sound for p in parts), flush=True)
    with tempfile.TemporaryDirectory(prefix=".wav2midi-", dir=dest.parent) as tmp:
        work = Path(tmp)
        cover = work / "cover"
        transcription = work / "transcription"
        prompt = args.prompt
        if args.parts:
            prompt += ". Target ensemble: " + ", ".join(f"{p.role}: {p.sound}" for p in parts) + "."
        subprocess.run(
            [
                exe,
                "scripts/instrumental.py",
                "cover",
                "--audio",
                src,
                "--style",
                prompt,
                "--transcriber-python",
                transcriber,
                "--transcriber-model",
                model,
                "--base-model",
                base,
                "--models-root",
                home / "models",
                "--offline",
                "--seed",
                str(args.seed),
                "--output",
                cover,
            ],
            cwd=home,
            check=True,
        )
        subprocess.run(
            [
                transcriber,
                "scripts/transcribe_cover.py",
                cover / "generation/native/audio.flac",
                "--task",
                "full",
                "--model",
                model,
                "--base-model",
                base,
                "--offline",
                "--output",
                transcription,
            ],
            cwd=home,
            check=True,
        )
        data = arrangement(transcription, parts, groove)
        staged = work / "arrangement.mid"
        staged.write_bytes(data)
        os.link(staged, dest)
    print(dest)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        sys.exit(str(exc))
