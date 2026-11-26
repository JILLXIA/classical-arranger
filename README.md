# Classical Arranger

Turn a WAV recording and an arrangement prompt into a multitrack MIDI file you can edit in Logic Pro or another DAW. Choose the instruments and the job each one plays. YuE2 generates a new instrumental performance, SheetSage2 transcribes that performance, and the arranger uses its melody, chords, and beat timing to write independent MIDI parts.

## Arrange a WAV

With a prepared GPU worker, run this command in the worker terminal. Replace the WAV path with your recording.

```sh
python3 /workspace/wav2midi/wav2midi.py "/workspace/song.wav" \
  "Electronic rock. Keep the melody on electric guitar and add short chord fills." \
  --parts "lead=guitar,bass=bass,drums,chords=rhodes"
```

The command writes a MIDI file to `result/<song>/<timestamp>-<groove>/` and prints its full path. Pass `--out` to choose a specific file. Open the MIDI file in Logic Pro and import the tempo map when prompted. Each part has its own track, MIDI channel, and General MIDI program or drum assignment. You can replace Logic's software instrument on each track.

### Choose your instruments

| Role | What it plays |
| --- | --- |
| `lead` | The transcribed lead melody, with note timing and velocities |
| `bass` | A bass line built from the detected harmony |
| `chords` | Chord voicings played as stabs or sustained notes |
| `arp` | Individual chord tones played in sequence |
| `drums` | A drum pattern on General MIDI channel 10 |

A chamber trio can use `--parts "lead=violin,bass=cello,arp=harp"`. A jazz quartet can use `--parts "lead=Alto Sax,bass=upright bass,drums,chords=rhodes"`. Repeat a pitched role to layer instruments, or omit drums entirely. A file supports up to 15 pitched tracks plus one drum track.

- **Sound selection.** Use an English alias, a full GM instrument name, or a program number from 1 to 128. Run `python3 wav2midi.py --list-sounds` to see them.
- **Prompt and groove.** Put the musical direction in the prompt. Use `--parts` for exact MIDI instrumentation. Without it, the program chooses a default ensemble. English keywords select the symbolic accompaniment groove, or you can specify `--groove rock`, `swing`, `soft`, or `dance`. YuE2 accepts free-form styles.
- **Alternate takes.** Change `--seed 42` to another nonnegative integer for a different performance. Choose a fresh output filename when using `--out`.

The multitrack file is a new arrangement of the transcribed performance. Exact guitar, bass, and drum performances cannot be recovered from a stereo mix through this workflow. Intermediate audio is temporary. To keep a generated recording as well, use the direct YuE2 or job API workflow below.

### Run from your Mac

Use Python 3.10 or later. From the repository directory, set the direct SSH host and port shown by your GPU provider. The worker needs the model environment described below and copies of `wav2midi.py` and `arranger.py` in `/workspace/wav2midi/`.

```sh
export YUE2_SSH="root@YOUR_POD_HOST"
export YUE2_SSH_PORT="YOUR_SSH_PORT"
ssh -p "$YUE2_SSH_PORT" "$YUE2_SSH" 'mkdir -p /workspace/wav2midi'
scp -P "$YUE2_SSH_PORT" wav2midi.py arranger.py "$YUE2_SSH:/workspace/wav2midi/"

python3 wav2midi.py "/path/to/song.wav" \
  "Chamber ensemble with a lyrical violin melody" \
  --parts "lead=violin,bass=cello,arp=harp"
```

SSH mode uploads the local WAV, runs inference on the worker, and downloads the MIDI into the corresponding song and version folder on the Mac. You can also pass `--ssh` and `--port` per run. In the GPU terminal itself, leave the SSH options and variables unset.

## Prepare a GPU worker

Use a Linux worker with a BF16-capable NVIDIA GPU in the 24 GB class, at least 24 GB of available host RAM, and persistent space for model weights, two Python environments, and intermediate audio. The WAV workflow requires both environments. FFmpeg 6.1 and its shared libraries must be available on the worker.

### Install YuE2

```sh
cd /workspace
git clone --depth 1 --branch yue2-music-v1.2.0 https://github.com/multimodal-art-projection/YuE.git
cd YuE/skills/yue2-music/instrumental
python3 scripts/verify_bundle.py
python3 scripts/test_tools.py
python3 scripts/setup_runtime.py install --venv .venv
.venv/bin/python scripts/setup_runtime.py models --output models
.venv/bin/python scripts/setup_runtime.py doctor --require-gpu --models-root models
```

