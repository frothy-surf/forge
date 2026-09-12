#!/usr/bin/env python3
"""Local one-shot run for forge bundles — the public image's entrypoint
No hosted service, no job worker, no
uploads: mount a bundle, mount WPS_GEOG, get frothy-style output.

  docker run --rm \\
    -v $(pwd)/my-model:/bundle \\
    -v $(pwd)/WPS_GEOG:/geog \\
    -v $(pwd)/out:/out \\
    <image> [--hours 24] [--date YYYY-MM-DD --cycle 12]

Bundle = a forge export (namelist.wps, namelist.input, region.json,
product.json). geo_em files are built on first run with geogrid against
the mounted WPS_GEOG and cached in /out/work; boundary data follows the
bundle's region.json boundary_source — GFS 0.25° from NOMADS (with an S3
archive fallback), or HRRR/RRFS via idx-subset from their S3 buckets.
Products (wind JSON/PNG per product.json rect) land in /out/wind alongside
the raw wrfwind/wrfout NetCDF.

Differences from the hosted runner (runner.py main): the window starts at
the forcing cycle (no local-day logic), no checkpoints/restarts, no nest
windows, no GeoPolar SST enrichment, and geog_data_res defaults to the
standard WPS datasets — the tuned frothy extras (SRTM 30 m topo,
WorldCover landuse) are dataset installs, not code, so a stock WPS_GEOG
download just works.
"""
import argparse
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_telemetry  # noqa: E402  (same bin/ directory in the image)
import runner  # noqa: E402
from runner import (  # noqa: E402
    BASEDIR, VTABLES, check_decomposition, download_gfs, download_hrrr,
    download_rrfs, edit_namelists, expect, force_sfcp_to_sfcp, grib_cache_dir,
    link_gribs, log, mpi_prefix, parallel_layout, region_config, run,
    set_metgrid_levels, stage_workdir, summarize_timing, wrf_environment,
    _replace_namelist_entry,
)

WORKER_DIR = Path(os.environ.get("WORKER_DIR", str(BASEDIR / "worker")))

# ------------------------------------------------------------- status page
# Terminal heartbeats + a tiny local web GUI. Headless stays the default
# posture: the server binds inside the container and only exists for you if
# you publish the port (docker run -p 8760:8760). --no-serve disables it.

STATUS = {"stage": "starting", "detail": "", "sim_time": None, "pct": None,
          "rate": None, "eta_s": None, "window": None}
_STATUS_LOCK = threading.Lock()
_CURRENT_LOG = {"path": None}


def set_status(**kw):
    with _STATUS_LOCK:
        STATUS.update(kw)


_STATUS_HTML = """<!doctype html><meta charset=utf-8>
<title>frothy forge run</title>
<style>
 body{background:#0d0d0d;color:#f2f1ec;font:14px/1.5 system-ui,sans-serif;
      max-width:760px;margin:40px auto;padding:0 16px}
 h1{font-size:16px} .dim{color:#898781}
 .bar{height:14px;background:#242423;border-radius:7px;overflow:hidden;margin:12px 0}
 .fill{height:100%;background:#3987e5;width:0%;transition:width .5s}
 pre{background:#1a1a19;border:1px solid rgba(255,255,255,.12);border-radius:8px;
     padding:10px;font-size:11px;overflow-x:auto;white-space:pre-wrap}
 .failed .fill{background:#e05c5c}
</style>
<h1>frothy forge <span class=dim id=stage>…</span></h1>
<div class=bar><div class=fill id=fill></div></div>
<div class=dim id=meta></div>
<pre id=log></pre>
<script>
async function tick(){
 try{
  const s=await (await fetch('/status.json')).json();
  document.getElementById('stage').textContent=s.stage+(s.detail?' — '+s.detail:'');
  document.body.className=s.stage==='failed'?'failed':'';
  document.getElementById('fill').style.width=(s.pct??(s.stage==='complete'?100:2))+'%';
  let m=[];
  if(s.sim_time)m.push('model time '+s.sim_time+'Z');
  if(s.pct!=null)m.push(s.pct+'%');
  if(s.rate)m.push(s.rate+'\u00d7 realtime');
  if(s.eta_s)m.push('~'+Math.round(s.eta_s/60)+' min left');
  document.getElementById('meta').textContent=m.join(' \u00b7 ');
  document.getElementById('log').textContent=(s.log||[]).join('\n');
 }catch(e){}
 setTimeout(tick,3000);
}
tick();
</script>"""


