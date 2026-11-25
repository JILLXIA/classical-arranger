"""Export a reviewed monophonic SMF melody as YuE2's instrumental events.

All musical times in the JSON are measured in quarter notes. The compiler at
skills/yue2-music/instrumental/scripts/compile_score.py creates the native ABC.
"""
import argparse
import json
import sys
from fractions import Fraction
from pathlib import Path


def json_time(time):
    if time.denominator == 1:
        return time.numerator
    return str(time)


def check_grid(time, description):
    denominator = time.denominator
    if denominator & (denominator - 1):
        raise ValueError(
            f"{description} is {time} quarter notes. YuE2's event compiler needs "
            "a binary grid. Review tuplets or quantize a COPY in Logic/MuseScore."
        )


MAJOR_KEYS = "Cb Gb Db Ab Eb Bb F C G D A E B F# C#".split()
MINOR_KEYS = "Abm Ebm Bbm Fm Cm Gm Dm Am Em Bm F#m C#m G#m D#m A#m".split()


def make_timeline(changes, default, name):
    """Resolve all simultaneous conductor messages and collapse duplicates."""
    declarations = {}
    for tick, value in changes:
        if tick in declarations and value != declarations[tick]:
            raise ValueError(f"Conflicting {name} at tick {tick}.")
        declarations[tick] = value
    declarations.setdefault(0, default)
    result = {}
    for tick, value in sorted(declarations.items()):
        if not result or value != previous:
            result[tick] = value
        previous = value
    return result


def integer_bpm(tempos, override):
    if len(tempos) != 1:
        raise ValueError(
            "There are tempo changes in this MIDI. Export a constant-tempo "
            "section for YuE2. Your full MIDI arrangement can keep its tempo map."
        )
    microseconds = tempos[0]
    if microseconds < 1:
        raise ValueError("Invalid MIDI tempo (must be positive).")
    if override is not None:
        if override < 1:
            raise ValueError("--bpm must be positive.")
        return override
    guess = round(Fraction(60_000_000, microseconds))
    if guess < 1 or abs(Fraction(60_000_000, guess) - microseconds) > 1:
        raise ValueError(
            f"Tempo is {60_000_000 / microseconds:.5f} BPM. YuE2 needs an "
            "integer quarter-note BPM. Choose --bpm explicitly for this export."
        )
    return guess


def make_bars(end, meters, keys, pickup):
    offset = Fraction(0)
    result, padding = [], Fraction(0)
    changes = sorted(set(meters) | set(keys) | {end})
    next_change = 0
    current_meter = meters[0]
    last_written_meter = None
    while offset < end:
        current_meter = meters.get(offset, current_meter)
        numerator, denominator = current_meter
        ordinary = Fraction(4 * numerator, denominator)
        if not ordinary > 0:
            raise ValueError("Time signature must have positive bar duration.")
        if offset == 0 and pickup is not None:
            check_grid(pickup, "Pickup")
            if not 0 < pickup < ordinary:
                raise ValueError("--pickup must be shorter than one full bar.")
            length = pickup
            written = length / 4
            label = f"{written.numerator}/{written.denominator}"
        else:
            length = ordinary
            label = f"{numerator}/{denominator}"
        to = offset + length
        while next_change < len(changes) and changes[next_change] <= offset:
            next_change += 1
        if next_change < len(changes):
            at = changes[next_change]
            if at != end and at < to:
                raise ValueError(
                    f"Key/meter change at quarter {at} is inside a bar. Check "
                    "--pickup or export shorter sections with changes on downbeats."
                )
        entry = {}
        if label != last_written_meter:
            entry["meter"] = label
        if offset == 0:
            entry["section"] = "verse"  # Section label used by the official events schema.
        elif offset in keys:
            entry["key"] = keys[offset]
        result.append(entry)
        last_written_meter = label
        offset = to
        # Protect memory if a corrupted SMF declares a huge end-of-track delta.
        if len(result) > 20_000:
            raise ValueError("More than 20,000 bars. Export a shorter excerpt.")
    if offset > end:
        padding = offset - end
    return result, padding


def build_events(notes, ppq, end_tick, tempo_changes, meter_changes, key_changes,
                 key_override=None, bpm_override=None, pickup=None):
    """notes contain absolute (start_tick, end_tick, MIDI pitch) triples."""
    if not notes:
        raise ValueError("The selected track/channel has no pitched MIDI notes.")
    end_tick = max(end_tick, max(end for _, end, _ in notes))
    tempos = make_timeline([(t, v) for t, v in tempo_changes if t < end_tick],
                           500_000, "tempo")
    meters = make_timeline([(t, v) for t, v in meter_changes if t < end_tick],
                           (4, 4), "meter")
    keys = make_timeline([(t, v) for t, v in key_changes if t < end_tick],
                         None, "key")
    if key_override is not None:
        if key_override not in MAJOR_KEYS + MINOR_KEYS:
            raise ValueError("Use a MIDI major/minor key such as C, Am, Bb or F#m.")
        if keys[0] is not None and keys[0] != key_override:
            raise ValueError(f"MIDI declares {keys[0]}. Remove --key or use that key.")
        keys[0] = key_override
    elif keys[0] is None:
        raise ValueError("No opening key signature. Pass a reviewed key, e.g. --key Am.")
    bpm = integer_bpm(tempos, bpm_override)
    melody, previous_end = [], 0
    for start, end, pitch in sorted(notes):
        if end <= start:
            raise ValueError(f"Zero or negative note duration at tick {start}.")
        if start < previous_end:
            raise ValueError(
                f"Overlapping notes at tick {start}. Isolate the main melody in "
                "Logic/MuseScore (or choose --channel) before exporting."
            )
        onset = Fraction(start, ppq)
        duration = Fraction(end - start, ppq)
        check_grid(onset, f"Onset at tick {start}")
        check_grid(duration, f"Duration at tick {start}")
        melody.append([json_time(onset), json_time(duration), pitch])
        previous_end = end
    meters = {Fraction(t, ppq): v for t, v in meters.items()}
    keys = {Fraction(t, ppq): v for t, v in keys.items()}
    bars, padding = make_bars(Fraction(end_tick, ppq), meters, keys, pickup)
    return {"bpm": bpm, "key": keys[0], "bars": bars, "notes": melody, "chords": []}, padding


