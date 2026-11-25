"""An offline, deterministic General MIDI accompaniment generator for Python 3.10+."""
from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


RESULT_DIR = Path(__file__).resolve().parent / "result"


def default_midi_path(source: Path) -> Path:
    return RESULT_DIR / f"{source.stem}-arranged-{datetime.now():%Y%m%d-%H%M%S-%f}.mid"


@dataclass(frozen=True)
class Event:
    tick: int
    # Channel events have kind 0x80..0xEF including channel. Meta type is 0x100 + type.
    kind: int
    data: bytes


def read_vlq(data: bytes, pos: int) -> tuple[int, int]:
    number = 0
    for _ in range(4):
        if pos >= len(data):
            raise ValueError("Incomplete MIDI variable-length value.")
        value = data[pos]
        pos += 1
        number = (number << 7) | (value & 127)
        if value < 128:
            return number, pos
    raise ValueError("Invalid MIDI variable-length value.")


def vlq(number: int) -> bytes:
    if not 0 <= number <= 0x0FFFFFFF:
        raise ValueError("Invalid MIDI time delta or payload length.")
    result = [number & 127]
    while number > 127:
        number >>= 7
        result.append(128 | (number & 127))
    return bytes(reversed(result))


def parse_track(data: bytes) -> list[Event]:
    events = []
    pos = tick = running = 0
    while pos < len(data):
        delta, pos = read_vlq(data, pos)
        tick += delta
        if pos >= len(data):
            raise ValueError("Missing MIDI event.")
        status = data[pos]
        if status >= 0x80:
            pos += 1
        elif running:
            status = running
        else:
            raise ValueError("Missing running status.")
        if 0x80 <= status <= 0xEF:
            running = status
            size = 1 if status >> 4 in (0xC, 0xD) else 2
            payload = data[pos:pos + size]
            if len(payload) != size or any(b >= 128 for b in payload):
                raise ValueError("Malformed MIDI channel event.")
            events.append(Event(tick, status, payload))
            pos += size
        elif status == 0xFF:
            running = 0
            if pos >= len(data):
                raise ValueError("Missing meta-event type.")
            meta = data[pos]
            size, pos = read_vlq(data, pos + 1)
            if pos + size > len(data):
                raise ValueError("Truncated MIDI meta-event.")
            events.append(Event(tick, 0x100 + meta, data[pos:pos + size]))
            pos += size
            if meta == 0x2F:
                if size != 0 or pos != len(data):
                    raise ValueError("Malformed end of MIDI track.")
                return events
        elif status in (0xF0, 0xF7):
            raise ValueError("This prototype only handles standard GM files without SysEx. Please export a plain MIDI clip.")
        else:
            raise ValueError("Unsupported MIDI system event.")
    raise ValueError("MIDI track has no end marker.")


def read_midi(data: bytes) -> tuple[int, list[list[Event]]]:
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("Use a MIDI excerpt under 8 MiB.")
    if len(data) < 14 or data[:4] != b"MThd":
        raise ValueError("Expected a Standard MIDI File (.mid).")
    header, fmt, count, ppq = struct.unpack(">IHHH", data[4:14])
    if header != 6 or fmt not in (0, 1) or not count or (fmt == 0 and count != 1):
        raise ValueError("Only standard type 0/1 MIDI files are supported.")
    if ppq < 24 or ppq >= 0x8000:
        raise ValueError("Use beat-based MIDI with at least 24 ticks per quarter note.")
    tracks = []
    pos = 14
    for _ in range(count):
        if data[pos:pos + 4] != b"MTrk" or len(data) < pos + 8:
            raise ValueError("Missing MIDI track chunk.")
        size = int.from_bytes(data[pos + 4:pos + 8], "big")
        pos += 8
        if size > len(data) - pos:
            raise ValueError("Truncated MIDI track.")
        tracks.append(parse_track(data[pos:pos + size]))
        pos += size
    if pos != len(data):
        raise ValueError("Unexpected data after the final MIDI track.")
    return ppq, tracks


def write_midi(ppq: int, tracks: list[list[Event]], end_tick: int) -> bytes:
    result = bytearray(b"MThd" + struct.pack(">IHHH", 6, 1, len(tracks), ppq))
    for track in tracks:
        payload = bytearray()
        prev = 0
        for event in sorted(track, key=lambda e: e.tick):
            payload.extend(vlq(event.tick - prev))
            prev = event.tick
            if event.kind >= 0x100:
                payload.extend([0xFF, event.kind - 0x100])
                payload.extend(vlq(len(event.data)))
            else:
                payload.append(event.kind)
            payload.extend(event.data)
        payload.extend(vlq(max(end_tick, prev) - prev) + b"\xff\x2f\x00")
        result.extend(b"MTrk" + len(payload).to_bytes(4, "big") + payload)
    return bytes(result)