class _StatusHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/status.json"):
            with _STATUS_LOCK:
                data = dict(STATUS)
            lp = _CURRENT_LOG["path"]
            if lp and Path(lp).exists():
                try:
                    data["log"] = Path(lp).read_text(errors="replace").splitlines()[-25:]
                except OSError:
                    pass
            body = json.dumps(data).encode()
            ctype = "application/json"
        else:
            body = _STATUS_HTML.encode()
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_status_server(port):
    try:
        srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), _StatusHandler)
    except OSError as e:
        log(f"status server not started ({e}) — progress continues in this terminal")
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"status page on container port {port} — add `-p {port}:{port}` to docker run "
        f"and open http://localhost:{port}")


def _rsl_tail(path, nbytes=200_000):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            return f.read().decode(errors="replace")
    except OSError:
        return ""


_TIMING_RE = re.compile(r"time (\d{4}-\d{2}-\d{2}_\d{2}:\d{2}:\d{2}) on domain\s+1:")


def run_wrf_with_progress(cmd, workdir, logfile, env, window):
    """wrf.exe with a heartbeat: parse rsl timing lines into sim-time
    progress, print every ~30 s, feed the status page continuously."""
    start, end = window
    total = (end - start).total_seconds()
    t0 = time.time()
    rsl = workdir / "rsl.out.0000"
    last_print = 0.0
    with open(logfile, "w") as out:
        proc = subprocess.Popen(cmd, cwd=workdir, stdout=out,
                                stderr=subprocess.STDOUT, env=env)
        while proc.poll() is None:
            time.sleep(5)
            text = _rsl_tail(rsl) if rsl.exists() else _rsl_tail(logfile)
            sim = None
            for m in _TIMING_RE.finditer(text):
                sim = m.group(1)
            if not sim:
                continue
            try:
                simdt = datetime.strptime(sim, "%Y-%m-%d_%H:%M:%S")
            except ValueError:
                continue
            done = (simdt - start).total_seconds()
            wall = time.time() - t0
            pct = max(0.0, min(100.0, done / total * 100))
            rate = done / wall if wall > 5 else None
            eta = (total - done) / rate if rate and rate > 0 else None
            set_status(stage="wrf", sim_time=sim, pct=round(pct, 1),
                       rate=round(rate, 1) if rate else None,
                       eta_s=int(eta) if eta is not None else None)
            if time.time() - last_print >= 30:
                last_print = time.time()
                msg = f"wrf: {sim}Z — {pct:.0f}% of the window"
                if rate:
                    msg += f", {rate:.1f}x realtime"
                if eta is not None:
                    msg += f", ~{max(1, int(eta // 60))} min left"
                log(msg)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)


def tail(path, n, label=None):
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return
    print(f"--- {label or Path(path).name} (last {min(n, len(lines))} lines):", file=sys.stderr)
    for line in lines[-n:]:
        print("  " + line, file=sys.stderr)


