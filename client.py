"""Plan, submit, poll and download an arrangement from a private service."""
import argparse
import json
import sys
import time
from pathlib import Path

import httpx

from arranger import RESULT_DIR


def checked(response):
    if response.is_error:
        raise ValueError(f"HTTP {response.status_code}: {response.text[:1500]}")
    return response.json()


NAMES = {"midi": "arrangement.mid", "preview": "preview.wav", "reference": "reference.flac",
         "reference_midi": "reference-transcription.mid"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--request", help="Natural-language request, sent to the configured planner")
    group.add_argument("--plan", type=Path, help="Validated JSON intent, no LLM needed")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--track", type=int, default=1)
    parser.add_argument("--channel", type=int, default=1)
    parser.add_argument("--chords", default="")
    parser.add_argument("--key", default="")
    parser.add_argument("--keep-harmony", action="store_true")
    parser.add_argument("--take", default="1", help="Change to launch a new attempt")
    parser.add_argument("--out", type=Path, default=RESULT_DIR,
                        help="Download directory (default: project result/ directory)")
    args = parser.parse_args()
    with httpx.Client(base_url=args.url, timeout=120, trust_env=False) as client:
        if args.plan:
            plan = json.loads(args.plan.read_text(encoding="utf-8"))
        else:
            plan = checked(client.post("/plan", json={"text": args.request}))
        print("Plan:", json.dumps(plan, ensure_ascii=False), flush=True)
        fields = dict(plan_json=json.dumps(plan), track=str(args.track), channel=str(args.channel),
                      chords=args.chords, key=args.key, take=args.take,
                      keep_harmony=str(args.keep_harmony).lower())
        with args.input.open("rb") as stream:
            receipt = checked(client.post("/jobs", data=fields, files={"file": (args.input.name, stream)}))
        print("Job:", receipt["job_id"], flush=True)
        last = None
        while True:
            status = checked(client.get(receipt["status_url"]))
            state = status["state"]
            if state != last:
                print(state, flush=True)
                last = state
            if state not in {"queued", "running"}:
                break
            time.sleep(1)
        output = args.out / receipt["job_id"]
        output.mkdir(parents=True, exist_ok=True)
        for kind, route in status["artifacts"].items():
            destination = output / NAMES[kind]
            body = client.get(route)
            body.raise_for_status()
            if destination.exists():
                if destination.read_bytes() != body.content:
                    raise ValueError(f"A different file already exists: {destination}")
            else:
                with destination.open("xb") as f:
                    f.write(body.content)
            print(destination)
        if status["error"]:
            print(status["error"], file=sys.stderr)
        return 0 if state == "done" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, httpx.HTTPError) as exc:
        raise SystemExit(str(exc))
