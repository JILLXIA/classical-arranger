# Revising an approved take

## Recover what the musician heard

Match the published master to its revision using filename, duration and a distinctive cue. Recover the matching MIDI, tempo map, pre-effect stems and mix settings where available. A nearby score with the same title can have different repeats, pickups or an earlier ending. Check the opening, a middle phrase and the cadence before aligning it.

For a project with likely follow-ups, keep the inputs needed for another edit in a revision-specific working directory, for example `arranger-data/sessions/<piece>/<revision>/`. Useful items include the parent take name, model audio and seed if used, authored MIDI, dry stems with their gains, tempo or alignment maps, and the short script or parameters that rebuild the changed passage. Record which filenames produced the delivered mix. A temporary directory suits scratch renders; move the working parts needed for later revisions into the session before finishing. Give the musician links to their requested deliverables.

When asking YuE2 for inspiration, specify ensemble roles, where the head is stated and which phrases have room to improvise. Save its verified native FLAC under a fresh name before worker cleanup. A MIDI preset preview, a YuE2 stereo performance and a mix of that performance with later overdubs are different useful starting points. Choose the one the user approved.

For example, on a prepared worker, run this from the YuE2 instrumental directory with the actual reference path and a fresh take directory. The example keeps the approved harmony and leaves performance choices to the model.

```sh
.venv/bin/python scripts/instrumental.py cover --audio "$REFERENCE_WAV" \
  --style "Violin-led swing quintet. State the head simply, then trade short answers with cello. Walking bass, clean guitar and ride cymbal." \
  --keep-harmony --seed 73 --offline --models-root models \
  --transcriber-python "$PWD/.venv-transcribe/bin/python" \
  --transcriber-model "$PWD/models/SheetSage2" --base-model "$PWD/models/MERT-v2-FullSong" \
  --output "$TAKE_DIR"
.venv/bin/python scripts/instrumental.py verify "$TAKE_DIR/generation"
```

Retrieve `$TAKE_DIR/generation/native/audio.flac` to the requesting project, check that it decodes and compare checksums when transferring between machines. For an existing local take, move straight to the edits below.

## Make the change audible

| What the musician hears | A practical edit |
| --- | --- |
| Swing feels weak | Listen for the underlying meter. For a swung waltz, give the three beats a distinct shape. For a requested four-beat swing, rephrase the pickup and cadences into four, give the bass a quarter-note walk, and place the ride pickup and foot hi-hat around beats two and four. Vary the offbeat timing and accents with phrase and speed. |
| The theme needs more freedom | State the recognizable head first. Locate the gaps between its note-offs and the next attacks, then write answers that resolve into the next chord tone. Try a chromatic enclosure, a blue note or a cello answer on a repeat. Leave dense runs and the next downbeat audible. Use a new model take when the musician wants to discover a different performance. |
| The whole ensemble seems to change at once | Move the *start* of the handover earlier if the opening feels static. Let a recurring hat or bass pickup connect both sections. Bring in clean guitar responses over several phrases, let the electric lead take a recognizable line, and taper the strings separately. Reserve the strongest fills and amp tone for later. |
| The lead guitar feels dry | Find the guitar stem and its MIDI first. Shape held notes with a scoop, bend release or delayed vibrato. Put short picked replies in the holes. For a wider chorus, double the rhythm with a second attack and panning, or harmonize a selected lead phrase. Shape the amp and cabinet tone and add a low, darker, tempo-following delay. Compare at similar loudness and leave room for the snare and bass. |
| The ending loses energy | Write toward a particular downbeat. A tom run or a brief rest can set up a single ensemble hit. For a bass/guitar tag, write the order of the answers and their final note-offs. Shorten effect returns enough for the stop to register. Keep any requested natural decay. |

For pitch bends, set the MIDI bend range explicitly, use an independent channel for a harmonized line and reset the wheel between phrases. Bound a fill by the actual note release and the next attack. On a doubled guitar take, small differences in picking and voicing survive mono playback better than simply delaying an identical waveform.

## Keep continuity through the mix

With separate stems, automate levels, expression and effects on the affected voices. Keep one shared musical clock and render from a common origin. A stereo model mix couples instruments together. Spectral processing can favor transients, bass or sustained tones, with limited isolation. For precise changes, use authored stems, reduce the outgoing layer and let a new playable part take over.

When joining performances, find a matching harmonic phrase and downbeat in both. For variable performances, use pitch-class alignment for the phrase, then check local drum transients for beat phase. A long crossfade between unrelated or drifting lead lines can sound like two bands playing at once. Align the overlap, stagger the handover, and listen from several bars before it through the first full rock phrase. Check headphones and mono when widening guitars or overlapping takes.

For two accelerandi, choose a theme return for the first, let the receiving instrument establish its phrase at a stable pulse, and use the next melodic return or drum pickup to launch the second. The recent swing-to-rock arrangement reached 270 BPM around 1:25, held there briefly, then reached 290 in the finale. Those were cues for that arrangement. Derive new cues and dynamics from the next recording and brief.

Keep the previous master under its existing name. Compare the new edit with that master at the last unchanged phrase, the handover, the climax and the actual final cutoff. Say which features you checked by reading events or measuring audio and which you assessed by listening.
