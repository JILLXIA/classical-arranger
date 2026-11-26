# Tempo, rendering and DAW export

Run the commands from the repository root. The examples share a temporary session directory. Run the shell blocks in order, then copy the requested deliverables into the song’s version folder.

## Put tempo requests on the output clock

Distinguish “start speeding up at 1:25” from “reach 270 BPM at 1:25.” Identify the phrase in the approved recording, decide where the ramp starts, and state the intended interpretation if it affects the edit. A request anchored to the next downbeat can follow the phrase; a fixed arrival time refers to the finished audio. Use quarter-note BPM consistently, particularly when moving between 3/8, a jazz waltz and four-beat swing.

The [MIDI timing helper](../scripts/midi_timing.py) replaces tempo changes from the requested start forward. `--from` uses seconds in the approved MIDI clock. Each `--to` supplies elapsed seconds in the new clock and the BPM to reach. It rounds anchors to MIDI ticks, eases between them, and holds the last target through the ending. Repeat a target BPM at a later time to hold a plateau. The musical events retain their beat positions, including meter changes, notes, note-offs, controllers, pitch bends and markers.

```sh
session_dir=$(mktemp -d)
python3 skills/classical-arranger/scripts/midi_timing.py curve \
  examples/demo-swing.mid "$session_dir"/demo-tempo-edit.mid \
  --from 1 --to 2=135 --to 3=150
python3 skills/classical-arranger/scripts/midi_timing.py check \
  "$session_dir"/demo-tempo-edit.mid --at 1 --at 2 --at 3
```

Update any marker *text* that explicitly quotes an old timestamp or BPM. If rephrasing changes the beat count, arrange that form before constructing the tempo curve. Listen to the actual pulse in the render as well as checking the conductor data. At very fast tempos a beat detector or a listener may count half-time.

## Hear an edit locally

Use the repository's FluidSynth preview on a configured worker or machine with a General MIDI SoundFont. On a Mac, the [offline DLS renderer](../scripts/render_macos.py) uses the system General MIDI synth and Python's standard library. It writes 44.1 kHz stereo 16-bit PCM. `--track` selects one self-contained instrument track by its zero-based file index. Tempo and full-song length come from the whole MIDI. Use the same gain and release tail for related stems. A split track that relies on another track for its MIDI program/controllers needs those setup events copied to the stem.

```sh
python3 skills/classical-arranger/scripts/render_macos.py \
  examples/demo-swing.mid "$session_dir"/demo-original.wav --tail 0.3
python3 skills/classical-arranger/scripts/render_macos.py \
  "$session_dir"/demo-tempo-edit.mid "$session_dir"/demo-bass.wav --track 2 --tail 0.3
```

When the source is a printed performance aligned to the original MIDI, retime its audio using the two MIDI clocks. The frame map below pairs input and output sample frames and preserves the duration of an audio tail beyond the MIDI ending. It assumes the same musical form, beat extent and time zero. For a free model performance, align it to the score first. For a new passage or coda, render it on the target clock and join on the chosen musical cue.

```sh
python3 skills/classical-arranger/scripts/midi_timing.py frame-map \
  examples/demo-swing.mid "$session_dir"/demo-tempo-edit.mid "$session_dir"/demo-original.wav "$session_dir"/demo-frames.txt
python3 skills/classical-arranger/scripts/stretch_wav.py \
  "$session_dir"/demo-original.wav "$session_dir"/demo-frames.txt "$session_dir"/demo-retimed.wav
python3 skills/classical-arranger/scripts/midi_timing.py check \
  "$session_dir"/demo-tempo-edit.mid --wav "$session_dir"/demo-retimed.wav --at 3
```

The [local WAV stretcher](../scripts/stretch_wav.py) requires NumPy and SciPy in its Python environment and accepts 16- or 24-bit PCM WAV. It uses stereo-linked waveform overlap/add to change timing while retaining pitch and copies the section before the first changed map segment. For exposed attacks, large ratios or sustained leads, audition the stretch and compare with re-rendering the authored notes at the new tempo. For a stereo master with a printed guitar, substitute a new tone using the available pre-mix guitar stem or rebuild the backing without it before adding the new guitar.

## Export the version the user actually heard

1. Use the approved master as the reference for time zero, pickup and ending. Recover the exact authored note tracks, including overdubs and tag, before transcribing any audio. For a tempo change, map existing beat-based events through the new conductor. For parts authored against a fixed seconds clock, convert *both* onsets and releases back to ticks from the final tempo map. Apply the same conversion to bends and controllers.
2. For a model-only section, transcribe the relevant region of the approved master, keeping its offset. A known matching score, phrase alignment and the available stems help assign the top line and inner voices. SheetSage2 can supply melody and chord information when available. If the GPU is offline and a matching score exists, a score-aligned reconstruction can serve as the editable layer. State what was reconstructed and the expected difference from the heard performance.
3. Keep useful DAW parts on separate named tracks and channels, with GM drums on channel 10. Distinct electric lead, rhythm and harmony tracks let the musician change an amp or a bend independently. Preserve tempos, meter changes and phrase markers. Resolve overlapping duplicate drum hits when combining layers, giving the authored part precedence. Retain expressive velocities and CC11 handovers.
4. Read back the delivered SMF. Check the requested onsets, note-offs, bends, peak tempo, ensemble hit and last note against the working parts. The timing helper reports track/channel note counts, unclosed notes, markers in seconds and the signed WAV-minus-MIDI duration. A release tail can explain an end difference. Neither equal durations nor a successful decode establishes that two performances line up in the middle.
5. Link the matching MIDI and listening master when both are part of the delivery. Tell the musician to import the MIDI tempo map in their DAW. The listening master preserves amp treatment, stereo effects and model audio; the MIDI carries the playable notes and controls. If delivering WAV and FLAC as the same mix, compare decoded PCM at the same bit depth.
