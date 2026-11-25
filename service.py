"""Private, single-user arrangement API. Run on loopback; use an SSH tunnel for a Pod."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from fractions import Fraction
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from arranger import arrange_bytes, inspect_bytes, read_midi
from midi_events import to_events
from preview import render_preview

ROOT = Path(os.environ.get("ARRANGER_DATA", "arranger-data")).resolve()
LIMIT = 32 * 1024 * 1024
ARTIFACTS = {"midi": "arrangement.mid", "preview": "preview.wav", "reference": "reference.flac"}
MIME = {"midi": "audio/midi", "preview": "audio/wav", "reference": "audio/flac"}
app = FastAPI(title="Classical Arranger", version="1.0", docs_url=None, redoc_url=None)
REFERENCE_MIDI_NAME = "reference-transcription.mid"


@contextlib.contextmanager
def database():
    ROOT.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(ROOT / "jobs.sqlite", timeout=15)
    db.row_factory = sqlite3.Row
    try:
        with db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs
                (id TEXT PRIMARY KEY, config TEXT NOT NULL, state TEXT NOT NULL,
                 error TEXT, available TEXT NOT NULL DEFAULT '[]')""")
            db.execute("""CREATE TABLE IF NOT EXISTS reference_exports
                (id TEXT PRIMARY KEY, state TEXT NOT NULL, error TEXT)""")
            yield db
    finally:
        db.close()


JOB_SELECT = """SELECT jobs.*, reference_exports.state AS export_state,
                reference_exports.error AS export_error FROM jobs
                LEFT JOIN reference_exports ON jobs.id = reference_exports.id"""


def find_job(job_id):
    with database() as db:
        row = db.execute(JOB_SELECT + " WHERE jobs.id = ?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Unknown job.")
    return row


@app.get("/health")
def health():
    with database() as db:
        db.execute("SELECT 1")
    return {"api": "ok"}  # A model generation test is a separate check.


def midi_upload(file):
    data = file.file.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(413, "Use a MIDI excerpt of 8 MiB or less.")
    return data


@app.post("/midi/inspect")
def inspect_melody(file: UploadFile = File(...)):
    try:
        return inspect_bytes(midi_upload(file), include_arrangement_bars=True)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def pickup_value(value):
    value = value.strip()
    if not value:
        return None
    if not re.fullmatch(r"(?:\d+(?:/\d+|\.\d+)?|\.\d+)", value):
        raise ValueError("Enter a positive pickup in quarter notes, for example 1/2.")
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError("Enter the pickup in quarter notes, for example 1/2.") from exc