def extract_notes(events):
    """Use note-on and note-off times. MIDI pedal/dynamics belong to the full MIDI."""
    from collections import deque
    active = {}
    result = []
    for event in events:
        if event.kind >> 4 not in (8, 9):
            continue
        pitch, velocity = event.data
        pending = active.setdefault(pitch, deque())
        if event.kind >> 4 == 9 and velocity > 0:
            pending.append(event.tick)
        elif not pending:
            raise ValueError(f"Unmatched note-off: pitch {pitch} at tick {event.tick}.")
        else:
            result.append((pending.popleft(), event.tick, pitch))
    if any(active.values()):
        raise ValueError("A melody note has no note-off message. Repair the MIDI first.")
    return result


def metadata(tracks):
    tempos, meters, keys = [], [], []
    for track in tracks:
        for event in track:
            data = event.data
            if event.kind == 0x151:
                if len(data) != 3:
                    raise ValueError("Malformed MIDI tempo.")
                tempos.append((event.tick, int.from_bytes(data, "big")))
            elif event.kind == 0x158:
                if len(data) != 4 or not data[0]:
                    raise ValueError("Malformed MIDI meter.")
                meters.append((event.tick, (data[0], 2 ** data[1])))
            elif event.kind == 0x159:
                if len(data) != 2 or data[1] not in (0, 1):
                    raise ValueError("Malformed MIDI key signature.")
                fifths = int.from_bytes(data[:1], "big", signed=True)
                if not -7 <= fifths <= 7:
                    raise ValueError("Invalid MIDI key signature.")
                names = MINOR_KEYS if data[1] else MAJOR_KEYS
                keys.append((event.tick, names[fifths + 7]))
    return tempos, meters, keys


def to_events(data: bytes, track_index: int, channel: int, chords: list[str],
              key: str | None = None, *, bpm: int | None = None,
              pickup: Fraction | None = None) -> dict:
    """Build the official event schema, using zero-based track and one-based channel.

    Supply chords from the same plan passed to arranger.arrange_bytes, or [].
    End-of-track is preserved for the selected track. Its last partial bar is padded
    with silence to the notated barline. The default first downbeat is tick zero.
    A shorter opening measure may be specified explicitly using pickup in quarters.
    """
    from arranger import chord_notes, read_midi, selected_events
    import warnings

    ppq, tracks = read_midi(data)
    if not 0 <= track_index < len(tracks) or not 1 <= channel <= 16 or channel == 10:
        raise ValueError("Choose a valid zero-based track and a melodic channel 1–16 (except 10).")
    track = tracks[track_index]
    notes = extract_notes(selected_events(track, channel))
    end_tick = track[-1].tick
    tempos, meters, keys = metadata(tracks)
    result, padding = build_events(notes, ppq, end_tick, tempos, meters, keys,
                                   key, bpm, pickup)
    if padding:
        warnings.warn(f"Last bar padded with {padding} quarter notes of silence.", stacklevel=2)
    if chords:
        if len(chords) != len(result["bars"]):
            raise ValueError(f"Expected one chord per bar ({len(result['bars'])}); "
                             f"you supplied {len(chords)}. Omit chords to re-harmonize.")
        cursor = Fraction(0)
        for symbol, bar in zip(chords, result["bars"]):
            if not isinstance(symbol, str):
                raise ValueError("Each chord must be a string, e.g. Am or E7.")
            symbol = symbol.strip()
            chord_notes(symbol)
            if symbol.endswith("9"):
                raise ValueError(
                    f"YuE2 ABC does not encode {symbol!r}. Keep the ninth in the MIDI; "
                    "for YuE2 supply a seventh chord explicitly or leave chords empty."
                )
            if "meter" in bar:
                meter = Fraction(bar["meter"])
            if not result["chords"] or symbol != result["chords"][-1][1]:
                result["chords"].append([json_time(cursor), symbol])
            cursor += 4 * meter
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--track", type=int, required=True, help="Zero-based melody track index")
    parser.add_argument("--channel", type=int, required=True, help="One-based melody channel")
    parser.add_argument("--key", help="Initial key if absent in MIDI, e.g. Am or Eb")
    parser.add_argument("--bpm", type=int, help="Explicit integer BPM for constant-tempo sources")
    parser.add_argument("--pickup", type=Fraction, help="Opening anacrusis in quarter notes, e.g. 1/2")
    parser.add_argument("--chords", help="One chord per bar separated by commas; optional")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    try:
        chords = args.chords.split(",") if args.chords else []
        events = to_events(args.input.read_bytes(), args.track, args.channel,
                           chords, args.key, bpm=args.bpm, pickup=args.pickup)
        encoded = json.dumps(events, ensure_ascii=False, indent=2) + "\n"
        with args.out.open("x", encoding="utf-8") as output:
            output.write(encoded)
        print(f"{args.out}: {len(events['notes'])} melody notes, {len(events['bars'])} bars, "
              f"{events['bpm']} BPM, {events['key']}.")
        return 0
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