def label(name: str) -> Event:
    return Event(0, 0x103, name.encode("utf-8"))


def selected_events(track: list[Event], channel: int) -> list[Event]:
    return [e for e in track if 0x80 <= e.kind < 0xF0 and (e.kind & 15) + 1 == channel]


def note_stats(events: list[Event]) -> dict:
    active = Counter()
    count = peak = unmatched_off = last_note_tick = 0
    for event in events:
        if event.kind >> 4 not in (8, 9):
            continue
        pitch, velocity = event.data
        last_note_tick = event.tick
        if event.kind >> 4 == 9 and velocity > 0:
            count += 1
            active[pitch] += 1
            peak = max(peak, active.total())
        elif active[pitch] > 0:
            active[pitch] -= 1
        else:
            unmatched_off += 1
    return {"notes": count, "max_simultaneous_keys": peak, "last_note_tick": last_note_tick,
            "unclosed_notes": active.total(), "unmatched_note_offs": unmatched_off}


def arrangement_bar_count(last_note_tick: int, ticks_per_bar: int) -> int:
    return (last_note_tick + ticks_per_bar - 1) // ticks_per_bar


def inspect_bytes(data: bytes, *, include_arrangement_bars: bool = False) -> dict:
    ppq, tracks = read_midi(data)
    result = []
    for index, track in enumerate(tracks):
        name = next((e.data.decode("utf-8", "replace") for e in track if e.kind == 0x103), "")
        channels = sorted({(e.kind & 15) + 1 for e in track if 0x80 <= e.kind < 0xF0})
        stats = [{"channel": c, **note_stats(selected_events(track, c))} for c in channels]
        result.append({"track": index, "name": name, "channels": stats})
    report = {"ticks_per_quarter": ppq, "tracks": result}
    if include_arrangement_bars:
        try:
            _, meter = conductor_events(tracks)
        except ValueError as exc:
            report["arrangement_meter_error"] = str(exc)
        else:
            for track_info in result:
                for channel in track_info["channels"]:
                    channel["arrangement_bars"] = arrangement_bar_count(channel["last_note_tick"], ppq * meter)
    return report


def conductor_events(tracks: list[list[Event]]) -> tuple[list[Event], int]:
    # Preserve the global tempo, time signatures and key signatures without repeating duplicates.
    signatures = {}
    for track in tracks:
        for event in track:
            if event.kind not in (0x151, 0x158, 0x159):
                continue
            expected = {0x151: 3, 0x158: 4, 0x159: 2}[event.kind]
            if len(event.data) != expected:
                raise ValueError("Malformed tempo, time signature or key signature.")
            if event.kind == 0x151 and int.from_bytes(event.data, "big") == 0:
                raise ValueError("Invalid zero MIDI tempo.")
            key = (event.tick, event.kind)
            if key in signatures and signatures[key] != event:
                raise ValueError("Conflicting global metadata at the same tick.")
            signatures[key] = event
    meters = {tick: (e.data[0], e.data[1]) for (tick, kind), e in signatures.items() if kind == 0x158}
    meter = meters.get(0, (4, 2))
    if meter not in {(2, 2), (3, 2), (4, 2)} or any(m != meter for m in meters.values()):
        raise ValueError("This prototype requires one time signature throughout: 2/4, 3/4 or 4/4.")
    return [label("Conductor"), *signatures.values()], meter[0]


QUALITIES = {"": (0, 4, 7), "m": (0, 3, 7), "7": (0, 4, 7, 10),
             "m7": (0, 3, 7, 10), "maj7": (0, 4, 7, 11), "dim": (0, 3, 6),
             "9": (0, 4, 7, 10, 14), "maj9": (0, 4, 7, 11, 14), "m9": (0, 3, 7, 10, 14)}
PITCH = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def chord_notes(name: str) -> list[int]:
    match = re.fullmatch(r"([A-G])([#b]?)(maj9|m9|9|maj7|m7|7|dim|m)?", name.strip())
    if not match:
        raise ValueError(f"Unsupported chord {name!r}. Try Am, F, Bb, C#7, Gmaj7, Dm7, Bdim, Am9, Cmaj9 or G9.")
    letter, accidental, quality = match.groups()
    root = (PITCH[letter] + {"#": 1, "b": -1, "": 0}[accidental]) % 12
    return [root + interval for interval in QUALITIES[quality or ""]]


