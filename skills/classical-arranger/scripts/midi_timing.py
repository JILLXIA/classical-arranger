"""Plan a smooth tempo change, inspect an export, or map aligned audio to a new MIDI clock."""
import argparse
from bisect import bisect_right
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from arranger import Event, inspect_bytes, read_midi, write_midi

TEMPO = 0x151
END = 0x12F
DEFAULT_TEMPO = 500_000


class MidiClock:
    """Convert between MIDI ticks and elapsed seconds, including pickups and tempo changes."""

    def __init__(self, ppq, tracks):
        changes = {}
        for track in tracks:
            for event in track:
                if event.kind != TEMPO:
                    continue
                value = int.from_bytes(event.data, "big")
                if len(event.data) != 3 or value == 0:
                    raise ValueError("Malformed MIDI tempo.")
                if changes.setdefault(event.tick, value) != value:
                    raise ValueError(f"Conflicting tempos at tick {event.tick}.")
        changes.setdefault(0, DEFAULT_TEMPO)
        self.ppq = ppq
        self.ticks = sorted(changes)
        self.tempos = [changes[tick] for tick in self.ticks]
        self.times = [0.0]
        for index, tick in enumerate(self.ticks[1:]):
            delta = (tick - self.ticks[index]) * self.tempos[index] / (ppq * 1e6)
            self.times.append(self.times[-1] + delta)

    def seconds(self, tick):
        i = bisect_right(self.ticks, tick) - 1
        return self.times[i] + (tick - self.ticks[i]) * self.tempos[i] / (self.ppq * 1e6)

    def tick(self, seconds):
        i = bisect_right(self.times, seconds) - 1
        return self.ticks[i] + (seconds - self.times[i]) * self.ppq * 1e6 / self.tempos[i]

    def tempo(self, tick):
        return self.tempos[bisect_right(self.ticks, tick) - 1]


def nonnegative(text):
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise ValueError("Expected finite nonnegative seconds.")
    return value


def target(text):
    try:
        when, bpm = text.split("=")
        seconds = nonnegative(when)
        value = float(bpm)
        if not math.isfinite(value) or value <= 0:
            raise ValueError
        micros = round(60e6 / value)
        if not 1 <= micros <= 0xffffff:
            raise ValueError
        return seconds, micros
    except (ValueError, OverflowError) as exc:
        raise argparse.ArgumentTypeError("Use output-seconds=BPM with a valid MIDI tempo.") from exc


def write_new(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent) as folder:
        staged = Path(folder) / "output"
        staged.write_bytes(data)
        os.link(staged, path)


