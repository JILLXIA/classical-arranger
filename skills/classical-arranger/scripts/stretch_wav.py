"""Apply a source/target sample-frame map to aligned PCM WAV locally (NumPy and SciPy)."""
import argparse
import os
from pathlib import Path
import sys
import tempfile
import wave

import numpy as np
from scipy import signal
from scipy.io import wavfile


def load_map(path, frames):
    pairs = np.loadtxt(path, dtype=np.int64, ndmin=2)
    if pairs.shape[1] != 2 or len(pairs) < 2:
        raise ValueError("Expected at least two source/target frame pairs.")
    if np.any(pairs[0] != 0) or np.any(np.diff(pairs, axis=0) <= 0) or pairs[-1, 0] != frames:
        raise ValueError("Map must start at 0 0, increase on both axes and end at the input frame count.")
    return pairs.T


def stretch(source, rate, before, after):
    # All channels use the same grain placement to keep stereo attacks together.
    different = np.flatnonzero(before != after)
    if not len(different):
        return source
    first = before[different[0] - 1]
    hop = max(4, round(.018 * rate))
    span, radius = 3 * hop, max(1, round(.011 * rate))
    start = max(0, first - span)
    audio = np.pad(source, ((radius, span + radius), (0, 0)))
    guide = audio.mean(axis=1)
    guide[1:] = guide[1:] - .74 * guide[:-1]
    window = np.hanning(span + 1)[:-1].astype("float32")
    out = np.zeros((after[-1] + span, source.shape[1]), dtype="float32")
    weights = np.zeros(len(out), dtype="float32")
    previous = None
    for at in range(start, after[-1], hop):
        goal = round(np.interp(at, after, before)) + radius
        chosen = goal
        if previous is not None and at >= first:
            overlap = span - hop
            ref = guide[previous + hop:previous + span]
            area = guide[goal - radius:goal + radius + overlap]
            correlation = signal.correlate(area, ref, mode="valid", method="fft")
            power = np.r_[0., np.cumsum(area * area, dtype=np.float64)]
            norm = np.sqrt(np.maximum(1.e-15, (power[overlap:] - power[:-overlap]) * (ref @ ref)))
            offsets = np.arange(-radius, radius + 1)
            chosen += offsets[np.argmax(correlation / norm - .05 * (offsets / radius) ** 2)]
        out[at:at + span] += audio[chosen:chosen + span] * window[:, None]
        weights[at:at + span] += window
        previous = chosen
    out = out[:after[-1]]
    out /= np.maximum(weights[:len(out), None], 1.e-8)
    out[:first] = source[:first]
    out[0] = source[0]
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path)
    parser.add_argument("frame_map", type=Path, help="Output of midi_timing.py frame-map for this WAV")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        with wave.open(str(args.wav), "rb") as audio:
            rate, width, channels = audio.getframerate(), audio.getsampwidth(), audio.getnchannels()
            before, after = load_map(args.frame_map, audio.getnframes())
        if width not in (2, 3):
            raise ValueError("Use a 16-bit or 24-bit PCM WAV.")
        _, pcm = wavfile.read(args.wav)
        # SciPy reads 24-bit PCM left-justified in int32; float32 holds its 24 significant bits.
        source = pcm.reshape(-1, channels).astype("float32") / float(1 << (pcm.dtype.itemsize * 8 - 1))
        out = stretch(source, rate, before, after)
        scale = 1 << (width * 8 - 1)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=args.output.parent) as folder:
            staged = Path(folder) / "mix.wav"
            with wave.open(str(staged), "wb") as output:
                output.setparams((channels, width, rate, 0, "NONE", "not compressed"))
                for at in range(0, len(out), rate):
                    chunk = np.rint(out[at:at + rate] * scale).clip(-scale, scale - 1)
                    if width == 2:
                        packed = chunk.astype("<i2").tobytes()
                    else:
                        packed = chunk.astype("<i4").view("u1").reshape(-1, 4)[:, :3].tobytes()
                    output.writeframesraw(packed)
            os.link(staged, args.output)
    except (ValueError, OSError, wave.Error) as exc:
        parser.exit(1, f"{parser.prog}: {exc}\n")
    print(f"{args.output} ({len(out) / rate:.6f}s)")


if __name__ == "__main__":
    main()