def note(track: list[Event], channel: int, pitch: int, start: int, length: int, velocity: int) -> None:
    track.append(Event(start, 0x90 | channel, bytes([pitch, velocity])))
    track.append(Event(start + length, 0x80 | channel, bytes([pitch, 0])))


def rhodes_voicing(chord: list[int], previous: list[int]) -> list[int]:
    # Bass supplies the root in extended chords; keep the third, seventh and ninth audible.
    tones = chord[1:] if len(chord) == 5 else chord
    pcs = sorted({tone % 12 for tone in tones})
    voicings = []
    for bottom in range(50, 68):
        if bottom % 12 not in pcs:
            continue
        voiced = sorted(bottom + (pc - bottom) % 12 for pc in pcs)
        if voiced[-1] <= 77:
            voicings.append(voiced)
    def movement(notes: list[int]) -> float:
        if not previous:
            return abs(sum(notes) / len(notes) - 61)
        return (sum(min(abs(n - old) for old in previous) for n in notes)
                + abs(notes[-1] - previous[-1]) + 0.5 * abs(notes[0] - previous[0]))
    return min(voicings, key=movement)


def neo_soul_bar(bass: list[Event], keys: list[Event], drums: list[Event], bass_ch: int,
                 keys_ch: int, ppq: int, meter: int, bar: int, chord: list[int],
                 voicing: list[int]) -> None:
    # Delay the backbeat slightly; keep the source melody on its original clock.
    start = bar * meter * ppq
    for beat in range(meter):
        t = start + beat * ppq
        note(drums, 9, 42, t, ppq // 8, 64 if beat % 2 == 0 else 58)
        note(drums, 9, 42, t + 9 * ppq // 16, ppq // 8, 48)
        if beat % 2 == 0:
            note(drums, 9, 36, t, ppq // 8, 94)
            note(bass, bass_ch, 36 + chord[0], t, ppq * 5 // 8, 83)
            onset, length = t + ppq // 4, ppq * 13 // 16
        else:
            note(drums, 9, 38, t + ppq // 24, ppq // 8, 82)
            note(drums, 9, 36, t + 3 * ppq // 4, ppq // 8, 69)
            note(bass, bass_ch, 36 + chord[2], t + ppq // 2, ppq * 3 // 8, 66)
            onset, length = t + 3 * ppq // 4, ppq // 5
        length = min(length, start + meter * ppq - onset)
        for index, pitch in enumerate(voicing):
            # Small voicing spreads are applied only to generated notes.
            offset = index * max(1, ppq // 128)
            note(keys, keys_ch, pitch, onset + offset, max(1, length - offset), 57 + index * 3)


def arrangement_tracks(ppq: int, meter: int, harmony: list[list[int]], style: str,
                       lead_channel: int) -> list[list[Event]]:
    if style not in ("funk", "swing", "neo_soul"):
        raise ValueError("Styles supported: funk, swing, neo_soul.")
    channels = [c for c in range(16) if c not in (lead_channel, 9)]
    bass_channel, keys_channel = channels[:2]
    bass_patch, keys_patch = (32, 0) if style == "swing" else (33, 4)
    bass = [label("Upright Bass" if style == "swing" else "Electric Bass"),
            Event(0, 0xC0 | bass_channel, bytes([bass_patch]))]
    keys = [label("Jazz Piano" if style == "swing" else "Electric Piano"),
            Event(0, 0xC0 | keys_channel, bytes([keys_patch]))]
    drums = [label("Drum Kit")]
    voicing = []
    for bar, chord in enumerate(harmony):
        if style == "neo_soul":
            voicing = rhodes_voicing(chord, voicing)
            neo_soul_bar(bass, keys, drums, bass_channel, keys_channel, ppq, meter, bar, chord, voicing)
            continue
        for beat in range(meter):
            start = (bar * meter + beat) * ppq
            if style == "funk":
                note(bass, bass_channel, 36 + chord[0 if beat % 2 == 0 else 2], start, ppq // 2, 93)
                if beat == meter - 1:
                    note(bass, bass_channel, 48 + chord[0], start + 3 * ppq // 4, ppq // 6, 74)
                for offset, velocity in ((0, 84), (ppq // 2, 62)):
                    note(drums, 9, 42, start + offset, ppq // 8, velocity)
                note(drums, 9, 36 if beat % 2 == 0 else 38, start, ppq // 8, 105)
                keys_offset, keys_length = ppq // 2, ppq // 4
            else:
                # A quarter-note walking bass and a swung ride pickup mark the swing feel.
                note(bass, bass_channel, 36 + chord[beat % len(chord)], start, ppq * 7 // 8, 85)
                note(drums, 9, 51, start, ppq // 6, 85)
                if beat % 2 == 1:
                    note(drums, 9, 51, start + 2 * ppq // 3, ppq // 8, 66)
                    note(drums, 9, 44, start, ppq // 8, 76)
                keys_offset = 0 if beat % 2 == 0 else 2 * ppq // 3
                keys_length = ppq // 2 if beat % 2 == 0 else ppq // 4
            for pitch in chord:
                note(keys, keys_channel, 60 + pitch, start + keys_offset, keys_length, 68)
    return [bass, keys, drums]


def arrange_bytes(midi_data: bytes, track_index: int, channel: int, chords: list[str], style: str) -> bytes:
    """Preserve one chosen MIDI track/channel, add bass/keys/drums using one chord per bar."""
    ppq, tracks = read_midi(midi_data)
    if not 0 <= track_index < len(tracks) or not 1 <= channel <= 16 or channel == 10:
        raise ValueError("Choose a valid zero-based track and melodic channel 1-16 (except 10).")
    lead = selected_events(tracks[track_index], channel)
    stats = note_stats(lead)
    if not stats["notes"] or stats["unclosed_notes"] or stats["unmatched_note_offs"]:
        raise ValueError("Selected track/channel must have notes and all note-ons/offs must match.")
    conductor, meter = conductor_events(tracks)
    ticks_per_bar = ppq * meter
    measures = arrangement_bar_count(stats["last_note_tick"], ticks_per_bar)
    if not 1 <= measures <= 512 or len(chords) != measures:
        raise ValueError(f"Supply exactly {measures} chords, one per bar from tick 0 (limit 512 bars).")
    harmony = [chord_notes(c) for c in chords]
    tracks_out = [conductor, [label("Source Melody"), *lead],
                  *arrangement_tracks(ppq, meter, harmony, style, channel - 1)]
    return write_midi(ppq, tracks_out, len(harmony) * ticks_per_bar)


def demo_bytes() -> bytes:
    """Original four-bar exercise, 2/4 at 112 BPM, Am E7 Dm Am."""
    ppq = 480
    conductor = [label("Conductor"), Event(0, 0x158, bytes([2, 2, 24, 8])),
                 Event(0, 0x151, (535714).to_bytes(3, "big")), Event(0, 0x159, bytes([0, 1]))]
    lead = [label("Demo Melody"), Event(0, 0xC0, bytes([0]))]
    for index, pitch in enumerate([69, 72, 76, 72, 71, 68, 64, 71, 74, 77, 76, 74, 72, 71, 69, 69]):
        note(lead, 0, pitch, index * 240, 180, 85 if index % 4 == 0 else 72)
    return write_midi(ppq, [conductor, lead], 3840)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    demo = commands.add_parser("demo", help="Create the built-in four-bar original melody")
    demo.add_argument("--out", required=True, type=Path)
    info = commands.add_parser("inspect", help="List tracks and one-based MIDI channels")
    info.add_argument("input", type=Path)
    arrange = commands.add_parser("arrange", help="Add one chord per bar from the first downbeat")
    arrange.add_argument("input", type=Path)
    arrange.add_argument("--track", required=True, type=int, help="Zero-based track number")
    arrange.add_argument("--channel", required=True, type=int, help="One-based MIDI channel, 1-16")
    arrange.add_argument("--chords", required=True, help="Comma-separated, one per bar, e.g. Am,E7,Dm,Am")
    arrange.add_argument("--style", choices=["funk", "swing", "neo_soul"], required=True)
    arrange.add_argument("--out", type=Path, help="Destination MIDI (default: project result/ directory)")
    args = parser.parse_args()
    try:
        if args.operation == "inspect":
            print(json.dumps(inspect_bytes(args.input.read_bytes()), indent=2))
            return 0
        if args.operation == "demo":
            result = demo_bytes()
        else:
            result = arrange_bytes(args.input.read_bytes(), args.track, args.channel,
                                   args.chords.split(","), args.style)
            if args.out is None:
                args.out = default_midi_path(args.input)
                args.out.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation protects existing arrangements and the original file.
        with args.out.open("xb") as output:
            output.write(result)
        print(args.out)
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
