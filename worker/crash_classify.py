#!/usr/bin/env python3
"""Failure classification for hosted runs.

Splits a failed run into 'model' (WRF rejected a configuration our
validation passed — refund within the tier allowance, BLOCK the config,
Sentry) versus 'infra' (our problem — always refund, never block). The
hosting service treats anything unclassified as infra, so the conservative
direction here is: only claim 'model' on strong evidence.

Evidence: the runner/exception text plus the tail of any rsl.error.* files
in the workdir (WRF's real errors land there, not on stdout).

Optional Sentry: if SENTRY_DSN is set and sentry_sdk is importable, model
crashes are reported with fingerprint = crash_class, so they group by class.
"""

import os
import re
from pathlib import Path

# Ordered: first match wins. (pattern, class, kind)
_RULES = [
    # -- infra first: an OOM-killed wrf.exe can also print CFL noise
    (r"No space left on device|OSError: \[Errno 28\]", "disk_full", "infra"),
    (r"Cannot allocate memory|MemoryError|Out of memory|oom-kill", "oom", "infra"),
    (r"URLError|HTTPError|Connection(Reset|Refused)|timed out|Temporary failure in name",
     "network", "infra"),
    (r"geo_em\.d0\d\.nc.*(missing|No such file)|static/.*missing", "static_missing", "infra"),
    # -- model classes: WRF's classic self-inflicted deaths
    # WRF's actual instability messages ONLY — a bare "cfl =" or "w_damping"
    # matches namelist echoes (a logged target_cfl override is not a crash)
    (r"points exceeded cfl|CFL\s*(violation|exceeded)|Maximum W-CFL|w-cfl exceeded", "cfl", "model"),
    # A segfault in real.exe (initialization, before any time-stepping) is
    # not a user config problem: the validator passed it, metgrid accepted it,
    # and real is walking OUR tables/physics init (a lake-model nest init can
    # segfault on a template that validated cleanly). Only a
    # wrf.exe segfault — mid-integration — is the model-class memory/nesting
    # case this rule was written for. real_segfault stays 'infra' until the
    # validator can actually predict it.
    (r"real\.exe[\s\S]{0,4000}(Segmentation fault|SIGSEGV|signal 11)", "real_segfault", "infra"),
    (r"Segmentation fault|SIGSEGV|signal 11", "segfault", "model"),
    (r"NaN|NAN detected|Floating.?point exception|denormal", "numerical_blowup", "model"),
    (r"real\.exe.*(error|exited [1-9])|not enough eta levels|p_top.*(too low|below)|"
     r"mismatch.*(landmask|num_land_cat)|non-monotonic.*eta_levels|"
     r"FATAL CALLED FROM FILE:.*(module_initialize_real|<stdin>)", "real_rejection", "model"),
    (r"metgrid\.exe.*exited [1-9]|ungrib\.exe.*exited [1-9]", "preproc_failure", "model"),
    (r"nest.*(outside|beyond).*parent|invalid.*(i_parent_start|j_parent_start)",
     "nesting_error", "model"),
]


def _rsl_tail(workdir, max_bytes=16384):
    """Last chunk of the newest rsl.error.* — where WRF actually says why."""
    try:
        rsls = sorted(Path(workdir).glob("rsl.error.*"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        if not rsls:
            return ""
        data = rsls[0].read_bytes()
        return data[-max_bytes:].decode("utf-8", "replace")
    except OSError:
        return ""


# A wrf.exe segfault AFTER this many clean integration steps is not the
# user's config: WRF dies on a bad namelist numerically (CFL, NaN) and
# noisily, not with a silent SIGSEGV an hour in. Late segfaults are our
# build/runtime/host — infra, never a config block.
LATE_SEGFAULT_MIN_STEPS = 500


def _integration_steps(workdir):
    try:
        rsls = sorted(Path(workdir).glob("rsl.error.*"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        if not rsls:
            return 0
        return sum(1 for line in rsls[0].open(errors="replace") if "Timing for main" in line)
    except OSError:
        return 0


def classify(error_text, workdir=None):
    """→ {"crash_class": str, "class_kind": "model"|"infra", "evidence": str}.

    Unmatched failures return class 'unknown' kind 'infra': never block a
    user's config on evidence we can't name.
    """
    corpus = error_text or ""
    if workdir:
        corpus = corpus + "\n" + _rsl_tail(workdir)
        steps = _integration_steps(workdir)
        if steps >= LATE_SEGFAULT_MIN_STEPS and re.search(
                r"Segmentation fault|SIGSEGV|signal 11|exit status 139", corpus, re.IGNORECASE) \
                and not re.search(r"cfl|NaN|Floating.?point", corpus, re.IGNORECASE):
            return {"crash_class": "late_segfault", "class_kind": "infra",
                    "evidence": f"SIGSEGV after {steps} clean integration steps (no CFL/NaN precursor)"}
    for pattern, cls, kind in _RULES:
        m = re.search(pattern, corpus, re.IGNORECASE)
        if m:
            start = max(0, m.start() - 120)
            return {"crash_class": cls, "class_kind": kind,
                    "evidence": corpus[start:m.end() + 120].strip()}
    return {"crash_class": "unknown", "class_kind": "infra", "evidence": ""}


def report_crash(job, result, error_text):
    """Best-effort Sentry event for model-class crashes; silent otherwise."""
    if result.get("class_kind") != "model" or not os.environ.get("SENTRY_DSN"):
        return
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=os.environ["SENTRY_DSN"],
                        release=os.environ.get("BLIP_IMAGE_REV", "dev"),
                        default_integrations=False)
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("crash_class", result["crash_class"])
            scope.set_tag("job_id", str(job.get("id")))
            scope.set_tag("owner", str(job.get("owner") or ""))
            scope.set_tag("config_hash", str(job.get("config_hash") or ""))
            scope.fingerprint = [result["crash_class"]]
            scope.set_extra("evidence", result.get("evidence", ""))
            scope.set_extra("error_tail", (error_text or "")[-4000:])
            sentry_sdk.capture_message(f"hosted run crash: {result['crash_class']}", "error")
        sentry_sdk.flush(timeout=5)
    except Exception:  # noqa: BLE001 — reporting must never mask the failure
        pass
