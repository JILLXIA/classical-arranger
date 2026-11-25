"""Deploy YuE2 on one GPU and request a stop on completion or from the running Pod.

The Pod timer requires a running container and a reachable Runpod API.
It is a best-effort stop, not a provider-enforced billing cap.
"""
import fcntl
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen


HERE = Path(__file__).resolve().parent
NAME = "classical-arranger-yue2"
GPU = "NVIDIA GeForce RTX 3090"
MAX_GPU_PRICE = 0.50


def graphql(key, query, **variables):
    request = Request("https://api.runpod.io/graphql", headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json",
        "User-Agent": "classical-arranger-deploy/1",
    }, data=json.dumps({"query": query, "variables": variables}).encode())
    with urlopen(request, timeout=30) as response:
        body = json.load(response)
    if body.get("errors"):
        raise RuntimeError("; ".join(e["message"] for e in body["errors"]))
    return body["data"]


def lookup(key):
    pods = graphql(key, "{ myself { pods { id name desiredStatus } } }")["myself"]["pods"]
    return [p for p in pods if p["name"] == NAME and p["desiredStatus"] != "TERMINATED"]


def get_pod(key, pod_id):
    return graphql(key, """query($id: String!) { pod(input: {podId: $id}) {
        desiredStatus runtime { ports { ip isIpPublic privatePort publicPort } }
    } }""", id=pod_id)["pod"]


def stop(key, pod_id):
    for attempt in range(5):
        try:
            if get_pod(key, pod_id)["desiredStatus"] == "EXITED":
                print(f"Stopped {pod_id}; 80 GB volume retained.", flush=True)
                return
            graphql(key, """mutation($id: String!) {
                podStop(input: {podId: $id}) { id desiredStatus }
            }""", id=pod_id)
        except Exception as error:
            print(f"Stop check {attempt + 1}: {error}", file=sys.stderr)
        time.sleep(4)
    if get_pod(key, pod_id)["desiredStatus"] != "EXITED":
        raise RuntimeError(f"Check Stop for Pod {pod_id} in the Runpod console.")
    print(f"Stopped {pod_id}; 80 GB volume retained.", flush=True)


def ssh_command(key, pod_id, identity):
    for _ in range(75):
        pod = get_pod(key, pod_id)
        if pod["desiredStatus"] == "EXITED":
            raise RuntimeError("The Pod stopped before SSH became available.")
        for port in (pod.get("runtime") or {}).get("ports") or []:
            if port["privatePort"] == 22 and port["isIpPublic"]:
                address = ipaddress.ip_address(port["ip"])
                ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                       "-o", "StrictHostKeyChecking=accept-new", "-o", "IdentitiesOnly=yes",
                       "-o", f"UserKnownHostsFile={HERE}/known-hosts-{pod_id}",
                       "-i", str(identity), "-p", str(int(port["publicPort"])), f"root@{address}"]
                if subprocess.run(ssh + ["true"], stderr=subprocess.DEVNULL).returncode == 0:
                    return ssh
        time.sleep(5)
    raise RuntimeError("SSH did not become ready. Inspect the Pod's startup log.")


def schedule_pod_stop(ssh, pod_id, deadline):
    # Leave ten minutes for API retries before the two-hour budget expires.
    seconds = int((deadline - datetime.now(timezone.utc)).total_seconds()) - 600
    if seconds <= 0:
        raise RuntimeError("Insufficient time left to run the GPU test.")
    script = """\
set -eu
command -v setsid >/dev/null
command -v timeout >/dev/null
runpodctl pod stop --help >/dev/null
setsid bash -c '
    sleep "$1"
    until timeout 30 runpodctl pod stop "$2"; do sleep 10; done
' bash "$1" "$2" </dev/null >/tmp/yue2-scheduled-stop.log 2>&1 &
kill -0 "$!"
"""
    command = shlex.join(["bash", "-s", "--", str(seconds), pod_id])
    subprocess.run(ssh + [command], input=script, text=True, check=True, timeout=20)