def curve(args):
    raw = args.midi.read_bytes()
    ppq, tracks = read_midi(raw)
    old = MidiClock(ppq, tracks)
    end = max(track[-1].tick for track in tracks)
    tick = round(old.tick(args.start))
    start_tick = tick
    elapsed = old.seconds(tick)
    tempo = old.tempo(tick)
    changes = []

    def add(at, micros):
        if not changes or changes[-1].data != micros.to_bytes(3, "big"):
            changes.append(Event(at, TEMPO, micros.to_bytes(3, "big")))

    # Smoothstep in microseconds/quarter as a function of beat. Its average
    # over a full segment is the mean of the endpoints, giving the beat span
    # for a requested duration. Integrate it over each quarter-beat step.
    for when, next_tempo in args.to:
        span = round((when - elapsed) * 2e6 * ppq / (tempo + next_tempo))
        if span < 1 or tick + span > end:
            raise ValueError("Targets must advance at least one tick and fit inside the song.")
        add(tick, tempo)
        for offset in range(1, span, max(1, ppq // 4)):
            stop = min(span, offset + max(1, ppq // 4))
            a, b = offset / span, stop / span
            integral = (b**3 - b**4 / 2) - (a**3 - a**4 / 2)
            mean = round(tempo + (next_tempo - tempo) * integral / (b - a))
            add(tick + offset, mean)
            elapsed += (stop - offset) * mean / (ppq * 1e6)
        elapsed += tempo / (ppq * 1e6)
        tick += span
        tempo = next_tempo
    add(tick, tempo)
    # Keep independent track endings, original SMF type, controllers and bends.
    result = bytearray(raw[:14])
    for index, track in enumerate(tracks):
        kept = [e for e in track[:-1] if e.kind != TEMPO or e.tick < start_tick]
        if index == 0:
            kept.extend(changes)
        result.extend(write_midi(ppq, [kept], track[-1].tick)[14:])
    write_new(args.output, result)
    print(args.output)


def wav_info(path):
    with wave.open(str(path), "rb") as source:
        return source.getframerate(), source.getnframes()


def check(args):
    raw = args.midi.read_bytes()
    ppq, tracks = read_midi(raw)
    clock = MidiClock(ppq, tracks)
    report = inspect_bytes(raw)
    duration = clock.seconds(max(track[-1].tick for track in tracks))
    report["duration_seconds"] = duration
    report["markers"] = [
        {"name": e.data.decode(errors="replace"), "seconds": clock.seconds(e.tick)}
        for e in sorted((e for tr in tracks for e in tr if e.kind == 0x106), key=lambda e: e.tick)
    ]
    report["cues"] = []
    for when in args.at:
        if when > duration:
            raise ValueError(f"Cue at {when:g}s is beyond the MIDI ending.")
        tick = clock.tick(when)
        report["cues"].append({"seconds": when, "quarter_note": tick / ppq,
                               "bpm": 60e6 / clock.tempo(tick)})
    if args.wav:
        rate, frames = wav_info(args.wav)
        report["wav_minus_midi_seconds"] = frames / rate - duration
    print(json.dumps(report, indent=2))
    for track in report["tracks"]:
        for channel in track["channels"]:
            if channel["unclosed_notes"] or channel["unmatched_note_offs"]:
                raise ValueError(f"Unbalanced notes in track {track['track']}.")


def frame_map(args):
    old_ppq, old_tracks = read_midi(args.midi.read_bytes())
    ppq, tracks = read_midi(args.revised.read_bytes())
    old, new = MidiClock(old_ppq, old_tracks), MidiClock(ppq, tracks)
    old_end = max(t[-1].tick for t in old_tracks)
    new_end = max(t[-1].tick for t in tracks)
    if old_end * ppq != new_end * old_ppq:
        raise ValueError("The frame map requires the same beat extent in both versions.")
    rate, frames = wav_info(args.wav)
    # Both clocks are linear between the union of their change points.
    beats = {tick / old_ppq for tick in old.ticks}
    beats.update(tick / ppq for tick in new.ticks)
    beats.add(old_end / old_ppq)
    pairs = [(0, 0)]
    for beat in sorted(beats):
        src = round(old.seconds(beat * old_ppq) * rate)
        dst = round(new.seconds(beat * ppq) * rate)
        if 0 < src < frames and (src, dst) != pairs[-1]:
            pairs.append((src, dst))
    audio_tick = min(old.tick(frames / rate), old_end)
    score_frames = round(old.seconds(audio_tick) * rate)
    dest_frames = round(new.seconds(audio_tick * ppq / old_ppq) * rate) + frames - score_frames
    pairs.append((frames, dest_frames))
    if any(x >= a or y >= b for (x, y), (a, b) in zip(pairs, pairs[1:])):
        raise ValueError("The tempo map cannot resolve strictly increasing frames at this sample rate.")
    write_new(args.output, "".join(f"{a} {b}\n" for a, b in pairs).encode())
    print(f"{len(pairs)} frame pairs at {rate} Hz; target duration {dest_frames / rate:.6f}s")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    edit = subs.add_parser("curve", help="Replace the tempo map from an elapsed time onward")
    edit.add_argument("midi", type=Path)
    edit.add_argument("output", type=Path)
    edit.add_argument("--from", dest="start", type=nonnegative, required=True, metavar="SECONDS")
    edit.add_argument("--to", type=target, required=True, action="append", metavar="SECONDS=BPM")
    edit.set_defaults(run=curve)
    audit = subs.add_parser("check", help="Inspect notes, cue tempos, markers and optional PCM WAV duration")
    audit.add_argument("midi", type=Path)
    audit.add_argument("--at", type=nonnegative, action="append", default=[], metavar="SECONDS")
    audit.add_argument("--wav", type=Path)
    audit.set_defaults(run=check)
    warp = subs.add_parser("frame-map", help="Write source/target frame pairs for aligned, pitch-preserving stretch")
    warp.add_argument("midi", type=Path, help="Original MIDI")
    warp.add_argument("revised", type=Path)
    warp.add_argument("wav", type=Path, help="PCM WAV aligned to the original MIDI, starting at frame zero")
    warp.add_argument("output", type=Path)
    warp.set_defaults(run=frame_map)
    args = parser.parse_args()
    try:
        args.run(args)
    except (ValueError, OSError, OverflowError, wave.Error) as exc:
        parser.exit(1, f"{parser.prog}: {exc}\n")


if __name__ == "__main__":
    main()
