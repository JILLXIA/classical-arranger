# Repository Guidelines

## Project Structure

Python modules live at the repository root. `arranger.py` reads MIDI, preserves the selected melody, and generates accompaniment. `preview.py` renders that arrangement as WAV with FluidSynth. `midi_events.py` exports melody events for a separate YuE2 reference performance.

`intent_planner.py` translates user requests into validated plans. `service.py` hosts the FastAPI routes and SQLite-backed job worker. `client.py` submits jobs and downloads their artifacts. The root-level tests use the MIDI files and JSON fixtures in `examples/`.

## Local Development

Run commands from the repository root. To inspect tracks and channels using only the Python 3.10+ standard library, run `python3 arranger.py inspect examples/demo.mid`. Track numbers start at zero and MIDI channels start at one.

For the API and tests, install the pinned dependencies with these commands.

```sh
python3 -m venv .venv-app
.venv-app/bin/python -m pip install -r requirements.txt
```

Start the API and worker in separate terminals. Both must use the same `ARRANGER_DATA` directory, which defaults to `arranger-data/` under the working directory.

```sh
# API
.venv-app/bin/python -m uvicorn service:app --host 127.0.0.1 --port 8000
# Worker
.venv-app/bin/python service.py
```

## Coding Style

Use four-space indentation, `snake_case` for functions and variables, `PascalCase` for classes, and `UPPER_CASE` for constants. Follow the surrounding module’s formatting. Keep the standalone MIDI CLI usable with the standard library.

## Testing

Run `.venv-app/bin/python -m unittest -v`. Name test files `test_*.py` and methods `test_*`. Extend `test_intent_planner.py` with mocked model responses for planning changes. Extend `test_workflow.py` for arrangement or job behavior. It compares MIDI output byte-for-byte against fixtures and checks that the melody survives each preset. To include the WAV render test, install FluidSynth 2.x and set `SOUNDFONT` to a General MIDI `.sf2` file.

## Commits and Pull Requests

A suggested commit subject is a short imperative such as `Preserve melody velocity in swing arrangements`. In pull requests, describe the behavior change, link any related issue, and report the tests run. For audible changes, identify the demo input and preset so reviewers can reproduce the result.