def install_and_download(ssh, pod_id):
    output = None
    with (HERE / "deploy-yue2.sh").open("rb") as script, \
            (HERE / f"{pod_id}-install.log").open("a") as log, \
            subprocess.Popen(ssh + ["bash -s"], stdin=script, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, errors="replace") as process:
        for line in process.stdout:
            print(line, end="", flush=True)
            log.write(line)
            if line.startswith("Verified audio: "):
                output = line.removeprefix("Verified audio: ").strip()
        if process.wait() or not output or not re.fullmatch(
                r"/workspace/yue2-check\.[A-Za-z0-9]+/example/generation/native/audio\.flac", output):
            raise RuntimeError("YuE2 installation or the GPU smoke test failed. See the install log.")
    remote = shlex.quote(output)
    expected = subprocess.check_output(ssh + ["sha256sum " + remote], text=True).split()[0]
    target = HERE / f"{pod_id}-verified.flac"
    temporary = target.with_suffix(".part")
    with temporary.open("wb") as f:
        subprocess.run(ssh + ["cat " + remote], stdout=f, check=True)
    if hashlib.sha256(temporary.read_bytes()).hexdigest() != expected:
        raise RuntimeError("Downloaded FLAC checksum mismatch. The source remains on /workspace.")
    temporary.replace(target)
    print(f"Verified audio on this Mac: {target}", flush=True)


def deploy(key, identity):
    prior = lookup(key)
    if prior:
        print("Existing Pod, no new rental:", [(p["id"], p["desiredStatus"]) for p in prior])
        return
    offers = graphql(key, """query($id: String!) { gpuTypes(input: {id: $id}) {
        lowestPrice(input: {gpuCount: 1, secureCloud: true, minMemoryInGb: 32,
                            supportPublicIp: true, minCudaVersion: "12.8"}) { uninterruptablePrice }
    } }""", id=GPU)["gpuTypes"]
    quote = ((offers[0]["lowestPrice"] or {}) if offers else {}).get("uninterruptablePrice")
    if quote is None or not 0 < float(quote) <= MAX_GPU_PRICE:
        raise RuntimeError(f"No matching Secure Cloud offer at or below ${MAX_GPU_PRICE:.2f}/h (quote: {quote}).")
    deadline = datetime.now(timezone.utc) + timedelta(hours=2)
    settings = dict(name=NAME, gpuTypeId=GPU, gpuCount=1, cloudType="SECURE",
                    imageName="runpod/pytorch:1.0.3-cu1281-torch291-ubuntu2404",
                    deployCost=float(quote), minMemoryInGb=32, minCudaVersion="12.8",
                    containerDiskInGb=20, volumeInGb=80, volumeMountPath="/workspace",
                    startSsh=True, startJupyter=False, supportPublicIp=True, ports="22/tcp",
                    env=[dict(key="PUBLIC_KEY", value=identity.with_suffix(".pub").read_text().strip())])
    print(f"Creating 1 RTX 3090 at quoted ${quote}/h plus storage. Target stop by {deadline.isoformat()}. "
          "The Pod timer needs a running container and access to Runpod.", flush=True)
    try:
        result = graphql(key, """mutation($input: PodFindAndDeployOnDemandInput!) {
            podFindAndDeployOnDemand(input: $input) { id costPerHr volumeInGb }
        }""", input=settings)["podFindAndDeployOnDemand"]
        pod_id = result["id"]
    except (OSError, ValueError, KeyError, TypeError, KeyboardInterrupt):
        # A lost response may hide a successful creation. Never repeat the mutation.
        found = lookup(key)
        if len(found) == 1:
            stop(key, found[0]["id"])
        raise
    try:
        (HERE / "runpod-id.txt").write_text(pod_id + "\n")
        print("Pod ID:", pod_id, flush=True)
        if result["volumeInGb"] != 80 or result["costPerHr"] is None or float(result["costPerHr"]) > MAX_GPU_PRICE + 0.02:
            raise RuntimeError(f"Unexpected provisioned configuration: {result}")
        ssh = ssh_command(key, pod_id, identity)
        schedule_pod_stop(ssh, pod_id, deadline)
        install_and_download(ssh, pod_id)
    finally:
        stop(key, pod_id)


def main():
    identity = Path.home() / ".ssh/id_ed25519"
    if not identity.is_file() or not identity.with_suffix(".pub").is_file():
        raise SystemExit("This launcher expects the Mac's SSH key pair at ~/.ssh/id_ed25519.")
    subprocess.run(["bash", "-n", str(HERE / "deploy-yue2.sh")], check=True)
    socket.getaddrinfo("api.runpod.io", 443)
    with (HERE / ".runpod-deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        key = os.environ.get("RUNPOD_API_KEY") or getpass.getpass("Runpod API key (hidden): ")
        if not key.strip():
            raise SystemExit("A Runpod API key is required.")
        deploy(key.strip(), identity)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        sys.exit(f"Deployment interrupted: {exc}")