@app.post("/midi/events")
def export_events(file: UploadFile = File(...), track: int = Form(..., ge=0),
                  channel: int = Form(..., ge=1, le=16), chords: str = Form(""),
                  key: str = Form(""), bpm: int | None = Form(None, ge=1),
                  pickup: str = Form("", max_length=32)):
    try:
        return to_events(midi_upload(file), track, channel,
                         [c.strip() for c in chords.split(",")] if chords else [],
                         key.strip() or None, bpm=bpm, pickup=pickup_value(pickup))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class PlanInput(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@app.post("/plan")
def plan(body: PlanInput):
    from intent_planner import plan_request
    base = os.environ.get("ARRANGER_LLM_BASE_URL")
    model = os.environ.get("ARRANGER_LLM_MODEL")
    if not base or not model:
        raise HTTPException(503, "Configure ARRANGER_LLM_BASE_URL and ARRANGER_LLM_MODEL, or submit an explicit plan.")
    try:
        return plan_request(body.text, base_url=base, model=model,
                            api_key=os.environ.get("ARRANGER_LLM_API_KEY", ""))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/jobs", status_code=202)
def submit(file: UploadFile = File(...), plan_json: str = Form(...),
           track: int = Form(1, ge=0), channel: int = Form(1, ge=1, le=16),
           chords: str = Form(""), key: str = Form(""), take: str = Form("1", max_length=80),
           keep_harmony: bool = Form(False), bpm: int | None = Form(None, ge=1),
           pickup: str = Form("", max_length=32)):
    ext = Path(file.filename or "").suffix.lower()
    ext = {".midi": ".mid", ".wave": ".wav"}.get(ext, ext)
    if ext not in {".mid", ".abc", ".wav"}:
        raise HTTPException(422, "Upload .mid, YuE2-native .abc or .wav. Other formats need conversion first.")
    try:
        plan = json.loads(plan_json)
        if not isinstance(plan, dict) or set(plan) != {"midi_style", "reference_style", "outputs"}:
            raise ValueError("Plan fields: midi_style, reference_style, outputs.")
        outputs, style, ref = plan["outputs"], plan["midi_style"], plan["reference_style"]
        if not isinstance(outputs, list) or not outputs or any(x not in ARTIFACTS for x in outputs):
            raise ValueError("outputs must be a nonempty list of midi, preview, reference.")
        symbolic = bool(set(outputs) & {"midi", "preview"})
        if symbolic and (ext != ".mid" or style not in ("funk", "swing", "neo_soul")):
            raise ValueError("Editable tracks/preview require .mid and funk, swing, or neo_soul. Transcribe audio separately first.")
        if not symbolic and style is not None:
            raise ValueError("Use null for midi_style in a reference-only request.")
        if "reference" in outputs and (not isinstance(ref, str) or not ref.strip() or len(ref) > 2000):
            raise ValueError("Supply reference_style (1–2000 characters).")
        if "reference" not in outputs and ref not in (None, ""):
            raise ValueError("Leave reference_style empty when no reference is requested.")
        chord_list = [c.strip() for c in chords.split(",")] if chords else []
        if symbolic and not chord_list:
            raise ValueError("Enter one chord per full bar for editable arrangement.")
        if keep_harmony and "reference" in outputs and ext == ".mid" and not chord_list:
            raise ValueError("Supply the original chord chart for keep_harmony.")
        if len(chord_list) > 4096:
            raise ValueError("Use a shorter excerpt.")
        normalized = dict(plan, outputs=sorted(set(outputs)))
        cfg = dict(plan=normalized, track=track, channel=channel, chords=chord_list,
                   key=key.strip(), ext=ext, take=take, keep_harmony=keep_harmony)
        if bpm is not None:
            cfg["bpm"] = bpm
        if pickup.strip():
            cfg["pickup"] = str(pickup_value(pickup))
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    config = json.dumps(cfg, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(config.encode() + b"\0")
    ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT) as staging:
        path = Path(staging) / ("source" + ext)
        with path.open("wb") as dst:
            size = 0
            while chunk := file.file.read(65536):
                size += len(chunk)
                if size > LIMIT:
                    raise HTTPException(413, "Use an excerpt smaller than 32 MiB.")
                digest.update(chunk)
                dst.write(chunk)
        if not size:
            raise HTTPException(422, "Empty source file.")
        job_id = digest.hexdigest()
        target = ROOT / job_id
        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone():
                target.mkdir(exist_ok=True)
                os.replace(path, target / path.name)
                saved = dict(cfg, filename=Path(file.filename or path.name).name)
                db.execute("INSERT INTO jobs(id, config, state) VALUES(?, ?, 'queued')",
                           (job_id, json.dumps(saved, ensure_ascii=False)))
    return {"job_id": job_id, "status_url": f"/jobs/{job_id}"}


def job_info(row):
    cfg = json.loads(row["config"])
    cfg.setdefault("filename", "source" + cfg["ext"])
    artifacts = {k: f"/jobs/{row['id']}/artifacts/{k}" for k in json.loads(row["available"])}
    if row["export_state"] == "done":
        artifacts["reference_midi"] = f"/reference-midi/{row['id']}/download"
    return {"id": row["id"], "config": cfg, "state": row["state"], "error": row["error"],
            "artifacts": artifacts, "reference_export": {"state": row["export_state"], "error": row["export_error"]}}


@app.get("/jobs")
def recent_jobs(offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100)):
    with database() as db:
        rows = db.execute(JOB_SELECT + " ORDER BY jobs.rowid DESC LIMIT ? OFFSET ?", (limit + 1, offset)).fetchall()
    return {"jobs": [job_info(row) for row in rows[:limit]], "has_more": len(rows) > limit}


@app.get("/jobs/{job_id}")
def status(job_id: str):
    return job_info(find_job(job_id))


@app.get("/jobs/{job_id}/source")
def job_source(job_id: str):
    cfg = job_info(find_job(job_id))["config"]
    return FileResponse(ROOT / job_id / ("source" + cfg["ext"]), filename=cfg["filename"])