def stage(name, cmd, workdir, logfile, pattern=None, env=None, extra_logs=(), rsl=False,
          progress=None):
    """Run one pipeline stage; on failure show the relevant log tails
    instead of a python traceback — the user's error is in Fortran, not
    in this script. `rsl` marks MPI stages whose success line lands in
    rsl.out.0000 — never inferred from the file's presence, because a
    stale rsl from an earlier real/wrf attempt would poison the check."""
    ok = True
    set_status(stage=name, detail="running")
    _CURRENT_LOG["path"] = str(logfile)
    try:
        if progress:
            run_wrf_with_progress(cmd, workdir, logfile, env, progress)
        else:
            run(cmd, workdir, logfile, env=env)
    except subprocess.CalledProcessError:
        ok = False
    if ok and pattern:
        rsl_file = workdir / "rsl.out.0000"
        target = rsl_file if rsl and rsl_file.exists() else logfile
        try:
            expect(pattern, target)
        except Exception:
            ok = False
    if ok:
        set_status(detail="done")
        return
    set_status(stage="failed", detail=name)
    tail(logfile, 40)
    for pat in extra_logs:
        for lf in sorted(workdir.glob(pat)):
            tail(lf, 15)
    # the crash classifier doubles as a user hint (cfl → "your time step",
    # not just a Fortran tail) and as the coarse class token telemetry sends
    info = local_telemetry.classify_failure(logfile, workdir)
    if info.get("crash_class") not in (None, "unknown"):
        log(f"failure class: {info['crash_class']} ({info['class_kind']})"
            + (f" — {info['evidence']}" if info.get("evidence") else ""))
    local_telemetry.finish("failed", stage=name,
                           crash_class=info.get("crash_class"),
                           class_kind=info.get("class_kind"))
    sys.exit(
        f"{name} failed — full logs in {workdir / 'LOG'} and {workdir} "
        f"(see forge.frothy.surf/run for common causes)"
    )


# Per-source cycle grid and publish lag: HRRR inits hourly and publishes fast;
# GFS/RRFS run the synoptic four with a longer tail. ERA5 is a reanalysis:
# any past hour is an "init", and the lag is the ~6-day ERA5T embargo.
CYCLE_GRID_H = {"gfs": 6, "hrrr": 1, "rrfs": 6, "era5": 1}
PUBLISH_LAG_H = {"gfs": 4, "hrrr": 2, "rrfs": 4,
                 "era5": 24 * runner.ERA5T_LAG_DAYS}


