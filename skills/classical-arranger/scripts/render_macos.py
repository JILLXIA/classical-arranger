"""Render GM MIDI or a selected track to stereo PCM WAV using the macOS DLS synth."""
import argparse
from array import array
import ctypes as C
from itertools import chain
import math
import os
from pathlib import Path
import sys
import tempfile
import wave

from midi_timing import MidiClock, read_midi

RATE = 44100
BLOCK = 512
U32 = C.c_uint32
PTR = C.c_void_p


class Component(C.Structure):
    _fields_ = [(name, U32) for name in ("type", "subtype", "manufacturer", "flags", "mask")]


class AudioFormat(C.Structure):
    _fields_ = [("rate", C.c_double)] + [(name, U32) for name in (
        "format", "flags", "bytes_per_packet", "frames_per_packet",
        "bytes_per_frame", "channels", "bits", "reserved")]


class Buffer(C.Structure):
    _fields_ = [("channels", U32), ("size", U32), ("data", PTR)]


class StereoBuffers(C.Structure):
    _fields_ = [("count", U32), ("buffers", Buffer * 2)]


class Timestamp(C.Structure):
    _fields_ = [("sample", C.c_double), ("host", C.c_uint64), ("scalar", C.c_double),
                ("word", C.c_uint64), ("smpte", C.c_byte * 24), ("flags", U32), ("reserved", U32)]


def fourcc(value):
    return int.from_bytes(value.encode("ascii"), "big")


def checked(status, operation):
    if status != 0:
        raise ValueError(f"{operation} returned Core Audio status {status}.")


def render(args, destination):
    if sys.platform != "darwin":
        raise ValueError("This renderer needs the macOS AudioToolbox framework.")
    ppq, tracks = read_midi(args.midi.read_bytes())
    clock = MidiClock(ppq, tracks)
    selected = tracks
    if args.track is not None:
        if not 0 <= args.track < len(tracks):
            raise ValueError(f"Track index must be in 0..{len(tracks) - 1}.")
        selected = [tracks[args.track]]
    events = sorted((e for e in chain.from_iterable(selected) if e.kind < 256), key=lambda e: e.tick)
    frames = [round(clock.seconds(e.tick) * RATE) for e in events]
    end = max(t[-1].tick for t in tracks)
    total = round((clock.seconds(end) + args.tail) * RATE)
    lib = C.CDLL("/System/Library/Frameworks/AudioToolbox.framework/AudioToolbox")

    def bind(name, result, *params):
        function = getattr(lib, name)
        function.restype, function.argtypes = result, list(params)
        return function

    find = bind("AudioComponentFindNext", PTR, PTR, PTR)
    create = bind("AudioComponentInstanceNew", C.c_int, PTR, PTR)
    dispose = bind("AudioComponentInstanceDispose", C.c_int, PTR)
    set_property = bind("AudioUnitSetProperty", C.c_int, PTR, U32, U32, U32, PTR, U32)
    initialize = bind("AudioUnitInitialize", C.c_int, PTR)
    uninitialize = bind("AudioUnitUninitialize", C.c_int, PTR)
    send = bind("MusicDeviceMIDIEvent", C.c_int, PTR, U32, U32, U32, U32)
    process = bind("AudioUnitRender", C.c_int, PTR, PTR, PTR, U32, U32, PTR)
    description = Component(fourcc("aumu"), fourcc("dls "), fourcc("appl"), 0, 0)
    component = find(None, C.byref(description))
    if not component:
        raise ValueError("macOS DLS Music Device is unavailable in this process.")
    unit = PTR()
    checked(create(component, C.byref(unit)), "Create DLS synth")
    initialized = False
    peak = 0.
    try:
        # Output scope, stream format property: native float, packed, noninterleaved.
        fmt = AudioFormat(RATE, fourcc("lpcm"), 41, 4, 1, 4, 2, 32, 0)
        checked(set_property(unit, 8, 2, 0, C.byref(fmt), C.sizeof(fmt)), "Set output format")
        checked(initialize(unit), "Initialize synth")
        initialized = True
        channels = [(C.c_float * BLOCK)() for _ in range(2)]
        index = 0
        with wave.open(str(destination), "wb") as output:
            output.setparams((2, 2, RATE, 0, "NONE", "not compressed"))
            for start in range(0, total, BLOCK):
                size = min(BLOCK, total - start)
                while index < len(events) and frames[index] < start + size:
                    e = events[index]
                    second = e.data[1] if len(e.data) == 2 else 0
                    checked(send(unit, e.kind, e.data[0], second, frames[index] - start), "Send MIDI event")
                    index += 1
                buffers = StereoBuffers(2, (Buffer * 2)(*(Buffer(1, size * 4, C.addressof(ch)) for ch in channels)))
                stamp = Timestamp(sample=start, flags=1)
                flags = U32()
                checked(process(unit, C.byref(flags), C.byref(stamp), 0, size, C.byref(buffers)), "Render block")
                samples = [value * args.gain for pair in zip(channels[0][:size], channels[1][:size]) for value in pair]
                block_peak = max(map(abs, samples))
                if not math.isfinite(block_peak) or block_peak > 1:
                    raise ValueError("Output exceeds PCM range. Lower --gain and use that gain for related stems.")
                peak = max(peak, block_peak)
                pcm = array("h", (round(sample * 32767) for sample in samples))
                if sys.byteorder != "little":
                    pcm.byteswap()
                output.writeframesraw(pcm.tobytes())
    finally:
        if initialized:
            uninitialize(unit)
        dispose(unit)
    if peak < 1 / 32767:
        raise ValueError("No audible signal. Check the selected track and its MIDI channel setup.")
    return total / RATE, peak


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("midi", type=Path)
    cli.add_argument("wav", type=Path)
    cli.add_argument("--track", type=int, help="Zero-based self-contained instrument track; keep full-song timing")
    cli.add_argument("--tail", type=float, default=1.5, help="Release tail in seconds (default 1.5)")
    cli.add_argument("--gain", type=float, default=1., help="Fixed linear gain for all rendered tracks (default 1)")
    args = cli.parse_args()
    if not math.isfinite(args.tail) or args.tail < 0 or not math.isfinite(args.gain) or args.gain <= 0:
        cli.error("Use a finite tail >= 0 and gain > 0.")
    try:
        args.wav.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=args.wav.parent) as folder:
            staged = Path(folder) / "preview.wav"
            duration, peak = render(args, staged)
            os.link(staged, args.wav)
    except (ValueError, OSError, wave.Error) as exc:
        cli.exit(1, f"{cli.prog}: {exc}\n")
    print(f"{args.wav} ({duration:.3f}s, peak {20 * math.log10(peak):.2f} dBFS)")


if __name__ == "__main__":
    main()