The default installation path is `/workspace/YuE/skills/yue2-music/instrumental`. For a different location, set `YUE2_INSTRUMENTAL` in the worker environment. See the [YuE2 instrumental release](https://github.com/multimodal-art-projection/YuE/tree/yue2-music-v1.2.0/skills/yue2-music/instrumental).

### Install the WAV transcriber

From the same instrumental directory, create a separate Python 3.11 environment for SheetSage2. The example below uses CUDA 12.6 PyTorch wheels and pinned model revisions.

```sh
python3.11 -m venv .venv-transcribe
.venv-transcribe/bin/python -m pip install huggingface-hub==0.36.0
.venv-transcribe/bin/huggingface-cli download m-a-p/SheetSage2 \
  --revision eab522a8168e8b8b8c4856bf8609cd86198f01fe --local-dir models/SheetSage2
.venv-transcribe/bin/huggingface-cli download m-a-p/MERT-v2-FullSong \
  --revision d8ba1c745e733b3908ce6ad16ebeb17ac7600a42 --local-dir models/MERT-v2-FullSong
.venv-transcribe/bin/python -m pip install torch==2.8.0 torchaudio==2.8.0 \
  --index-url https://download.pytorch.org/whl/cu126
.venv-transcribe/bin/python -m pip install -r models/SheetSage2/requirements.txt
```

Model downloads need network access during setup. Generation and transcription use the installed weights offline. [SheetSage2 model card](https://huggingface.co/m-a-p/SheetSage2) · [MERT-v2-FullSong model card](https://huggingface.co/m-a-p/MERT-v2-FullSong)

## Other workflows

### Arrange an existing MIDI locally

The standalone MIDI CLI uses the Python standard library and preserves the selected melody track and channel. Track numbers start at zero and channels at one. The three accompaniment presets are `funk`, `swing`, and `neo_soul`. Supply one chord per full bar.

```sh
python3 arranger.py inspect examples/demo.mid
python3 arranger.py arrange examples/demo.mid --track 1 --channel 1 \
  --chords Am9,E7,Dm9,Am9 --style neo_soul
```

The arranged MIDI is saved in `result/<song>/<timestamp>-<style>/` by default. Use a separate melody track for a polyphonic piano score. For a YuE2-native score, export monophonic melody events with `midi_events.py` and compile them with the upstream score compiler. Use fixed tempo, a supported meter, and note lengths on the supported grid. The exporter supports explicit tempo and pickup overrides.

```sh
python3 midi_events.py examples/demo.mid --track 1 --channel 1 --key Am \
  --chords Am,E7,Dm,Am --out /tmp/melody-events.json
# Run in the YuE2 instrumental directory with the events file available locally.
python3 scripts/compile_score.py /tmp/melody-events.json --output work/score-01
.venv/bin/python scripts/instrumental.py cover --abc work/score-01/score.abc \
  --style "Warm electric piano, round bass and laid-back drums" \
  --models-root models --offline --output work/take-01
.venv/bin/python scripts/instrumental.py verify work/take-01/generation
```

The generated mix is in `work/take-01/generation/native/audio.flac`. For an existing recording, replace `--abc PATH` with `--audio PATH` and pass `--transcriber-python .venv-transcribe/bin/python --transcriber-model "$PWD/models/SheetSage2" --base-model "$PWD/models/MERT-v2-FullSong"`. Add `--keep-harmony` to retain supplied or transcribed chords. For original music, use the upstream `run --style` command with a fresh output directory.

### Use the private job API

From the project root, install the application dependencies. Start the API and the worker in separate terminals with the same absolute `ARRANGER_DATA` path, or use their shared default of `arranger-data/` from the same working directory.

```sh
python3 -m venv .venv-app
.venv-app/bin/python -m pip install -r requirements.txt
# Terminal 1
.venv-app/bin/python -m uvicorn service:app --host 127.0.0.1 --port 8000
# Terminal 2
.venv-app/bin/python service.py
# Terminal 3
.venv-app/bin/python client.py examples/demo.mid --plan examples/midi-only.json \
  --track 1 --channel 1 --chords Am9,E7,Dm9,Am9
```

The client polls the job and writes artifacts to `result/<song>/<job_id>/` in the client project. Request `midi` for the preset arrangement, `preview` for a WAV of those same notes, and `reference` for a separate YuE2 performance. A reference-only plan is included in `examples/reference-plan.json`. To run the API on a pod, bind it to loopback and forward its port through SSH.

- **Reference worker.** Set `YUE2_INSTRUMENTAL` on the worker. WAV input also needs SheetSage2. To transcribe a finished job's reference mix into melody-and-chord MIDI, submit `POST /reference-midi/{job_id}`, poll the same route with GET, and download from `GET /reference-midi/{job_id}/download` when done.
- **WAV previews.** Install FluidSynth 2.x and set `SOUNDFONT` to a General MIDI `.sf2` file on the worker. Set `FLUIDSYNTH_LIBRARY` if library discovery fails.
- **Optional text planner.** Set `ARRANGER_LLM_BASE_URL` and `ARRANGER_LLM_MODEL` on the API for a chat-completions endpoint with JSON support. Supply `ARRANGER_LLM_API_KEY` if required. Use `client.py --request "Make a funk MIDI arrangement"` instead of `--plan`, along with the input and chord arguments. The WAV-to-multitrack command takes its prompt directly.
- **Jobs and retries.** `GET /health` checks the API and database. `GET /jobs/{id}` reports status and available artifacts. Identical input, settings, and take reuse the same job. After correcting a failed setup, pass a new take such as `--take 2`.

## Development

Run `.venv-app/bin/python -m unittest -v`. The optional WAV render test also requires FluidSynth and a SoundFont. The small MIDI and JSON fixtures in `examples/` cover melody preservation, preset output, and event export. Recordings, user-generated MIDI, model weights, environments, and job databases belong outside version control.