@app.get("/jobs/{job_id}/artifacts/{kind}")
def artifact(job_id: str, kind: str):
    row = find_job(job_id)
    if kind not in json.loads(row["available"]):
        raise HTTPException(404, "Artifact unavailable.")
    return FileResponse(ROOT / job_id / ARTIFACTS[kind], media_type=MIME[kind], filename=ARTIFACTS[kind])


def reference_audio(source_id):
    row = find_job(source_id)
    if "reference" not in json.loads(row["available"]):
        raise HTTPException(409, "This take needs a finished YuE2 reference before exporting MIDI.")
    source = ROOT / source_id / ARTIFACTS["reference"]
    if not source.is_file():
        raise HTTPException(404, "Reference recording unavailable.")
    return source


@app.get("/reference-midi/{source_id}")
def reference_export_status(source_id: str):
    reference_audio(source_id)
    with database() as db:
        row = db.execute("SELECT state, error FROM reference_exports WHERE id=?", (source_id,)).fetchone()
    return dict(row) if row else {"state": None, "error": None}


@app.post("/reference-midi/{source_id}", status_code=202)
def request_reference_midi(source_id: str):
    reference_audio(source_id)
    with database() as db:
        db.execute("""INSERT INTO reference_exports (id, state) VALUES (?, 'queued')
                      ON CONFLICT(id) DO UPDATE SET state='queued', error=NULL
                      WHERE reference_exports.state='failed'""", (source_id,))
    return reference_export_status(source_id)


@app.get("/reference-midi/{source_id}/download")
def download_reference_midi(source_id: str):
    if reference_export_status(source_id)["state"] != "done":
        raise HTTPException(404, "The reference MIDI export is not ready.")
    return FileResponse(ROOT / source_id / REFERENCE_MIDI_NAME, media_type="audio/midi", filename=REFERENCE_MIDI_NAME)


def transcription_paths(home):
    exe = home / ".venv-transcribe/bin/python"
    model, base_model = home / "models/SheetSage2", home / "models/MERT-v2-FullSong"
    if not (exe.is_file() and model.is_dir() and base_model.is_dir()):
        raise ValueError("Audio transcription requires the SheetSage2 Python environment and the SheetSage2 and MERT weights on this worker.")
    return exe, model, base_model


def transcribe_reference(source_id):
    source = reference_audio(source_id)
    raw_home = os.environ.get("YUE2_INSTRUMENTAL")
    if not raw_home:
        raise ValueError("Set YUE2_INSTRUMENTAL on the worker and install the SheetSage2 transcriber.")
    home = Path(raw_home).resolve()
    exe, model, base_model = transcription_paths(home)
    folder = ROOT / source_id
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=folder) as tmp:
        output = Path(tmp) / "transcription"
        with (folder / "worker.log").open("ab") as log:
            result = subprocess.run([str(exe), "scripts/transcribe_cover.py", str(source), "--task", "full",
                                     "--output", str(output), "--model", str(model), "--base-model", str(base_model),
                                     "--offline"], cwd=home, stdout=log, stderr=subprocess.STDOUT)
        if result.returncode:
            raise ValueError("SheetSage2 transcription failed. Check worker.log for this recording and retry.")
        midi = output / "transcription.mid"
        if not midi.is_file():
            raise ValueError("SheetSage2 did not produce transcription.mid.")
        _, tracks = read_midi(midi.read_bytes())
        if not any(e.kind >> 4 == 9 and e.data[1] for track in tracks for e in track):
            raise ValueError("SheetSage2 found no sounding notes in the reference.")
        os.replace(midi, folder / REFERENCE_MIDI_NAME)


