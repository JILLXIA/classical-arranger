---
name: classical-arranger
description: Run Classical Arranger to turn a WAV and an arrangement prompt into editable instrument tracks for a DAW. Also use for the project's local MIDI presets, WAV previews, and YuE2 reference recordings.
---

# Run Classical Arranger

Run commands from the checkout that contains `wav2midi.py` and `arranger.py`. Save final deliverables in the project's `result/` directory unless the user specifies a different location. Choose the workflow by the source file and the artifact the musician wants.

## WAV and prompt to editable MIDI

Use `wav2midi.py` for this request. It takes the WAV and prompt directly. YuE2 generates a performance, SheetSage2 transcribes its melody, harmony, and beats, and the arranger writes a MIDI track for each requested part. The accompaniment is newly arranged. The workflow infers chords and needs no chord chart or LLM planner.

1. Locate the WAV and the existing GPU worker. On a prepared worker, use paths on that worker and leave `YUE2_SSH` unset. From a Mac, use the local WAV path and set `YUE2_SSH` and `YUE2_SSH_PORT` to the worker's direct SSH connection. SSH mode expects both project scripts in `/workspace/wav2midi/` and downloads the result automatically. For first-time setup, follow [Run from your Mac](../../README.md#run-from-your-mac) or [Prepare a GPU worker](../../README.md#prepare-a-gpu-worker).
2. Keep the user's musical direction in the prompt. For named instruments, set `--parts` explicitly with `lead`, `bass`, `chords`, `arp`, or the standalone `drums` part. The pitched roles accept an alias, full General MIDI name, or program number from 1 to 128. Use `python3 wav2midi.py --list-sounds` to resolve a sound. If the user leaves instrumentation open, omit `--parts` to use the groove's default ensemble. Use `--groove rock|swing|soft|dance` only to select an accompaniment rhythm explicitly.
3. Run the command with the actual paths and prompt. For example, on the machine holding the input:

   ```sh
   python3 wav2midi.py "song.wav" \
     "Electronic rock. Keep the melody on guitar, add short synth fills, and leave out piano." \
     --parts "lead=guitar,bass=bass,drums,chords=synth" --out "result/song-rock.mid"
   python3 arranger.py inspect "result/song-rock.mid"
   ```

   Choose a fresh output path. Omitting `--out` writes a timestamped MIDI to the project’s `result/` directory. SSH mode downloads it into the local project. Check the inspection report for the requested track names and sounding notes. Four instruments produce four instrument tracks plus a conductor track for tempo and meter. Drums use channel 10. Return the MIDI path and the instrument list.

If inference reports a missing dependency, inspect the path on the worker. The default model directory is `/workspace/YuE/skills/yue2-music/instrumental`. A custom installation uses `YUE2_INSTRUMENTAL` in the worker process. The generator uses `.venv`, and transcription uses `.venv-transcribe` with both SheetSage2 and MERT weights. An interrupted SSH run may print a retained job directory. Check it for a completed `output.mid` before repeating inference.

## Other deliverables

- **Arrange an existing MIDI locally.** Inspect the input first, select the melody by zero-based track and one-based channel, and supply one chord per bar. The standard-library CLI supports `funk`, `swing`, and `neo_soul`. Follow [Arrange an existing MIDI locally](../../README.md#arrange-an-existing-midi-locally).
- **Keep a YuE2 audio performance.** The WAV-to-multitrack command discards its intermediate audio. For a saved mix, use a `reference` job or direct YuE2 generation. When running YuE2 directly, copy the verified `generation/native/audio.flac` into the requesting project’s `result/` directory under a fresh name. A finished job also supports a melody-and-chord transcription of that particular mix. Use the WAV workflow above when the deliverable calls for separately editable instrument parts.
- **Render a WAV preview or run queued jobs.** Start the loopback API and worker with the same `ARRANGER_DATA` directory. A `preview` renders the preset MIDI arrangement using FluidSynth and the worker's `SOUNDFONT`. The client downloads job artifacts to `result/<job_id>/`. Follow [Use the private job API](../../README.md#use-the-private-job-api).