def latest_cycle(source="gfs", now=None):
    """Newest cycle the source should have fully published. For ERA5 that
    is the newest reanalysis hour outside the embargo — default a hindcast
    to yesterday-minus-embargo at 00 UTC so a no-args run covers a clean
    local day rather than ending mid-afternoon."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    ready = now - timedelta(hours=PUBLISH_LAG_H[source])
    if source == "era5":
        return ready.replace(hour=0, minute=0, second=0, microsecond=0)
    grid = CYCLE_GRID_H[source]
    return ready.replace(hour=(ready.hour // grid) * grid,
                         minute=0, second=0, microsecond=0)


def horizon_cap(source, cycle_hour):
    """How far this source's cycle publishes boundary data, in forecast
    hours — the same limits runner.py main clamps hosted runs to: GFS f120,
    HRRR f48 on synoptic cycles (f18 otherwise), RRFS f84/f18. ERA5 has no
    forecast horizon at all — the cap is a sanity bound on one container
    run; the ERA5T embargo itself is enforced at download time."""
    if source == "hrrr":
        return 48 if cycle_hour in (0, 6, 12, 18) else 18
    if source == "rrfs":
        return 84 if cycle_hour in (0, 6, 12, 18) else 18
    if source == "era5":
        return 168
    return 120


def stamp(workdir, fname, section, key, val):
    p = workdir / fname
    text, matched = _replace_namelist_entry(p.read_text(), key, val)
    if not matched:
        # key absent from the bundle's namelist — append inside the section
        text = text.replace(f"&{section}", f"&{section}\n {key} = {val}", 1)
    p.write_text(text)


def stamp_wps(workdir, key, val):
    stamp(workdir, "namelist.wps", "geogrid", key, val)


def ndom_of(workdir):
    text = (workdir / "namelist.wps").read_text()
    ints = runner._namelist_ints(text, "max_dom")
    return ints[0] if ints else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bundle", default="/bundle", help="forge bundle directory")
    ap.add_argument("--geog", default="/geog", help="WPS_GEOG directory (UCAR download)")
    ap.add_argument("--out", default="/out", help="output directory")
    ap.add_argument("--date", default=None, help="cycle date YYYY-MM-DD (default: newest available)")
    ap.add_argument("--cycle", type=int, default=None,
                    help="cycle init hour, UTC (GFS/RRFS: 0/6/12/18; HRRR: 0-23)")
    ap.add_argument("--hours", type=int, default=None,
                    help="forecast length (default: region.json forecast_hours, "
                         "capped at the source's boundary-data horizon)")
    ap.add_argument("--offset", type=int, default=0,
                    help="start the window N hours after the cycle (uses the cycle's later "
                         "forecast hours — day-2 windows, or fast-forwarding to a crash)")
    ap.add_argument("--geog-res", default="default",
                    help="geog_data_res stamped into namelist.wps for geogrid; "
                         "'keep' preserves the bundle's values (needs the matching datasets "
                         "under /geog and the bundle's own GEOGRID.TBL)")
    ap.add_argument("--stop-after", choices=["geogrid", "ungrib", "metgrid", "real"],
                    help="run the pipeline only through this stage (debugging)")
    ap.add_argument("--no-serve", action="store_true",
                    help="disable the local status page (default: serves on container port 8760)")
    ap.add_argument("--no-telemetry", action="store_true",
                    help="disable anonymous run telemetry (image version, outcome, timings — "
                         "never your config or logs; BLIP_TELEMETRY=0 and DO_NOT_TRACK=1 also work)")
    ap.add_argument("--dry-run", action="store_true",
                    help="stage + edit namelists + print the plan, then stop before downloads/executables")
    args = ap.parse_args()

    # which build is this? (support threads and telemetry both need it)
    log(f"frothy-forge image {local_telemetry.IMAGE_REV} ({local_telemetry.MODEL_ID})")

    bundle = Path(args.bundle)
    out = Path(args.out)
    missing = [f for f in ("namelist.wps", "namelist.input", "region.json") if not (bundle / f).exists()]
    if missing:
        sys.exit(f"bundle at {bundle} is missing {', '.join(missing)} — export it from forge.frothy.surf")

    # image defaults <- bundle region.json. The forge image ships no
    # regions/ directory (the hosted service's region configs stay with it), so
    # the bundle is the only region source.
    cfg = region_config(None, config_dir=bundle)
    # The bundle's boundary_source drives the whole forcing path — Vtable,
    # fetcher, cycle grid, horizon; the wrong table would ungrib an HRRR
    # bundle silently wrong.
    source = str(cfg.get("boundary_source", "gfs")).lower()
    if source not in VTABLES:
        sys.exit(f"bundle region.json boundary_source '{source}' is not supported "
                 f"by this image ({' | '.join(sorted(VTABLES))})")
    interval = int(cfg.get("boundary_interval_hours", 3))

    if args.cycle is not None and args.cycle % CYCLE_GRID_H[source]:
        sys.exit(f"--cycle {args.cycle} is not a {source.upper()} init hour "
                 f"(every {CYCLE_GRID_H[source]} h)")
    if args.date:
        cycle_dt = datetime.strptime(args.date, "%Y-%m-%d").replace(hour=args.cycle or 0)
    else:
        cycle_dt = latest_cycle(source)
        if args.cycle is not None:
            cycle_dt = cycle_dt.replace(hour=args.cycle)

    cap = horizon_cap(source, cycle_dt.hour)
    hours_len = int(args.hours or cfg.get("forecast_hours", 24))
    if hours_len > cap:
        log(f"window {hours_len}h capped at {cap}h — {source.upper()} t{cycle_dt:%H}z "
            "publishes no boundary data beyond that")
        hours_len = cap
    if hours_len % interval:
        rounded = hours_len + interval - (hours_len % interval)
        if rounded > cap:  # can't round past the boundary data; shorten instead
            rounded = cap - (cap % interval)
        log(f"window {hours_len}h rounded to {rounded}h (boundary data arrives every {interval}h)")
        hours_len = rounded
    offset = max(0, args.offset)
    if offset + hours_len > cap:
        sys.exit(f"--offset {offset} + --hours {hours_len} exceeds {source.upper()} "
                 f"t{cycle_dt:%H}z's f{cap} boundary data")
    start = cycle_dt + timedelta(hours=offset)
    end = start + timedelta(hours=hours_len)
    hours = list(range(offset, offset + hours_len + 1, interval))

    workdir = out / "work"
    logdir = workdir / "LOG"
    logdir.mkdir(parents=True, exist_ok=True)
    log(f"bundle {bundle} → window {start:%Y-%m-%d_%H}Z..{end:%Y-%m-%d_%H}Z "
        f"({source.upper()} {cycle_dt:%Y%m%d %H}Z, f{hours[0]:03d}-f{hours[-1]:03d})")
    set_status(window=[f"{start:%Y-%m-%d_%H:%M}", f"{end:%Y-%m-%d_%H:%M}"])
    if not args.no_serve and not args.dry_run:
        start_status_server(int(os.environ.get("FORGE_STATUS_PORT", "8760")))

    # everything region-specific comes from the bundle: both namelists,
    # wind_io.txt when the model routes iofields, GEOGRID.TBL for --geog-res
    # keep. No image fallback exists here (see region_config above).
    stage_workdir(None, workdir, config_dir=bundle, boundary_source=source)

    # geogrid runs against the user's stock WPS_GEOG: stock table, mounted
    # path, and (unless told otherwise) standard dataset resolutions.
    # --geog-res keep preserves BOTH the bundle's res tokens and the staged
    # (bundle-or-region) GEOGRID.TBL — custom tokens like SRTM/worldcover
    # only exist in the custom table, so the two must travel together.
    ndom = ndom_of(workdir)
    stamp_wps(workdir, "geog_data_path", f"'{args.geog}'")
    stamp_wps(workdir, "opt_geogrid_tbl_path", "'./'")
    if args.geog_res != "keep":
        shutil.copy(BASEDIR / "tables" / "GEOGRID.TBL.ARW", workdir / "GEOGRID.TBL")
        stamp_wps(workdir, "geog_data_res", ", ".join([f"'{args.geog_res}'"] * ndom))
        # the stock table's default landuse is MODIS 20-class + lakes (21
        # categories); a bundle tuned for another source (frothy's USGS-28)
        # must follow the data it's actually getting or real.exe aborts on
        # the NUM_LAND_CAT dimension
        stamp(workdir, "namelist.input", "physics", "num_land_cat", "21,")
    elif not (workdir / "GEOGRID.TBL").exists():
        sys.exit("--geog-res keep needs a GEOGRID.TBL in the bundle: the custom "
                 "dataset tokens in namelist.wps only resolve through the table that "
                 "defines them, and this image ships only the stock WPS table")
    else:
        log("geog-res keep: bundle geog_data_res + bundle GEOGRID.TBL (custom datasets)")

    edit_namelists(workdir, start, end, ndom, interval)
    if source == "rrfs":
        force_sfcp_to_sfcp(workdir)

    if args.dry_run:
        log(f"dry run: workdir staged at {workdir}; {ndom} domain(s); "
            f"would fetch {len(hours)} hour(s) of {source.upper()} data then run "
            f"geogrid → ungrib → avg_tsfc → metgrid → real → wrf → extract")
        return

    local_telemetry.begin(args, source, hours_len, ndom)

    need_geo = [i for i in range(1, ndom + 1) if not (workdir / f"geo_em.d{i:02d}.nc").exists()]
    if need_geo:
        if not Path(args.geog).is_dir():
            sys.exit(f"geo_em missing and no WPS_GEOG at {args.geog} — mount the UCAR geographical "
                     "dataset (https://www2.mmm.ucar.edu/wrf/users/download/get_sources_wps_geog.html)")
        log(f"geogrid: building geo_em for d{need_geo[0]:02d}..d{need_geo[-1]:02d} (first run only)")
        stage("geogrid", ["geogrid.exe"], workdir, logdir / "geogrid.out",
              "Successful completion", extra_logs=("geogrid.log*",))
    if args.stop_after == "geogrid":
        log("stopped after geogrid")
        return

    # honors BLIP_GRIB_CACHE like the hosted runner, so repeated local runs of
    # one cycle (or several bundles on one host) reuse the download
    grib_dir = grib_cache_dir(cfg, cycle_dt, source) or (workdir / "GRIB")
    if source == "hrrr":
        # coverage check inside needs geo_em.d01 — geogrid ran above
        download_hrrr(cfg, cycle_dt, hours, grib_dir, workdir)
    elif source == "rrfs":
        download_rrfs(cfg, cycle_dt, hours, grib_dir, workdir)
    elif source == "era5":
        # per-user CDS token (CDSAPI_KEY / mounted ~/.cdsapirc); requests
        # ride the Copernicus queue — runner logs progress while it waits
        runner.download_era5(cfg, cycle_dt, hours, grib_dir)
    else:
        download_gfs(cfg, cycle_dt, hours, grib_dir)
    link_gribs(grib_dir, workdir)

    ranks, threads, cpus = parallel_layout(cfg)
    ranks = check_decomposition(workdir, ranks, ndom)
    mpi, bound = mpi_prefix(ranks, threads, cfg)
    env = wrf_environment(cfg, threads, bound)
    log(f"parallel layout: {ranks} MPI rank(s) x {threads} OpenMP thread(s) across {cpus} CPU(s)")

    stage("ungrib", ["ungrib.exe"], workdir, logdir / "ungrib.out",
          "Successful completion", extra_logs=("ungrib.log*",))
    if args.stop_after == "ungrib":
        log("stopped after ungrib")
        return
    stage("avg_tsfc", ["avg_tsfc.exe"], workdir, logdir / "avg_tsfc.out")
    stage("metgrid", ["metgrid.exe"], workdir, logdir / "metgrid.out",
          "Successful completion", extra_logs=("metgrid.log*",))
    if args.stop_after == "metgrid":
        log("stopped after metgrid")
        return
    set_metgrid_levels(workdir)
    stage("real", mpi + ["real.exe"], workdir, logdir / "real.out",
          "SUCCESS COMPLETE REAL_EM INIT", env=env, extra_logs=("rsl.error.0000",), rsl=True)
    if args.stop_after == "real":
        log("stopped after real")
        return
    stage("wrf", mpi + ["wrf.exe"], workdir, logdir / "wrf.out",
          "SUCCESS COMPLETE WRF", env=env, extra_logs=("rsl.error.0000",), rsl=True,
          progress=(start, end))
    summarize_timing(workdir, logdir)

    # ---- products: per-domain extract_wind over the bundle's rects
    pcfg_path = bundle / "product.json"
    extracted = 0
    if pcfg_path.exists():
        pcfg = json.loads(pcfg_path.read_text())
        rects = pcfg.get("rects") or []
        by_dom = {}
        for r in rects:
            by_dom.setdefault(r.get("domain") or pcfg.get("domain") or "d01", []).append(r)
        for dom, dom_rects in sorted(by_dom.items()):
            chunks = sorted(workdir.glob(f"wrfwind_{dom}*")) or sorted(workdir.glob(f"wrfout_{dom}*"))
            static = workdir / f"geo_em.{dom}.nc"
            if not chunks or not static.exists():
                log(f"skipping {dom} products ({'no output' if not chunks else 'no geo_em'})")
                continue
            sliced = workdir / f"product_{dom}.json"
            sliced.write_text(json.dumps(dict(pcfg, rects=dom_rects)))
            r = subprocess.run(
                [sys.executable, str(WORKER_DIR / "extract_wind.py"),
                 "--config", str(sliced), "--out", str(out / "wind"),
                 "--static", str(static), "--append", *[str(c) for c in chunks]],
                capture_output=True, text=True)
            if r.returncode != 0:
                log(f"extraction failed for {dom}:\n" + (r.stdout or "")[-1200:] + (r.stderr or "")[-1200:])
            else:
                extracted += len(dom_rects)

    set_status(stage="complete", pct=100.0, detail="")
    local_telemetry.finish("ok")
    outputs = sorted(workdir.glob("wrfwind_*")) + sorted(workdir.glob("wrfout_*"))
    log(f"run complete: {len(outputs)} NetCDF output file(s) in {workdir}"
        + (f"; products for {extracted} rect(s) in {out / 'wind'}" if extracted else ""))


if __name__ == "__main__":
    main()