def process_pending_export():
    with database() as db:
        row = db.execute("SELECT id FROM reference_exports WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone()
        if not row:
            return False
        source_id = row["id"]
        db.execute("UPDATE reference_exports SET state='running' WHERE id=?", (source_id,))
    error = None
    try:
        transcribe_reference(source_id)
    except Exception as exc:
        logging.exception("Reference MIDI export for %s", source_id)
        error = str(exc)
    with database() as db:
        db.execute("UPDATE reference_exports SET state=?, error=? WHERE id=?",
                   ("failed" if error is not None else "done", error, source_id))
    return True


def make_reference(folder, cfg):
    raw_home = os.environ.get("YUE2_INSTRUMENTAL")
    if not raw_home:
        raise ValueError("Set YUE2_INSTRUMENTAL to the installed instrumental directory on the worker.")
    home = Path(raw_home).resolve()
    exe = home / ".venv/bin/python"
    if not exe.is_file():
        raise ValueError("YuE2 inference venv missing; complete setup and GPU doctor first.")
    def run(*args):
        with (folder / "worker.log").open("ab") as log:
            subprocess.run([str(exe), *map(str, args)], cwd=home, stdout=log,
                           stderr=subprocess.STDOUT, check=True)

    ext = cfg["ext"]
    source = folder / ("source" + ext)
    flags = []
    if ext == ".mid":
        harmony = cfg["chords"] if cfg["keep_harmony"] else []
        events = to_events(source.read_bytes(), cfg["track"], cfg["channel"], harmony, cfg["key"] or None,
                           bpm=cfg.get("bpm"), pickup=pickup_value(cfg.get("pickup", "")))
        events_path = folder / "events.json"
        events_path.write_text(json.dumps(events), encoding="utf-8")
        compiled = folder / "compiled"
        run("scripts/compile_score.py", events_path, "--output", compiled)
        source = compiled / "score.abc"
    if ext == ".wav":
        transcriber, model, base_model = transcription_paths(home)
        flags = ["--audio", source, "--transcriber-python", transcriber,
                 "--transcriber-model", model, "--base-model", base_model]
    else:
        flags = ["--abc", source]
    if cfg["keep_harmony"]:
        flags.append("--keep-harmony")
    result = folder / "yue"
    run("scripts/instrumental.py", "cover", *flags, "--style", cfg["plan"]["reference_style"],
        "--models-root", home / "models", "--offline", "--output", result)
    run("scripts/instrumental.py", "verify", result / "generation")
    candidates = [result / "generation/native/audio.flac", result / "generation/audio.flac", result / "listen/audio.flac"]
    audio = next((f for f in candidates if f.is_file() and f.stat().st_size > 4), None)
    if not audio:
        raise RuntimeError("Verified generation returned no expected FLAC. Inspect the job's yue/ directory.")
    shutil.copyfile(audio, folder / ARTIFACTS["reference"])


def process(job_id, cfg):
    folder = ROOT / job_id
    outputs = cfg["plan"]["outputs"]
    available, errors = [], []
    midi = folder / ARTIFACTS["midi"]
    def stage(kind, operation):
        try:
            operation()
        except Exception as exc:
            logging.exception("Job %s / %s", job_id, kind)
            errors.append(f"{kind}: {exc}")
            return False
        if kind in outputs:
            available.append(kind)
            with database() as db:
                db.execute("UPDATE jobs SET available=? WHERE id=?", (json.dumps(available), job_id))
        return True
    if "midi" in outputs or "preview" in outputs:
        midi_ok = stage("midi", lambda: midi.write_bytes(arrange_bytes(
            (folder / "source.mid").read_bytes(), cfg["track"], cfg["channel"], cfg["chords"], cfg["plan"]["midi_style"])))
        if "preview" in outputs and midi_ok:
            stage("preview", lambda: render_preview(midi, Path(os.environ.get("SOUNDFONT", "")), folder / ARTIFACTS["preview"]))
    if "reference" in outputs:
        stage("reference", lambda: make_reference(folder, cfg))
    state = ("partial" if available else "failed") if errors else "done"
    with database() as db:
        db.execute("UPDATE jobs SET state=?, error=? WHERE id=?", (state, "\n".join(errors) or None, job_id))


def worker(once=False):
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("One worker is already serving this data directory.")
        with database() as db:
            db.execute("""UPDATE reference_exports SET state='failed',
                          error='Worker interrupted. Retry the MIDI export.' WHERE state='running'""")
            db.execute("""UPDATE jobs SET state=CASE WHEN available='[]' THEN 'failed' ELSE 'partial' END,
                          error='Worker interrupted. Check artifacts and submit a new take to retry.'
                          WHERE state='running'""")
        while True:
            with database() as db:
                row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone()
                if row:
                    db.execute("UPDATE jobs SET state='running' WHERE id=?", (row["id"],))
            if row:
                process(row["id"], json.loads(row["config"]))
                if once:
                    return
            exported = process_pending_export()
            if once:
                return
            if row is None and not exported:
                time.sleep(0.5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run one private worker; start the API via uvicorn service:app.")
    parser.add_argument("--once", action="store_true", help="Process at most one queued job and exit.")
    worker(parser.parse_args().once)
