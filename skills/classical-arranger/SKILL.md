---
name: classical-arranger
description: Arrange a WAV or MIDI, explore a YuE2 performance, or revise an approved take locally. Use for swing, fills, section transitions, tempo curves, instrument tone, WAV previews, and DAW-ready MIDI aligned to the mix.
---

# Run Classical Arranger

Run commands from the checkout that contains `wav2midi.py` and `arranger.py`. Choose the workflow by the source file and the artifact the musician wants.

## Organize deliverables

Use `result/<song>/<version>/` unless the musician specifies another destination. Reuse the song folder across revisions. For musician-facing takes, choose the next numbered folder with a short musical description, such as `result/furelise/v07-rock-guitar-290/`. Group the matching MIDI, WAV, FLAC and any requested stems there. An additional export of the same performance belongs in its existing folder. A changed arrangement, tempo or mix gets a new sibling folder. Keep working files in a separate session directory and link the delivered files at their final paths.

## Choose the working version and engine

- For a follow-up such as “more fills after 1:25,” identify the version the musician actually heard. Start from its master, tempo map, authored MIDI and available stems. Local editing is the default for changes to that performance. Read [Revising an approved take](references/revising-a-take.md).
- For a request to hear a fresh interpretation, use YuE2 on an available worker. Supply the approved take as the musical reference when the user likes its instrumentation. Keep the verified native audio for playback. A stopped GPU can stay stopped throughout local revisions.
- For a song supplied as a recording, check for a corresponding score or multitrack MIDI before choosing transcription. Compare the pickup, melodic pitches in more than one section, tempo changes and ending against the audio. A matching score provides an editable local starting point. The WAV-to-MIDI workflow below supports generating a new performance and arranging its transcription.
- For a tempo change, a timestamp-specific edit, or a MIDI export of the finished performance, read [Tempo, rendering and DAW export](references/tempo-and-export.md). Its local commands also render a single stem on macOS and retime an aligned WAV while preserving pitch.

When delivering a revision, identify the base take and the contributions from new model generation, reused audio, MIDI writing, transcription and mixing as applicable. Give listening cues in the delivered mix's clock. Mention which MIDI layers were reconstructed when the user plans to edit them.

## WAV and prompt to editable MIDI

Use `wav2midi.py` for this request. It takes the WAV and prompt directly. YuE2 generates a performance, SheetSage2 transcribes its melody, harmony, and beats, and the arranger writes a MIDI track for each requested part. The accompaniment is newly arranged. The workflow infers chords and needs no chord chart or LLM planner.

1. Locate the WAV and the existing GPU worker. On a prepared worker, use paths on that worker and leave `YUE2_SSH` unset. From a Mac, use the local WAV path and set `YUE2_SSH` and `YUE2_SSH_PORT` to the worker's direct SSH connection. SSH mode expects both project scripts in `/workspace/wav2midi/` and downloads the result automatically. For first-time setup, follow [Run from your Mac](../../README.md#run-from-your-mac) or [Prepare a GPU worker](../../README.md#prepare-a-gpu-worker).
2. Keep the user's musical direction in the prompt. For named instruments, set `--parts` explicitly with `lead`, `bass`, `chords`, `arp`, or the standalone `drums` part. The pitched roles accept an alias, full General MIDI name, or program number from 1 to 128. Use `python3 wav2midi.py --list-sounds` to resolve a sound. If the user leaves instrumentation open, omit `--parts` to use the groove's default ensemble. Use `--groove rock|swing|soft|dance` only to select an accompaniment rhythm explicitly.
3. Run the command with the actual paths and prompt. For example, on the machine holding the input:

   ```sh
   python3 wav2midi.py "song.wav" \
     "Electronic rock. Keep the melody on guitar, add short synth fills, and leave out piano." \
     --parts "lead=guitar,bass=bass,drums,chords=synth" --out "result/song/v01-electronic-rock/song-rock.mid"
   python3 arranger.py inspect "result/song/v01-electronic-rock/song-rock.mid"
   ```

   Choose a fresh output path. Omitting `--out` creates a timestamped version folder under the song. SSH mode downloads it into the local project. Check the inspection report for the requested track names and sounding notes. Four instruments produce four instrument tracks plus a conductor track for tempo and meter. Drums use channel 10. Return the MIDI path and the instrument list.

If inference reports a missing dependency, inspect the path on the worker. The default model directory is `/workspace/YuE/skills/yue2-music/instrumental`. A custom installation uses `YUE2_INSTRUMENTAL` in the worker process. The generator uses `.venv`, and transcription uses `.venv-transcribe` with both SheetSage2 and MERT weights. An interrupted SSH run may print a retained job directory. Check it for a completed `output.mid` before repeating inference.

## Other deliverables

- **Arrange an existing MIDI locally.** Inspect the input first, select the melody by zero-based track and one-based channel, and supply one chord per bar. The standard-library CLI supports `funk`, `swing`, and `neo_soul`. Follow [Arrange an existing MIDI locally](../../README.md#arrange-an-existing-midi-locally).
- **Keep a YuE2 audio performance.** The WAV-to-multitrack command discards its intermediate audio. For a saved mix, use a `reference` job or direct YuE2 generation. When running YuE2 directly, copy the verified `generation/native/audio.flac` into the chosen song/version folder. A finished job also supports a melody-and-chord transcription of that particular mix. Use the WAV workflow above when the deliverable calls for separately editable instrument parts.
- **Render a WAV preview or run queued jobs.** Start the loopback API and worker with the same `ARRANGER_DATA` directory. A `preview` renders the preset MIDI arrangement using FluidSynth and the worker's `SOUNDFONT`. The client downloads job artifacts to `result/<song>/<job_id>/` by default. For a named delivery, move the completed job folder to the chosen version name or collect its artifacts in the matching existing version. Follow [Use the private job API](../../README.md#use-the-private-job-api).
