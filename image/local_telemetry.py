"""Anonymous run telemetry for the public forge image (opt-out).

One small event per run — started / completed / failed — so we can see
which image versions are in the wild and which stages break, without a
support thread. WHAT IS SENT: image/build ids, boundary source, window
length, domain count, stage reached, coarse crash class, duration, CPU
count, outcome, a random install id. NEVER SENT: namelists, domain
coordinates, file paths, hostnames, logs, or anything from the bundle —
read send() below; that dict is the entire payload.

Opt out any of three ways (all honored):
    --no-telemetry              CLI flag
    BLIP_TELEMETRY=0            env var
    DO_NOT_TRACK=1              the console-app convention

Delivery is best-effort BY DESIGN: a single POST with a short timeout,
started events on a daemon thread, and every failure is silent — a run
must never slow down or break because api.frothy.surf is unreachable.
"""
import json
import os
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

# override exists for testing against a local endpoint, not for
# pointing other people's telemetry anywhere
_ENDPOINT = os.environ.get("BLIP_TELEMETRY_URL", "https://api.frothy.surf") + "/e"
IMAGE_REV = os.environ.get("BLIP_IMAGE_REV", "dev")
MODEL_ID = os.environ.get("BLIP_MODEL_ID", "dev")

_state = {"enabled": False, "anon": None, "base": {}, "t0": 0.0, "sent_final": False}


def _truthy_off(v):
    return str(v or "").strip().lower() in ("1", "true", "yes")


def opted_out(no_telemetry_flag):
    if no_telemetry_flag:
        return True
    if os.environ.get("BLIP_TELEMETRY", "1") == "0":
        return True
    if _truthy_off(os.environ.get("DO_NOT_TRACK")):
        return True
    return False


def _install_id(out_dir):
    """Random id persisted next to the outputs — counts installs, not
    people; lives only as long as the user keeps their out directory."""
    try:
        p = Path(out_dir) / ".frothy-id"
        if p.exists():
            v = p.read_text().strip()
            if v:
                return v[:36]
        v = str(uuid.uuid4())
        p.write_text(v + "\n")
        return v
    except OSError:
        return str(uuid.uuid4())  # unwritable out dir: still counted, once


def _post(name, props):
    body = json.dumps({
        "site": "local",
        "anon_id": _state["anon"],
        "events": [{"name": name, "props": props}],
    }).encode()
    req = urllib.request.Request(
        _ENDPOINT, data=body, method="POST",
        headers={"user-agent": f"frothy-local/{IMAGE_REV[:12]}"},
    )
    try:
        urllib.request.urlopen(req, timeout=4).read()
    except Exception:
        pass  # silent by contract


def begin(args, source, hours, ndom=None):
    """Called once from local_run.main after the plan is resolved.
    Prints the transparency line either way and sends run_started."""
    _state["enabled"] = not opted_out(args.no_telemetry)
    _state["t0"] = time.monotonic()
    if not _state["enabled"]:
        print("telemetry: off", file=sys.stderr)
        return
    print("telemetry: on — anonymous run stats only (image version, outcome, "
          "timings; never your config or logs). Disable with --no-telemetry "
          "or DO_NOT_TRACK=1.", file=sys.stderr)
    _state["anon"] = _install_id(args.out)
    _state["base"] = {
        "image_rev": IMAGE_REV, "model_id": MODEL_ID,
        "source": source, "hours": hours,
        **({"ndom": ndom} if ndom else {}),
        "cpus": os.cpu_count(),
    }
    # background: startup must not wait on the network
    threading.Thread(target=_post, args=("run_started", dict(_state["base"])),
                     daemon=True).start()


def finish(outcome, stage=None, crash_class=None, class_kind=None):
    """run_completed / run_failed, sent synchronously (the run is over;
    the 4 s timeout is the worst case). Idempotent — first call wins."""
    if not _state["enabled"] or _state["sent_final"]:
        return
    _state["sent_final"] = True
    props = dict(_state["base"])
    props["dur_s"] = int(time.monotonic() - _state["t0"])
    if stage:
        props["stage"] = stage
    if crash_class:
        props["crash_class"] = crash_class
    if class_kind:
        props["class_kind"] = class_kind
    _post("run_completed" if outcome == "ok" else "run_failed", props)


def classify_failure(logfile, workdir):
    """Coarse crash class via the crash classifier (worker/crash_classify
    .py, present in this image). Used twice: a better hint for the USER on
    stderr, and a class token in telemetry — never the logs themselves."""
    try:
        worker_dir = os.environ.get("WORKER_DIR") or str(Path(__file__).resolve().parent.parent / "worker")
        if worker_dir not in sys.path:
            sys.path.insert(0, worker_dir)
        from crash_classify import classify  # noqa: PLC0415
        tail = ""
        try:
            tail = Path(logfile).read_text(errors="replace")[-8000:]
        except OSError:
            pass
        return classify(tail, Path(workdir))
    except Exception:
        return {"crash_class": "unknown", "class_kind": "infra", "evidence": ""}
