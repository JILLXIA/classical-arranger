import json
import os
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from arranger import arrange_bytes, read_midi, selected_events
from midi_events import to_events
import service
import wav2midi


EXAMPLES = Path(__file__).resolve().parent / "examples"


class ArrangementTests(unittest.TestCase):
    def test_custom_midi_parts_follow_transcribed_meter_and_patches(self):
        parts = wav2midi.parse_parts("lead=violin,bass=43,arp=Orchestral Harp,drums")
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "beat.lab").write_text("0 2 3 4\n0.5 3 3 4\n1 1 3 4\n1.6 2 3 4\n")
            def n(pitch):
                return dict(start=0, end=2.2, pitch=pitch, velocity=90)
            playback = dict(duration=2.2, tracks=[dict(name="Ins", notes=[n(72)]),
                            dict(name="Chords", notes=[n(p) for p in (36, 60, 64, 67)])])
            (folder / "playback.json").write_text(json.dumps(playback))
            ppq, tracks = read_midi(wav2midi.arrangement(folder, parts, "soft"))
        meters = [(e.tick, e.data[:2]) for e in tracks[0] if e.kind == 0x158]
        self.assertEqual(meters, [(0, bytes([2, 2])), (2 * ppq, bytes([3, 2]))])
        self.assertEqual(len(tracks), 5)
        for track, channel, patch in zip(tracks[1:], (0, 1, 2, 9), (40, 42, 46, 0)):
            self.assertTrue(any(e.kind == 0xc0 + channel and e.data == bytes([patch]) for e in track))
            self.assertTrue(any(e.kind == 0x90 + channel and e.data[1] for e in track))
        self.assertEqual([e.data[0] for e in tracks[1] if e.kind == 0x90], [72])
        self.assertEqual(next(e.tick for e in tracks[1] if e.kind == 0x80), 4 * ppq)
        with self.assertRaises(ValueError):
            wav2midi.parse_parts("lead=129")

    def test_midi_reference_can_choose_its_own_harmony(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(service, "ROOT", Path(tmp)), TestClient(service.app) as api:
            files = {"file": ("melody.mid", (EXAMPLES / "demo.mid").read_bytes())}
            plan = {"midi_style": None, "outputs": ["reference"], "reference_style": "Bossa nova"}
            fields = {"plan_json": json.dumps(plan)}
            job = api.post("/jobs", files=files, data=fields)
            self.assertEqual(job.status_code, 202, job.text)
            cfg = api.get(job.json()["status_url"]).json()["config"]
            self.assertEqual(cfg["chords"], [])
            self.assertFalse(cfg["keep_harmony"])
            self.assertEqual(api.post("/jobs", files=files, data=dict(fields, keep_harmony="true")).status_code, 422)
            for preset_output in ("midi", "preview"):
                mixed = dict(plan, midi_style="funk", outputs=["reference", preset_output])
                self.assertEqual(api.post("/jobs", files=files, data={"plan_json": json.dumps(mixed)}).status_code, 422)

    def test_api_source_tools_and_history(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(service, "ROOT", Path(tmp)), TestClient(service.app) as api:
            self.assertEqual(api.get("/").status_code, 404)
            source = (EXAMPLES / "demo.mid").read_bytes()
            files = {"file": ("study.mid", source)}
            report = api.post("/midi/inspect", files=files).json()
            self.assertEqual(report["tracks"][1]["channels"][0]["notes"], 16)
            self.assertEqual(report["tracks"][1]["channels"][0]["arrangement_bars"], 4)
            fields = {"track": 1, "channel": 1, "chords": "Am,E7,Dm,Am"}
            events = api.post("/midi/events", files=files, data=fields)
            self.assertEqual(events.json(), json.loads((EXAMPLES / "demo-events.json").read_text()))
            self.assertEqual(api.post("/midi/events", files=files, data=dict(fields, pickup="1/0")).status_code, 422)
            plan = {"midi_style": "funk", "outputs": ["midi"], "reference_style": ""}
            fields["plan_json"] = json.dumps(plan)
            first = api.post("/jobs", files=files, data=fields).json()["job_id"]
            renamed = api.post("/jobs", files={"file": ("renamed.mid", source)}, data=fields).json()
            self.assertEqual(renamed["job_id"], first)
            second = api.post("/jobs", files=files, data=dict(fields, take="2")).json()["job_id"]
            listing = api.get("/jobs?limit=1").json()
            self.assertTrue(listing["has_more"])
            self.assertEqual(listing["jobs"][0]["id"], second)
            self.assertEqual(api.get(f"/jobs/{first}/source").content, source)
            with service.database() as db:
                config = json.loads(db.execute("SELECT config FROM jobs WHERE id=?", (first,)).fetchone()[0])
                config.pop("filename")
                db.execute("UPDATE jobs SET config=? WHERE id=?", (json.dumps(config), first))
            older = api.get("/jobs?limit=1&offset=1").json()
            self.assertEqual(older["jobs"][0]["config"]["filename"], "source.mid")
            self.assertFalse(older["has_more"])

    def test_wav_reference_requires_transcriber_before_inference(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"YUE2_INSTRUMENTAL": tmp}):
            folder = Path(tmp)
            exe = folder / ".venv/bin/python"
            exe.parent.mkdir(parents=True)
            exe.touch()
            with patch.object(service.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "SheetSage2"):
                    service.make_reference(folder, {"ext": ".wav"})
                run.assert_not_called()

    def test_reference_native_audio_from_worker(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"YUE2_INSTRUMENTAL": tmp}):
            folder = Path(tmp)
            exe = folder / ".venv/bin/python"
            exe.parent.mkdir(parents=True)
            exe.touch()
            source = (EXAMPLES / "demo.mid").read_bytes()
            (folder / "source.mid").write_bytes(source)
            audio = b"fLaC-test-payload"
            def simulate_cli(args, **kwargs):
                if "cover" in args:
                    self.assertIn("--keep-harmony", args)
                    self.assertIn("--offline", args)
                    target = folder / "yue/generation/native/audio.flac"
                    target.parent.mkdir(parents=True)
                    target.write_bytes(audio)
            cfg = dict(ext=".mid", track=1, channel=1, chords=["Am", "E7", "Dm", "Am"],
                       key="Am", bpm=100, keep_harmony=True, plan={"reference_style": "Soul"})
            with patch.object(service.subprocess, "run", side_effect=simulate_cli) as run:
                service.make_reference(folder, cfg)
            self.assertEqual(run.call_count, 3)
            self.assertEqual(json.loads((folder / "events.json").read_text())["bpm"], 100)
            self.assertEqual((folder / "reference.flac").read_bytes(), audio)

    def test_export_saved_reference_without_regenerating(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(service, "ROOT", Path(tmp)), TestClient(service.app) as api:
            home = Path(tmp).resolve() / "instrumental"
            audio = b"fLaC-existing-reference"
            plan = {"midi_style": None, "outputs": ["reference"], "reference_style": "Waltz"}
            job = api.post("/jobs", files={"file": ("theme.abc", b"test ABC")},
                           data={"plan_json": json.dumps(plan)}).json()["job_id"]
            url = f"/reference-midi/{job}"
            self.assertEqual(api.post(url).status_code, 409)
            with patch.object(service, "make_reference", side_effect=lambda folder, cfg: (folder / "reference.flac").write_bytes(audio)) as generate:
                service.worker(once=True)
                self.assertEqual(api.post(url).json()["state"], "queued")
                self.assertEqual(api.post(url).json()["state"], "queued")
                self.assertEqual(api.get(url + "/download").status_code, 404)
                with patch.dict(os.environ, {"YUE2_INSTRUMENTAL": str(home)}), self.assertLogs(level="ERROR"):
                    service.worker(once=True)
                self.assertEqual(api.get(url).json()["state"], "failed")
                status = api.get(f"/jobs/{job}").json()
                self.assertEqual(status["state"], "done")
                self.assertEqual(api.get(status["artifacts"]["reference"]).content, audio)

                exe = home / ".venv-transcribe/bin/python"
                exe.parent.mkdir(parents=True)
                exe.touch()
                (home / "models/SheetSage2").mkdir(parents=True)
                (home / "models/MERT-v2-FullSong").mkdir()
                midi = (EXAMPLES / "demo.mid").read_bytes()

                def transcriber(args, **kwargs):
                    self.assertEqual(Path(args[2]).read_bytes(), audio)
                    self.assertEqual(args[:2], [str(exe), "scripts/transcribe_cover.py"])
                    self.assertEqual(args[args.index("--task") + 1], "full")
                    self.assertIn("--offline", args)
                    self.assertEqual(api.get(url).json()["state"], "running")
                    self.assertEqual(api.post(url).json()["state"], "running")
                    output = Path(args[args.index("--output") + 1])
                    output.mkdir()
                    (output / "transcription.mid").write_bytes(midi)
                    return subprocess.CompletedProcess(args, 0)

                api.post(url)
                with patch.dict(os.environ, {"YUE2_INSTRUMENTAL": str(home)}), \
                        patch.object(service.subprocess, "run", side_effect=transcriber) as run:
                    service.worker(once=True)
                    self.assertEqual(api.post(url).json()["state"], "done")
                    service.worker(once=True)
                    run.assert_called_once()
                generate.assert_called_once()
            latest = api.get("/jobs").json()["jobs"][0]
            self.assertEqual(latest["reference_export"]["state"], "done")
            self.assertEqual(api.get(latest["artifacts"]["reference_midi"]).content, midi)

    def test_library_export_survives_worker_interruption(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(service, "ROOT", Path(tmp)), \
                patch.object(service, "PROJECT", Path(tmp)), TestClient(service.app) as api:
            name = "turkish-march-yue2.flac"
            sample = Path(tmp) / service.RECORDING / name
            sample.parent.mkdir(parents=True)
            sample.write_bytes(b"fLaC-demo")
            url = "/reference-midi/turkish-march"
            self.assertIsNone(api.get(url).json()["state"])
            api.post(url)
            with service.database() as db:
                db.execute("UPDATE reference_exports SET state='running'")
            service.worker(once=True)
            self.assertEqual(api.get(url).json()["state"], "failed")
            self.assertEqual(api.post(url).json()["state"], "queued")
            self.assertEqual(api.get(url + "/download").status_code, 404)
            self.assertEqual(api.post("/reference-midi/unknown").status_code, 404)

    def test_musical_reference(self):
        source = (EXAMPLES / "demo.mid").read_bytes()
        ppq, tracks = read_midi(source)
        leads = selected_events(tracks[1], 1)
        backings = set()
        for style in ("funk", "swing", "neo_soul"):
            chords = "Am9,E7,Dm9,Am9" if style == "neo_soul" else "Am,E7,Dm,Am"
            result = arrange_bytes(source, 1, 1, chords.split(","), style)
            self.assertEqual(result, (EXAMPLES / f"demo-{style.replace('_','-')}.mid").read_bytes())
            out_ppq, out = read_midi(result)
            self.assertEqual((out_ppq, len(out)), (ppq, 5))
            self.assertEqual(leads, selected_events(out[1], 1))
            backings.add(tuple(out[4]))
        self.assertEqual(len(backings), 3)
        events = to_events(source, 1, 1, ["Am","E7","Dm","Am"], "Am")
        self.assertEqual(events, json.loads((EXAMPLES / "demo-events.json").read_text()))
        self.assertEqual(len(events["notes"]), 16)

    def test_job_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(service, "ROOT", Path(tmp)), TestClient(service.app) as api:
            plan = dict(midi_style="funk", outputs=["midi"], reference_style="")
            files = {"file": ("exercise.mid", (EXAMPLES/"demo.mid").read_bytes())}
            fields = {"plan_json": json.dumps(plan), "chords": "Am,E7,Dm,Am"}
            r = api.post("/jobs", files=files, data=fields)
            self.assertEqual(r.status_code, 202, r.text)
            status_url = r.json()["status_url"]
            self.assertEqual(api.get(status_url).json()["state"], "queued")
            self.assertEqual(api.post("/jobs", files=files, data=fields).json()["status_url"], status_url)
            service.worker(once=True)
            status = api.get(status_url).json()
            self.assertEqual(status["state"], "done", status)
            self.assertEqual(api.get(status["artifacts"]["midi"]).content, (EXAMPLES/"demo-funk.mid").read_bytes())
            self.assertEqual(api.get(status_url + "/artifacts/reference").status_code, 404)

            bad = api.post("/jobs", files=files, data=dict(fields, chords="Am"))
            service.worker(once=True)
            self.assertEqual(api.get(bad.json()["status_url"]).json()["state"], "failed")
            with patch.dict(os.environ, {"YUE2_INSTRUMENTAL": ""}):
                fields["plan_json"] = json.dumps(dict(midi_style="funk", outputs=["reference","midi"], reference_style="Funk"))
                job = api.post("/jobs", files=files, data=fields)
                service.worker(once=True)
                partial = api.get(job.json()["status_url"]).json()
                self.assertEqual(partial["state"], "partial")
                self.assertIn("midi", partial["artifacts"])

    def test_preview(self):
        sf = os.environ.get("SOUNDFONT")
        if not sf:
            self.skipTest("Set SOUNDFONT to run the local FluidSynth render test.")
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "preview.wav"
            service.render_preview(EXAMPLES/"demo-funk.mid", Path(sf), wav)
            with wave.open(str(wav)) as audio:
                self.assertEqual((audio.getnchannels(),audio.getframerate()), (2,48000))
                self.assertGreater(audio.getnframes(), 200000)
                self.assertTrue(any(audio.readframes(audio.getnframes())))


if __name__ == "__main__":
    unittest.main()
