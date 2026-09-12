#!/usr/bin/env python3
"""WRF run orchestration for frothy.

Computes the
forecast window (local midnight to midnight for the region's timezone),
downloads a GFS 0.25-degree subregion from NOMADS, then runs
ungrib -> avg_tsfc -> metgrid -> real -> wrf in a work directory.

Environment / arguments:
  runner.py [--region PNW] [--start-day 0] [--cycle 0] [--workdir DIR]

Expects (from the image layout, overridable via env):
  BLIP_BASEDIR   /opt/blip     bin/, tables/, wrf_run/, regions/<REGION>/
  geo_em.d0*.nc  present in the workdir (staged by the caller)

GRIB subregion bounds and timezone are read from regions/<REGION>/region.json
if present, else defaults for PNW are used.
"""

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASEDIR = Path(os.environ.get("BLIP_BASEDIR", "/opt/blip"))

DEFAULTS = {
    "timezone": "America/Los_Angeles",
    "grib": {"left_lon": -132, "right_lon": -113, "bottom_lat": 42, "top_lat": 55},
    "forecast_hours": 72,
    "boundary_interval_hours": 3,
    "domains": 2,
}

FILTER_URLS = [
    "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl",
    "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25b.pl",
]


def log(msg):
    print(f"[runner {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run(cmd, cwd, logfile=None, env=None):
    log(f"exec: {' '.join(map(str, cmd))}")
    out = open(logfile, "w") if logfile else None
    try:
        subprocess.run(cmd, cwd=cwd, check=True, stdout=out or sys.stdout,
                       stderr=subprocess.STDOUT, env=env)
    finally:
        if out:
            out.close()


def expect(pattern, path):
    text = Path(path).read_text(errors="replace")
    if not re.search(pattern, text):
        tail = "\n".join(text.splitlines()[-25:])
        raise RuntimeError(f"'{pattern}' not found in {path}; tail:\n{tail}")


# Keys that must match the prebuilt geo_em statics byte-for-byte in grid
# terms. Changing them per job without a matching static set produces
# metgrid/real failures (or worse, silently wrong terrain), so job-level
# overrides refuse them. Geometry changes go through a static-set variant:
# run geogrid, upload geo_em + templates under a new name, point the model
# row's config at it.
GEOMETRY_KEYS = {
    "max_dom", "e_we", "e_sn", "dx", "dy", "ref_lat", "ref_lon", "ref_x", "ref_y",
    "truelat1", "truelat2", "stand_lon", "pole_lat", "pole_lon", "map_proj",
    "parent_id", "parent_grid_ratio", "i_parent_start", "j_parent_start",
    "num_land_cat", "geog_data_res",
}
# region.json keys a job may override (pure runtime behavior). The parallel
# layout keys change how the run is spread over CPUs, not what it computes —
# except that MPI decomposition perturbs results at roundoff level, which is
# why they ride the config fingerprint and cold-start their own checkpoints.
RUNTIME_KEYS = {"forecast_hours", "boundary_interval_hours", "grib", "boundary_source",
                "mpi_ranks", "omp_threads", "mpi_bind", "omp_wait_policy",
                "omp_proc_bind", "omp_places"}

# How this image's wrf.exe was built (set from the Dockerfile's WRF_PARALLEL
# build arg): smpar = OpenMP only, dmpar = MPI only, dm+sm = both.
WRF_PARALLEL = os.environ.get("BLIP_WRF_PARALLEL", "smpar")

# Smallest patch dimension (grid cells per rank, per axis) worth handing to a
# rank: WRF needs room for its halos, and thin patches spend more time in
# communication than in physics.
MIN_PATCH_CELLS = 10


def _int_or_none(val):
    try:
        return int(val) if val not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _cgroup_cpu_limit():
    """The container's CPU limit in whole CPUs, or None when uncapped."""
    try:  # cgroup v2: "<quota> <period>", or "max <period>"
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if quota != "max":
            return int(quota) / int(period)
        return None
    except (OSError, ValueError):
        pass
    try:  # cgroup v1
        quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
        period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
        if quota > 0 and period > 0:
            return quota / period
    except (OSError, ValueError):
        pass
    return None


def available_cpus():
    """CPUs this container may actually use.

    os.cpu_count() reports the HOST's processors, not the container's share:
    on a 32-vCPU replica scheduled onto a 96-core machine it answers 96. Every
    thread past the quota is worse than useless — the cgroup throttles the
    whole process once the period's budget is spent, while OpenMP barriers
    wait on threads the scheduler has stopped running.
    """
    try:
        cpus = len(os.sched_getaffinity(0))
    except AttributeError:  # not Linux
        cpus = os.cpu_count() or 4
    quota = _cgroup_cpu_limit()
    if quota:
        cpus = min(cpus, max(1, int(quota)))
    host = os.cpu_count() or cpus
    if host != cpus:
        # Log the gap: threads sized from the host count would run inside a
        # smaller cpus budget and be throttled.
        log(f"host advertises {host} CPUs, this container may use {cpus} — "
            f"the previous build would have run {host} OpenMP threads here")
    if not quota and cpus > 16:
        log(f"no cgroup CPU quota visible; assuming all {cpus} CPUs are ours — "
            "set BLIP_CPUS to the replica's vCPU count if this is a shared host")
    return max(1, cpus)


def parallel_layout(cfg):
    """(mpi_ranks, omp_threads, usable_cpus) for this run.

    WRF scales best as a hybrid: MPI decomposes the grid across ranks, and a
    few OpenMP threads share each rank's patch. Pure OpenMP — all an smpar
    build can do — flattens out well below 32 threads.

    Every part is overridable, by env for a whole deploy or by job config for
    a single run, so layouts can be compared without rebuilding the image.
    Job config wins over env, so an A/B override on one job still takes effect
    on a service that already sets a deploy-wide default.
    """
    cpus = _int_or_none(os.environ.get("BLIP_CPUS")) or available_cpus()
    threads = (_int_or_none(cfg.get("omp_threads"))
               or _int_or_none(os.environ.get("BLIP_OMP_THREADS")))
    ranks = (_int_or_none(cfg.get("mpi_ranks"))
             or _int_or_none(os.environ.get("BLIP_MPI_RANKS")))

    if WRF_PARALLEL == "smpar":
        ranks, threads = 1, threads or cpus
    elif WRF_PARALLEL == "dmpar":
        threads = 1
        ranks = ranks or cpus
    else:  # dm+sm
        # Default: ONE rank over every CPU — exactly the old smpar behavior.
        # Measured on 8 CPUs, 1 rank x 8 threads beat every MPI split (2x4 by
        # 10%, 4x2 by 30%), so a split default is not assumed to win on
        # hardware nobody has measured. Multi-rank layouts are opt-in
        # (mpi_ranks/omp_threads via job config or BLIP_* env) until a
        # sweep on the target hardware shows a split winning.
        #
        # Pinning either side derives the other, so omp_threads=8 means
        # cpus/8 ranks, and mpi_ranks=4 means 4 ranks x cpus/4 threads.
        if threads and not ranks:
            ranks = max(1, cpus // threads)
        elif ranks and not threads:
            threads = max(1, cpus // ranks)
        elif not ranks and not threads:
            ranks, threads = 1, cpus

    if ranks * threads > cpus:
        fitted = max(1, cpus // threads)
        log(f"layout {ranks}x{threads} needs {ranks * threads} CPUs but only "
            f"{cpus} are usable; reducing to {fitted} rank(s)")
        ranks = fitted
    return ranks, threads, cpus


def rank_mesh(ranks):
    """WRF's rank grid: the divisor pair of `ranks` closest to square."""
    nx = 1
    for cand in range(1, math.isqrt(ranks) + 1):
        if ranks % cand == 0:
            nx = cand
    return nx, ranks // nx


def check_decomposition(workdir, ranks, ndom):
    """Back off a rank count that would slice a domain too thin.

    WRF's own complaint about this arrives as a crash minutes into wrf.exe,
    after WPS and real have already burned their time, so it is worth
    catching before the expensive part starts.
    """
    text = (workdir / "namelist.input").read_text()
    we = _namelist_ints(text, "e_we")[:ndom]
    sn = _namelist_ints(text, "e_sn")[:ndom]
    if not we or not sn:
        return ranks
    while ranks > 1:
        a, b = rank_mesh(ranks)
        # Either orientation is a legal mesh; WRF picks the one that suits
        # the domain's aspect, so judge by the better of the two.
        smallest = max(min(min(w // nx, s // ny) for w, s in zip(we, sn))
                       for nx, ny in ((a, b), (b, a)))
        if smallest >= MIN_PATCH_CELLS:
            return ranks
        log(f"{ranks} ranks would leave {smallest}-cell patches "
            f"(minimum {MIN_PATCH_CELLS}); halving")
        ranks //= 2
    return ranks


# WRF tiles each domain into OMP_NUM_THREADS row-strips (1D-Y). The OUTER
# domain's strips are the ones that matter: a 104-row d01 runs 32 threads
# at 3-row strips daily, including nests down to 46 rows (1.4-row strips)
# — thin NEST tiles are fine — and a 48-thread run of the same d01 gives
# 2-row strips. A 38-row d01 at 32 threads is 1-row outer strips, and
# wrf.exe segfaults on the first nest step. Floor = 2: every proven-working
# layout is untouched, 1-row outer strips are not allowed.
MIN_D01_TILE_ROWS = 2


def cap_threads_for_d01(workdir, threads):
    """Cap OpenMP threads so EVERY domain's row-strips stay >=
    MIN_D01_TILE_ROWS. Capping on d01 alone is not enough: an outer domain
    of 60 rows allows 30 threads, but a 55-row nest under it then tiles
    thinner than the floor and WRF segfaults at that nest's first tile
    pass. The binding constraint is the SMALLEST domain."""
    text = (workdir / "namelist.input").read_text()
    sn = _namelist_ints(text, "e_sn")
    ndom = (_namelist_ints(text, "max_dom") or [len(sn)])[0]
    rows = sn[:ndom] if sn else []
    if not rows or threads <= 1:
        return threads
    binding = min(rows)
    allowed = max(1, binding // MIN_D01_TILE_ROWS)
    if threads > allowed:
        log(f"{threads} OpenMP threads would tile the {binding}-row domain "
            f"(smallest of {rows}) into {max(1, binding // threads)}-row strips "
            f"(minimum {MIN_D01_TILE_ROWS}); using {allowed}")
        return allowed
    return threads


def _namelist_ints(text, key):
    m = re.search(rf"^\s*{key}\s*=\s*([^\n]*)", text, re.M)
    if not m:
        return []
    return [int(t) for t in (tok.strip() for tok in m.group(1).split(","))
            if t.isdigit()]


def wrf_environment(cfg, threads, bound):
    """Runtime environment for real.exe/wrf.exe.

    Job config beats deploy env beats the built-in default.
    """
    env = dict(os.environ)
    env["OMP_NUM_THREADS"] = str(threads)
    # Idle OpenMP threads spin at barriers by default, which bills CPU for
    # doing nothing on a usage-metered host. PASSIVE trades a little barrier
    # wake-up latency for that; flip it per deploy to measure which wins.
    defaults = {"OMP_WAIT_POLICY": "PASSIVE"}
    # Pinning threads is only safe once ranks own disjoint cores. Under
    # --bind-to none every rank sees the same place list, so binding would
    # stack all of their threads onto the same CPUs.
    if bound:
        defaults["OMP_PROC_BIND"] = "close"
        defaults["OMP_PLACES"] = "cores"
    for key, cfg_key in (("OMP_WAIT_POLICY", "omp_wait_policy"),
                         ("OMP_PROC_BIND", "omp_proc_bind"),
                         ("OMP_PLACES", "omp_places")):
        val = cfg.get(cfg_key) or os.environ.get(key) or defaults.get(key)
        if val:
            env[key] = str(val)
    return env


def mpi_prefix(ranks, threads, cfg):
    """(argv prefix, ranks_are_bound) for launching an MPI-built executable."""
    if WRF_PARALLEL not in ("dmpar", "dm+sm"):
        return [], False
    # The hosted-service container runs as root, which OpenMPI refuses to do
    # unprompted; the flag is inert for the unprivileged forge image.
    cmd = ["mpirun", "-np", str(ranks), "--allow-run-as-root"]
    bind = cfg.get("mpi_bind") or os.environ.get("BLIP_MPI_BIND") or "none"
    bound = bind != "none"
    if bound:
        cmd += ["--map-by", f"slot:PE={threads}", "--bind-to", bind]
    else:
        # Hosts that cap CPU by quota rather than by cpuset let the container
        # see every core, so pinning ranks to particular ones binds
        # them to CPUs the scheduler may never hand us. Off until measured.
        cmd += ["--bind-to", "none"]
    cmd += shlex.split(os.environ.get("BLIP_MPI_EXTRA_ARGS", ""))
    return cmd, bound


def summarize_timing(workdir, logdir):
    """Log WRF's own per-domain integration cost.

    This is the breakdown every layout or namelist experiment is trying to
    read, and MPI builds bury it in rsl.out.0000 instead of on stdout.
    """
    src = workdir / "rsl.out.0000"
    if not src.exists():
        src = logdir / "wrf.out"
    if not src.exists():
        return
    pattern = re.compile(r"Timing for main.*?on domain\s+(\d+):\s+([0-9.]+) elapsed")
    steps, secs = {}, {}
    for line in src.read_text(errors="replace").splitlines():
        m = pattern.search(line)
        if m:
            dom = int(m.group(1))
            steps[dom] = steps.get(dom, 0) + 1
            secs[dom] = secs.get(dom, 0.0) + float(m.group(2))
    for dom in sorted(steps):
        log(f"timing d{dom:02d}: {steps[dom]} steps, {secs[dom]:.0f}s "
            f"({secs[dom] / steps[dom]:.2f}s/step)")
    if secs:
        # A parent's step time already contains the nest steps run inside it,
        # so the outermost domain's total IS the integration total — summing
        # the domains would count the nests two or three times over.
        outer = min(secs)
        log(f"timing total: {secs[outer]:.0f}s integration (d{outer:02d} encloses "
            f"its nests; nested totals are a subset, not additive)")


def region_config(region, config_dir=None, overrides=None):
    """Effective region config: image defaults <- image region.json <-
    service-provided region.json (config_dir) <- job overrides.
    region=None skips the image layer: the public forge image ships no
    regions/ directory, so a bundle's region.json is the only source."""
    cfg = dict(DEFAULTS)
    for f in [BASEDIR / "regions" / region / "region.json" if region else None,
              Path(config_dir) / "region.json" if config_dir else None]:
        if f and f.exists():
            cfg.update(json.loads(f.read_text()))
    for key, val in (overrides or {}).items():
        if key in RUNTIME_KEYS:
            cfg[key] = val
    return cfg


def forecast_window(cfg, start_day, cycle, run_date=None):
    """Return (cycle_date_utc, [forecast hours], start_dt_utc, end_dt_utc).

    The run covers the region-local day from midnight — except when the
    forcing cycle's init falls after that midnight (the mid-day refresh
    cycles, e.g. 12z/18z against a 07Z local midnight). Backdating those to
    the previous day's cycle would force the run with data OLDER than the
    earlier cycle it replaces in the serving order; instead the refresh
    starts at the init itself and re-forecasts the rest of the day with the
    freshest model, extending the horizon by the same amount."""
    tz = ZoneInfo(cfg["timezone"])
    if run_date:
        y, m, d = map(int, run_date.split("-"))
        local_midnight = datetime(y, m, d, tzinfo=tz)
    else:
        local_midnight = (datetime.now(tz) + timedelta(days=start_day)).replace(
            hour=0, minute=0, second=0, microsecond=0)
    midnight_utc = local_midnight.astimezone(timezone.utc)
    cycle_dt = midnight_utc.replace(hour=cycle, minute=0)
    start_utc = max(midnight_utc, cycle_dt)
    first = int((start_utc - cycle_dt).total_seconds() // 3600)
    step = cfg["boundary_interval_hours"]
    hours = list(range(first, first + cfg["forecast_hours"] + 1, step))
    end_utc = start_utc + timedelta(hours=cfg["forecast_hours"])
    return cycle_dt, hours, start_utc, end_utc


# HRRR fields for Vtable.HRRR (= WPS Vtable.raphrrr): full 3D state on
# NATIVE (hybrid) levels from wrfnatf, plus surface / 2 m / 10 m / RUC
# 9-level soil from wrfprsf — the nat files carry no TSOIL or surface PRES.
# ungrib reads both files together; the Vtable's level types disambiguate.
# Instantaneous only ("acc" entries are accumulation duplicates).
def _want_hrrr_nat(var, level, timedesc):
    return ("acc" not in timedesc and level.endswith("hybrid level")
            and var in {"HGT", "PRES", "TMP", "SPFH", "UGRD", "VGRD",
                        "CLMR", "RWMR", "CIMIXR", "SNMR", "GRLE",
                        "NCONCD", "NCCICE", "SPNCR"})


def _want_hrrr_sfc(var, level, timedesc):
    if "acc" in timedesc:
        return False
    if "below ground" in level and var in {"TSOIL", "SOILW"}:
        return True
    if level == "surface" and var in {"PRES", "HGT", "TMP", "WEASD", "SNOD",
                                      "ICEC", "LAND", "CNWAT"}:
        return True
    if level == "2 m above ground" and var in {"TMP", "DPT", "RH", "SPFH"}:
        return True
    if level == "10 m above ground" and var in {"UGRD", "VGRD"}:
        return True
    return level == "mean sea level" and var in {"MSLMA", "PRMSL", "MSLET"}


def check_hrrr_coverage(workdir):
    """HRRR's CONUS grid tops out around 50.4N at PNW longitudes — it cannot
    force a domain that leaves its footprint (the stock 3-domain PNW d01
    reaches ~53.4N). Verify the OUTERMOST domain fits before spending hours
    of compute; metgrid would otherwise fail or extrapolate garbage."""
    from netCDF4 import Dataset
    from pyproj import CRS, Transformer

    crs = CRS.from_proj4("+proj=lcc +lat_1=38.5 +lat_2=38.5 +lat_0=38.5 "
                         "+lon_0=-97.5 +R=6371229 +units=m")
    tf = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    x0, y0 = tf.transform(-122.719528, 21.138123)  # HRRR grid origin
    nx, ny, d = 1799, 1059, 3000.0
    nc = Dataset(workdir / "geo_em.d01.nc")
    lats = nc.variables["XLAT_M"][0]
    lons = nc.variables["XLONG_M"][0]
    edge_lat = list(lats[0, :]) + list(lats[-1, :]) + list(lats[:, 0]) + list(lats[:, -1])
    edge_lon = list(lons[0, :]) + list(lons[-1, :]) + list(lons[:, 0]) + list(lons[:, -1])
    nc.close()
    margin = 2 * d  # metgrid needs source points beyond the boundary
    for lat, lon in zip(edge_lat, edge_lon):
        x, y = tf.transform(float(lon), float(lat))
        if not (x0 + margin <= x <= x0 + (nx - 1) * d - margin
                and y0 + margin <= y <= y0 + (ny - 1) * d - margin):
            raise RuntimeError(
                f"outermost domain edge ({lat:.2f}N, {lon:.2f}E) is outside HRRR "
                "coverage — HRRR-init needs a domain layout that fits the HRRR "
                "grid (see variants/PNW-hrrr: drop d01, 2 km becomes outermost)")


def _atomic_write(path, data):
    """Publish a download in one step.

    A worker sharing the cache must never see a half-written file, find it
    non-empty, and treat it as a complete download.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.part")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return path


# Boundary sources and the ungrib table each one stages as workdir/Vtable.
# HRRR forces from native hybrid levels (Vtable.raphrrr); RRFS's native
# product only ships on the huge North-America grid, so it forces from
# prslev + 2dfld (isobaric + RUC soil) via the RAP pressure table. ERA5
# is the ECMWF reanalysis (hindcasts): stock Vtable.ECMWF (pressure-level
# atmosphere + ECMWF 4-layer soil), included as tables/Vtable.ERA5.
VTABLES = {"gfs": "Vtable.GFS", "hrrr": "Vtable.HRRR", "rrfs": "Vtable.RRFS",
           "era5": "Vtable.ERA5"}


def _grib_box_hash(cfg):
    return hashlib.sha256(json.dumps(cfg["grib"], sort_keys=True).encode()).hexdigest()[:8]


def grib_cache_dir(cfg, cycle_dt, source):
    """Shared cache directory for this (source, region box, cycle), or None.

    Forcing files are immutable once published, so anything fetching the same
    cycle again can reuse them: a variant A/B on one date, a retried job, a
    second worker on the same host. It does NOT help across different forecast
    days — those are different cycles and genuinely different files, even
    though the GFS filenames look identical (the date rides the query, not the
    name). The box is part of the key because NOMADS crops server-side, so the
    same filename holds different bytes for a different region.
    """
    root = os.environ.get("BLIP_GRIB_CACHE")
    if not root:
        return None
    cache = Path(root) / f"{source}-{_grib_box_hash(cfg)}-{cycle_dt:%Y%m%d%H}"
    try:
        cache.mkdir(parents=True, exist_ok=True)
        os.utime(cache)  # keep-alive: eviction is by age of last use
    except OSError as e:  # noqa: BLE001 — a broken cache must not fail the run
        log(f"GRIB cache unusable ({e}); downloading into the workdir")
        return None
    prune_grib_cache(Path(root), cache)
    return cache


def prune_grib_cache(root, keep):
    """Evict cycles nobody has touched lately (default 7 days)."""
    days = float(os.environ.get("BLIP_GRIB_CACHE_DAYS", "7"))
    cutoff = time.time() - days * 86400
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    for d in entries:
        try:
            if d != keep and d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                log(f"GRIB cache: evicted {d.name} (unused {days:g}+ days)")
        except OSError:
            pass


# ---- shared read-through forcing cache ---------------------------------
# The per-host BLIP_GRIB_CACHE dedupes a cycle's forcing across jobs on one
# machine; this layer dedupes it across hosts through an object store. Keys
# mirror the local cache ({source}-{boxhash}-{cycle}); values are the files
# exactly as staged (GFS: NOMADS-cropped, HRRR/RRFS: idx-subset +
# wgrib2-cropped), so a hit skips the flaky upstream entirely — the first
# host to want a cycle fetches from NOAA, everyone after pulls from the
# store. Racing writers are benign (same-bytes PUTs). Every path fails
# open: no env, store down, 404 — the run proceeds against NOAA exactly as
# before. Retention is a lifecycle rule on the forcing/ prefix, not code.

def _forcing_r2_enabled():
    return (os.environ.get("BLIP_FORCING_R2", "1") != "0"
            and os.environ.get("CONTROL_PLANE_URL")
            and os.environ.get("WORKER_TOKEN"))


def _forcing_r2_url(cfg, source, cycle_dt, name):
    base = os.environ["CONTROL_PLANE_URL"].rstrip("/")
    return (f"{base}/store/forcing/{source}-{_grib_box_hash(cfg)}-"
            f"{cycle_dt:%Y%m%d%H}/{name}")


def _forcing_r2_headers():
    return {"Authorization": f"Bearer {os.environ['WORKER_TOKEN']}",
            "User-Agent": "frothy-worker/1.0"}


def forcing_r2_get(cfg, source, cycle_dt, name, target):
    """Stage `name` from the shared cache into `target`; False on any miss."""
    if not _forcing_r2_enabled():
        return False
    req = urllib.request.Request(_forcing_r2_url(cfg, source, cycle_dt, name),
                                 headers=_forcing_r2_headers())
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = resp.read()
        if len(data) < 10_000:
            raise IOError(f"suspiciously small cached object ({len(data)} b)")
        _atomic_write(target, data)
        log(f"{name}: from shared forcing cache")
        return True
    except urllib.error.HTTPError as e:
        if e.code != 404:
            log(f"shared forcing cache read failed ({e}); fetching upstream")
        return False
    except Exception as e:  # noqa: BLE001 - cache must never fail a run
        log(f"shared forcing cache read failed ({e}); fetching upstream")
        return False


# The store's upload path caps request bodies around 100 MB; a cropped HRRR
# wrfnatf hour (~97 MB) rides the line. Files this large only exist for
# hybrid-level sources, whose value is mostly in deduping the SMALL files
# anyway — skip rather than fail the upload.
R2_PUT_MAX_BYTES = 90_000_000


def forcing_r2_put(cfg, source, cycle_dt, name, path):
    if not _forcing_r2_enabled():
        return
    size = path.stat().st_size
    if size > R2_PUT_MAX_BYTES:
        log(f"{name}: {size / 1e6:.0f} MB exceeds the shared cache upload "
            "cap; not cached")
        return
    try:
        req = urllib.request.Request(_forcing_r2_url(cfg, source, cycle_dt, name),
                                     data=path.read_bytes(), method="PUT",
                                     headers=_forcing_r2_headers())
        with urllib.request.urlopen(req, timeout=600):
            pass
        log(f"{name}: uploaded to shared forcing cache")
    except Exception as e:  # noqa: BLE001 - cache must never fail a run
        log(f"shared forcing cache write failed ({e}); continuing")


def _crop_grib(path, g):
    """Crop CONUS-wide messages to the region's grib box with wgrib2
    (-small_grib). Measured on a wrfnatf hour: 372 MB -> 97 MB, and ungrib
    over the cropped set peaks at 0.6 GB in 74 s where the CONUS-wide set
    took 34 min and was OOM-killed on a 4 GB worker. Costs ~40 s per file.
    No-op when wgrib2 isn't installed."""
    if not shutil.which("wgrib2"):
        return
    cropped = path.with_suffix(".crop")
    r = subprocess.run(
        ["wgrib2", str(path), "-small_grib",
         f"{g['left_lon']}:{g['right_lon']}", f"{g['bottom_lat']}:{g['top_lat']}",
         str(cropped)],
        capture_output=True, text=True)
    if r.returncode == 0 and cropped.exists() and cropped.stat().st_size > 100_000:
        cropped.replace(path)
    else:
        cropped.unlink(missing_ok=True)
        log(f"wgrib2 crop failed ({r.returncode}); keeping full grid")


def download_hrrr(cfg, cycle_dt, hours, grib_dir, workdir):
    """HRRR boundary/initial conditions via idx-ranged subsetting
    (worker/hrrr_fetch.py). The S3 bucket is also the archive, so hindcasts
    fetch through the same path."""
    sys.path.insert(0, str(BASEDIR / "worker"))
    import hrrr_fetch

    check_hrrr_coverage(workdir)
    grib_dir.mkdir(parents=True, exist_ok=True)
    date = f"{cycle_dt:%Y-%m-%d}"
    for h in hours:
        for product, selector, floor in (("wrfnatf", _want_hrrr_nat, 200),
                                         ("wrfprsf", _want_hrrr_sfc, 20)):
            target = grib_dir / f"hrrr.t{cycle_dt:%H}z.{product}{h:03d}.grib2"
            if target.exists() and target.stat().st_size > 1_000_000:
                continue
            if forcing_r2_get(cfg, "hrrr", cycle_dt, target.name, target):
                continue
            log(f"download hrrr {product}{h:02d} (idx subset)")
            msgs = hrrr_fetch.fetch_messages(date, cycle_dt.hour, h, product,
                                             selector, log=log)
            if len(msgs) < floor:
                raise RuntimeError(
                    f"{product}{h:02d}: only {len(msgs)} messages matched — "
                    "inventory changed upstream?")
            tmp = target.with_name(f".{target.name}.{os.getpid()}.part")
            with open(tmp, "wb") as f:
                for _, _, body in msgs:
                    f.write(body)
            _crop_grib(tmp, cfg["grib"])
            os.replace(tmp, target)
            forcing_r2_put(cfg, "hrrr", cycle_dt, target.name, target)


# RRFS pressure-level atmosphere: 45 isobaric levels of the classic set.
# (RH rather than SPFH — Vtable.RAP.pressure.ncep derives moisture from RH.)
def _want_rrfs_prs(var, level, timedesc):
    return ("acc" not in timedesc and level.endswith(" mb")
            and var in {"HGT", "TMP", "RH", "UGRD", "VGRD"})


# RRFS keeps its surface/2 m/10 m/RUC-soil set in the 2dfld product, with
# the same variable names the HRRR wrfprsf subset uses (MSL is MSLET).
_want_rrfs_sfc = _want_hrrr_sfc

RRFS_ORIGINS = ["https://noaa-rrfs-pds.s3.amazonaws.com"]


def _rrfs_urls(cycle_dt, product, fh):
    name = f"rrfs.t{cycle_dt:%H}z.{product}.3km.f{fh:03d}.conus.grib2"
    return [f"{o}/rrfs_a/rrfs.{cycle_dt:%Y%m%d}/{cycle_dt:%H}/{name}"
            for o in RRFS_ORIGINS]


def download_rrfs(cfg, cycle_dt, hours, grib_dir, workdir):
    """RRFS boundary/initial conditions: isobaric atmosphere from prslev plus
    surface/soil from 2dfld, both on the SAME 1799x1059 3 km Lambert grid as
    HRRR (verified from live GRIB keys), so the HRRR
    coverage check applies verbatim. RRFS's native-level product only ships
    on the huge North-America grid (~2 GB/hour of selected messages), so
    this is the GFS-style pressure-level tradeoff at 3 km: ~330 MB per
    forecast hour before cropping, idx-subset like HRRR. Vtable.RRFS
    (= WPS Vtable.RAP.pressure.ncep) maps the isobaric set and the RUC
    9-level soil."""
    sys.path.insert(0, str(BASEDIR / "worker"))
    import hrrr_fetch

    check_hrrr_coverage(workdir)
    grib_dir.mkdir(parents=True, exist_ok=True)
    for h in hours:
        for product, selector, floor in (("prslev", _want_rrfs_prs, 150),
                                         ("2dfld", _want_rrfs_sfc, 20)):
            target = grib_dir / f"rrfs.t{cycle_dt:%H}z.{product}{h:03d}.grib2"
            if target.exists() and target.stat().st_size > 1_000_000:
                continue
            if forcing_r2_get(cfg, "rrfs", cycle_dt, target.name, target):
                continue
            log(f"download rrfs {product} f{h:03d} (idx subset)")
            msgs = hrrr_fetch.fetch_messages_from(
                _rrfs_urls(cycle_dt, product, h), selector, log=log,
                what=f"rrfs {product} f{h:03d}")
            if len(msgs) < floor:
                raise RuntimeError(
                    f"rrfs {product} f{h:03d}: only {len(msgs)} messages matched — "
                    "inventory changed upstream?")
            tmp = target.with_name(f".{target.name}.{os.getpid()}.part")
            with open(tmp, "wb") as f:
                for _, _, body in msgs:
                    f.write(body)
            _crop_grib(tmp, cfg["grib"])
            os.replace(tmp, target)
            forcing_r2_put(cfg, "rrfs", cycle_dt, target.name, target)


def download_gfs(cfg, cycle_dt, hours, grib_dir):
    grib_dir.mkdir(parents=True, exist_ok=True)
    g = cfg["grib"]
    base_query = {
        "dir": f"/gfs.{cycle_dt:%Y%m%d}/{cycle_dt:%H}/atmos",
        "all_var": "on", "all_lev": "on", "subregion": "",
        "leftlon": g["left_lon"], "rightlon": g["right_lon"],
        "bottomlat": g["bottom_lat"], "toplat": g["top_lat"],
    }
    for h in hours:
        for url, suffix in zip(FILTER_URLS, ["", "b"]):
            name = f"gfs.t{cycle_dt:%H}z.pgrb2{suffix}.0p25.f{h:03d}"
            target = grib_dir / name
            if target.exists() and target.stat().st_size > 100_000:
                continue
            if forcing_r2_get(cfg, "gfs", cycle_dt, name, target):
                continue
            query = dict(base_query, file=name)
            full = f"{url}?{urllib.parse.urlencode(query)}"
            for attempt in range(5):
                try:
                    log(f"download {name} (attempt {attempt + 1})")
                    with urllib.request.urlopen(full, timeout=180) as resp:
                        data = resp.read()
                    if len(data) < 10_000:
                        raise IOError(f"suspiciously small response ({len(data)} b)")
                    _atomic_write(target, data)
                    break
                except Exception as e:  # noqa: BLE001
                    log(f"  failed: {e}")
                    time.sleep(30 * (attempt + 1))
            else:
                # NOMADS retains ~10 days; fall back to the NOAA S3 archive
                # (full-globe files, no subregion filter — larger but complete)
                s3 = (f"https://noaa-gfs-bdp-pds.s3.amazonaws.com"
                      f"/gfs.{cycle_dt:%Y%m%d}/{cycle_dt:%H}/atmos/{name}")
                for attempt in range(3):
                    try:
                        log(f"download {name} from S3 archive (attempt {attempt + 1})")
                        with urllib.request.urlopen(s3, timeout=600) as resp:
                            data = resp.read()
                        if len(data) < 1_000_000:
                            raise IOError(f"suspiciously small S3 object ({len(data)} b)")
                        _atomic_write(target, data)
                        break
                    except Exception as e:  # noqa: BLE001
                        log(f"  S3 failed: {e}")
                        time.sleep(20 * (attempt + 1))
                else:
                    raise RuntimeError(f"could not download {name} from NOMADS or S3")
            forcing_r2_put(cfg, "gfs", cycle_dt, name, target)


# ---- ERA5 (Copernicus CDS) -------------------------------------------------
#
# The reanalysis source: per-USER credentials (a free CDS account's Personal
# Access Token, via CDSAPI_KEY or a mounted ~/.cdsapirc), requests go into
# Copernicus' queue, and data is ~6 days behind real time (the ERA5T
# embargo). Protocol per ecmwf-projects/datapi (the official client):
# POST {api}/retrieve/v1/processes/{dataset}/execution with {"inputs": ...}
# and a PRIVATE-TOKEN header, poll the job's monitor link until
# "successful", download the result asset. Implemented on stdlib urllib
# like every other fetcher in this file — no cdsapi dependency in the image.

CDS_API_URL = os.environ.get("CDS_API_URL", "https://cds.climate.copernicus.eu/api")
ERA5T_LAG_DAYS = 6  # preliminary ERA5T publishes ~5 days behind; one spare

ERA5_PL_DATASET = "reanalysis-era5-pressure-levels"
ERA5_SFC_DATASET = "reanalysis-era5-single-levels"
# The exact fields Vtable.ERA5 (stock WPS Vtable.ECMWF) maps: Z/T/U/V/RH on
# the 37 pressure levels, plus the surface set with ECMWF's 4-layer soil.
ERA5_PL_VARIABLES = ["geopotential", "temperature", "u_component_of_wind",
                     "v_component_of_wind", "relative_humidity"]
ERA5_PL_LEVELS = ["1", "2", "3", "5", "7", "10", "20", "30", "50", "70",
                  "100", "125", "150", "175", "200", "225", "250", "300",
                  "350", "400", "450", "500", "550", "600", "650", "700",
                  "750", "775", "800", "825", "850", "875", "900", "925",
                  "950", "975", "1000"]
ERA5_SFC_VARIABLES = [
    "10m_u_component_of_wind", "10m_v_component_of_wind",
    "2m_dewpoint_temperature", "2m_temperature",
    "geopotential",  # invariant surface Z -> SOILHGT (source terrain)
    "land_sea_mask", "mean_sea_level_pressure", "sea_ice_cover",
    "sea_surface_temperature", "skin_temperature", "snow_depth",
    "soil_temperature_level_1", "soil_temperature_level_2",
    "soil_temperature_level_3", "soil_temperature_level_4",
    "surface_pressure",
    "volumetric_soil_water_layer_1", "volumetric_soil_water_layer_2",
    "volumetric_soil_water_layer_3", "volumetric_soil_water_layer_4",
]


def cds_token():
    """The user's CDS Personal Access Token: CDSAPI_KEY, else ~/.cdsapirc
    (the file the official client uses, so existing setups just work)."""
    tok = os.environ.get("CDSAPI_KEY", "").strip()
    if tok:
        return tok
    rc = Path(os.environ.get("CDSAPI_RC", Path.home() / ".cdsapirc"))
    if rc.is_file():
        for line in rc.read_text().splitlines():
            if line.strip().lower().startswith("key:"):
                return line.split(":", 1)[1].strip()
    raise RuntimeError(
        "ERA5 needs your Copernicus CDS Personal Access Token: register free at "
        "https://cds.climate.copernicus.eu, accept the ERA5 licence once, then pass "
        "-e CDSAPI_KEY=<token> (or mount your ~/.cdsapirc)")


def _cds_json(url, token, payload=None, timeout=60):
    body = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST" if body else "GET",
                                 headers={"PRIVATE-TOKEN": token,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:500]
        except Exception:  # noqa: BLE001
            pass
        if e.code in (401, 403):
            raise RuntimeError(
                f"CDS rejected the token ({e.code}): check CDSAPI_KEY, and that the "
                f"ERA5 licence is accepted on your CDS account. {detail}") from e
        raise RuntimeError(f"CDS request failed ({e.code} at {url}): {detail}") from e


def _cds_link(doc, rel, fallback):
    for link in doc.get("links") or []:
        if link.get("rel") == rel and link.get("href"):
            return link["href"]
    return fallback


CDS_SAY_EVERY_S = 30.0

# CC-BY-4.0 attribution the Copernicus licence asks for on anything derived
# from ERA5; rides run_meta.json and the published wind manifest.
ERA5_ATTRIBUTION = (
    "Contains modified Copernicus Climate Change Service information {year} (ERA5). "
    "Neither the European Commission nor ECMWF is responsible for any use that may be "
    "made of the Copernicus information or data it contains.")


def cds_status_word(status):
    """CDS says 'accepted' for a request sitting in its queue."""
    return "queued" if status == "accepted" else status


def cds_qos_note(doc):
    """A short note from the job's QoS block, when CDS sends one: the
    per-user running/queued request counts (lists or ints) and any named
    limit. Empty when the block is empty — it is, on a quiet day."""
    try:
        qos = ((doc.get("metadata") or {}).get("qos") or {}).get("status") or {}
    except AttributeError:
        return ""
    if not isinstance(qos, dict) or not qos:
        return ""
    parts = []
    for key in ("running", "queued"):
        v = qos.get(key)
        if isinstance(v, list):
            parts.append(f"{key} {len(v)}")
        elif isinstance(v, (int, float)):
            parts.append(f"{key} {int(v)}")
    for key in ("limits", "limit"):
        v = qos.get(key)
        if isinstance(v, (str, int)):
            parts.append(f"limit {v}")
        elif isinstance(v, list) and v:
            parts.append(f"{len(v)} limit(s)")
    return f" · {', '.join(parts)}" if parts else ""


def cds_retrieve(dataset, inputs, target, token,
                 max_wait_s=int(os.environ.get("CDS_QUEUE_MAX_S", "14400"))):
    """Submit one CDS request, ride the queue, download the result GRIB.

    The queue is real (minutes to hours at busy times) — poll with backoff
    and keep the user informed; a silent multi-hour hang reads as a crash.
    """
    sub = _cds_json(f"{CDS_API_URL}/retrieve/v1/processes/{dataset}/execution",
                    token, {"inputs": inputs})
    job_url = _cds_link(sub, "monitor",
                        f"{CDS_API_URL}/retrieve/v1/jobs/{sub.get('jobID', '')}")
    t0, delay, status = time.monotonic(), 2.0, sub.get("status", "accepted")
    doc = sub
    last_said = t0
    while status in ("accepted", "running"):
        if time.monotonic() - t0 > max_wait_s:
            raise RuntimeError(
                f"CDS queue exceeded {max_wait_s}s for {dataset} — the queue is long "
                "right now; retry later or raise CDS_QUEUE_MAX_S")
        time.sleep(delay)
        delay = min(60.0, delay * 1.5)
        doc = _cds_json(job_url, token)
        new = doc.get("status", status)
        # Narrate the wait: on every status change and at least every 30 s
        # while nothing changes, so a hosted run's heartbeat can show the
        # Copernicus queue live.
        if new != status or time.monotonic() - last_said >= CDS_SAY_EVERY_S:
            log(f"  CDS {dataset}: {cds_status_word(new)} "
                f"({time.monotonic() - t0:.0f}s){cds_qos_note(doc)}")
            last_said = time.monotonic()
            status = new
    if status != "successful":
        raise RuntimeError(f"CDS job for {dataset} ended '{status}': "
                           f"{json.dumps(doc.get('message') or doc)[:400]}")
    res = _cds_json(_cds_link(doc, "results", f"{job_url}/results"), token)
    href = ((res.get("asset") or {}).get("value") or {}).get("href")
    if not href:
        raise RuntimeError(f"CDS results for {dataset} carry no asset href: "
                           f"{json.dumps(res)[:400]}")
    req = urllib.request.Request(href, headers={"PRIVATE-TOKEN": token})
    with urllib.request.urlopen(req, timeout=1800) as resp:
        data = resp.read()
    if len(data) < 10_000:
        raise IOError(f"suspiciously small CDS download ({len(data)} b)")
    _atomic_write(target, data)


def download_era5(cfg, cycle_dt, hours, grib_dir):
    """ERA5 boundary/initial conditions: one pressure-level + one surface
    GRIB per calendar day (day-batched requests are far kinder to the CDS
    queue than per-hour ones), server-side cropped to the region box.
    Both file families ungrib together under the one Vtable."""
    grib_dir.mkdir(parents=True, exist_ok=True)
    token = cds_token()
    g = cfg["grib"]
    area = [g["top_lat"], g["left_lon"], g["bottom_lat"], g["right_lon"]]  # N W S E

    # the hosted runner's cycle_dt is tz-aware UTC, local_run's is naive;
    # everything below (the embargo compare, request stamps, cache keys)
    # works in naive UTC.
    if cycle_dt.tzinfo is not None:
        cycle_dt = cycle_dt.astimezone(timezone.utc).replace(tzinfo=None)
    times = [cycle_dt + timedelta(hours=h) for h in hours]
    horizon = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=ERA5T_LAG_DAYS)
    late = [t for t in times if t > horizon]
    if late:
        raise RuntimeError(
            f"ERA5 is a reanalysis ~{ERA5T_LAG_DAYS} days behind real time: "
            f"{late[0]:%Y-%m-%d %H}Z isn't published yet (newest ≈ {horizon:%Y-%m-%d}). "
            "Pick an earlier day — or use GFS/HRRR for recent dates and forecasts.")

    by_day = {}
    for t in times:
        by_day.setdefault(t.date(), []).append(f"{t:%H}:00")
    for day, hh in sorted(by_day.items()):
        base = {
            "product_type": ["reanalysis"],
            "year": [f"{day:%Y}"], "month": [f"{day:%m}"], "day": [f"{day:%d}"],
            "time": sorted(hh),
            "area": area,
            "data_format": "grib",
            "download_format": "unarchived",
        }
        for dataset, extra, tag in (
            (ERA5_PL_DATASET,
             {"variable": ERA5_PL_VARIABLES, "pressure_level": ERA5_PL_LEVELS}, "pl"),
            (ERA5_SFC_DATASET, {"variable": ERA5_SFC_VARIABLES}, "sfc"),
        ):
            target = grib_dir / f"era5_{tag}_{day:%Y%m%d}.grib"
            if target.exists() and target.stat().st_size > 1_000_000:
                continue
            # Shared cache keyed by the DAY (not the init hour): the day's
            # file serves every window that touches it. The licence
            # (CC-BY-4.0) allows the share; the file is ~2 MB per hour.
            day_dt = datetime(day.year, day.month, day.day)
            if forcing_r2_get(cfg, "era5", day_dt, target.name, target):
                continue
            log(f"download era5 {tag} {day} ({len(hh)} hour(s); CDS queue may take a while)")
            cds_retrieve(dataset, dict(base, **extra), target, token)
            forcing_r2_put(cfg, "era5", day_dt, target.name, target)


def stage_workdir(region, workdir, config_dir=None, boundary_source="gfs"):
    """Link executables, tables and region files into the run directory.
    Service-provided templates (config_dir, staged by the caller)
    take precedence over the copies baked into the image; region=None means
    there are no baked copies (forge image) and config_dir is the only
    source of namelists."""
    workdir.mkdir(parents=True, exist_ok=True)
    rdir = BASEDIR / "regions" / region if region else None
    for f in ("namelist.wps", "namelist.input", "GEOGRID.TBL", "wind_io.txt"):
        for src in [Path(config_dir) / f if config_dir else None, rdir / f if rdir else None]:
            if src and src.exists():
                shutil.copy(src, workdir / f)
                break
    shutil.copy(BASEDIR / "tables" / "METGRID.TBL", workdir / "METGRID.TBL")
    shutil.copy(BASEDIR / "tables" / VTABLES[boundary_source], workdir / "Vtable")
    # WRF runtime tables/data files (LANDUSE.TBL, RRTMG_*, ozone, etc.)
    for src in (BASEDIR / "wrf_run").iterdir():
        dst = workdir / src.name
        if src.name in ("namelist.input",):
            continue
        if dst.is_symlink() or dst.exists():
            continue
        dst.symlink_to(src)


def edit_namelists(workdir, start, end, ndom=2, interval_h=None):
    """Stamp run dates into both namelists with one column per domain —
    a missing column reads as month zero downstream ('Screwy NDATE').
    interval_h additionally stamps the boundary interval into both files
    (hourly for HRRR forcing, 3-hourly for GFS)."""
    wps = workdir / "namelist.wps"
    t = wps.read_text()
    s = start.strftime("%Y-%m-%d_%H:%M:%S")
    e = end.strftime("%Y-%m-%d_%H:%M:%S")
    dates_s = ", ".join([f"'{s}'"] * ndom)
    dates_e = ", ".join([f"'{e}'"] * ndom)
    t = re.sub(r"start_date\s*=.*", f"start_date           = {dates_s},", t)
    t = re.sub(r"end_date\s*=.*", f"end_date             = {dates_e},", t)
    if interval_h:
        t = re.sub(r"^(\s*interval_seconds\s*=).*", rf"\g<1> {interval_h * 3600},", t, flags=re.M)
    wps.write_text(t)

    ni = workdir / "namelist.input"
    t = ni.read_text()

    def cols(fmt, dt):
        return ",".join([format(dt, fmt)] * ndom)

    for key, val in [
        ("run_days", "0"), ("run_hours", "0"), ("run_minutes", "0"), ("run_seconds", "0"),
        ("start_year", cols("%Y", start)), ("start_month", cols("%m", start)),
        ("start_day", cols("%d", start)), ("start_hour", cols("%H", start)),
        ("end_year", cols("%Y", end)), ("end_month", cols("%m", end)),
        ("end_day", cols("%d", end)), ("end_hour", cols("%H", end)),
    ]:
        t = re.sub(rf"^(\s*{key}\s*=).*", rf"\g<1> {val},", t, flags=re.M)
    if interval_h:
        t = re.sub(r"^(\s*interval_seconds\s*=).*", rf"\g<1> {interval_h * 3600}", t, flags=re.M)
    ni.write_text(t)


def parse_nest_windows(cfg, overrides, ndom, start, end):
    """Job-level nest time windows: {"nest_windows": {"d05": [s, e] | null}}.

    Enforcement of region.json's "nests" scheduling intent: the hosting
    service resolves intent into concrete UTC
    windows at enqueue; this validates them against the run. null = nest not
    integrated at all (max_dom shrink — trailing domains only, since max_dom
    is a count). Window starts must sit on the boundary-interval grid:
    real.exe initializes a late nest from the met_em file at the nest's
    start time, and met_em only exists on that grid.

    Returns ({domain_index: (start_utc, end_utc) or None}, effective max_dom).
    """
    raw = (overrides or {}).get("nest_windows") or {}
    windows = {}
    interval = int(cfg["boundary_interval_hours"])
    for name, win in raw.items():
        m = re.fullmatch(r"d(\d{2})", str(name))
        idx = int(m.group(1)) if m else 0
        if idx < 2 or idx > ndom:
            raise RuntimeError(
                f"nest_windows: '{name}' is not a nest of this {ndom}-domain run")
        if win is None:
            windows[idx] = None
            continue
        try:
            s, e = (datetime.strptime(str(v).replace("T", "_"), "%Y-%m-%d_%H:%M:%S")
                    .replace(tzinfo=timezone.utc) for v in win)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"nest_windows: bad window for {name}: {win!r}") from exc
        s, e = max(s, start), min(e, end)
        if s >= e:
            raise RuntimeError(
                f"nest_windows: {name} window {win!r} lies outside the run "
                f"{start:%Y-%m-%d_%H:%M}..{end:%Y-%m-%d_%H:%M}")
        if (s - start).total_seconds() % (interval * 3600):
            raise RuntimeError(
                f"nest_windows: {name} start {s:%Y-%m-%d_%H:%M}Z is off the "
                f"{interval}h boundary grid — met_em only exists at those times")
        windows[idx] = (s, e)
    # trailing off-nests shrink max_dom (it is a count); an off nest with an
    # ACTIVE sibling after it (e.g. lake-north calm while lake-south fires)
    # cannot be dropped that way, so it is pinned to an empty window
    # (start == end) — the domain exists but never activates
    eff = ndom
    while eff >= 2 and windows.get(eff, "on") is None:
        eff -= 1
    for idx, win in list(windows.items()):
        if win is None and idx <= eff:
            windows[idx] = (start, start)
    return windows, eff


def apply_nest_windows(workdir, windows, eff_ndom, ndom, start, end):
    """Stamp per-domain start/end columns for windowed nests; shrink max_dom
    when trailing nests are off. Runs after edit_namelists (which stamps
    identical columns everywhere) and rewrites only date/max_dom lines.
    namelist.wps gets the same columns so metgrid produces the late nest's
    met_em at its start time."""
    starts = {i: start for i in range(1, ndom + 1)}
    ends = {i: end for i in range(1, ndom + 1)}
    for idx, win in windows.items():
        if win:
            starts[idx], ends[idx] = win

    wps = workdir / "namelist.wps"
    t = wps.read_text()
    # a child window outside its parent's active window aborts WRF at nest
    # start; validate against the staged namelist's domain tree
    parents = [int(v) for v in
               re.search(r"parent_id\s*=([^\n]*)", t).group(1).replace(",", " ").split()][:ndom]
    for idx in range(2, eff_ndom + 1):
        p = parents[idx - 1]
        if p >= 1 and (starts[idx] < starts[p] or ends[idx] > ends[p]):
            raise RuntimeError(
                f"nest_windows: d{idx:02d} window exceeds its parent d{p:02d}'s window")
    fmt = "%Y-%m-%d_%H:%M:%S"
    dates_s = ", ".join(f"'{starts[i].strftime(fmt)}'" for i in range(1, ndom + 1))
    dates_e = ", ".join(f"'{ends[i].strftime(fmt)}'" for i in range(1, ndom + 1))
    t = re.sub(r"start_date\s*=.*", f"start_date           = {dates_s},", t)
    t = re.sub(r"end_date\s*=.*", f"end_date             = {dates_e},", t)
    if eff_ndom != ndom:
        t = re.sub(r"^(\s*max_dom\s*=).*", rf"\g<1> {eff_ndom},", t, flags=re.M)
    wps.write_text(t)

    ni = workdir / "namelist.input"
    t = ni.read_text()
    for key, f2, src in [
        ("start_year", "%Y", starts), ("start_month", "%m", starts),
        ("start_day", "%d", starts), ("start_hour", "%H", starts),
        ("start_minute", "%M", starts), ("start_second", "%S", starts),
        ("end_year", "%Y", ends), ("end_month", "%m", ends),
        ("end_day", "%d", ends), ("end_hour", "%H", ends),
        ("end_minute", "%M", ends), ("end_second", "%S", ends),
    ]:
        val = ",".join(format(src[i], f2) for i in range(1, ndom + 1))
        t = re.sub(rf"^(\s*{key}\s*=).*", rf"\g<1> {val},", t, flags=re.M)
    if eff_ndom != ndom:
        t = re.sub(r"^(\s*max_dom\s*=).*", rf"\g<1> {eff_ndom},", t, flags=re.M)
    ni.write_text(t)
    for idx, win in sorted(windows.items()):
        log(f"nest window d{idx:02d}: " + (
            "off (max_dom shrink)" if win is None else
            "off (pinned empty window)" if win[0] == win[1] else
            f"{win[0]:%Y-%m-%d %H:%M}Z..{win[1]:%Y-%m-%d %H:%M}Z"))


def _replace_namelist_entry(text, key, val):
    """Replace a namelist entry including any continuation lines (values
    like eta_levels span multiple lines). Returns (new_text, matched)."""
    if isinstance(val, (list, tuple)):
        val = ", ".join(str(v) for v in val)
    pattern = (rf"^([ \t]*{re.escape(key)}[ \t]*=)[^\n]*"
               r"(?:\n(?![ \t]*[A-Za-z_][A-Za-z0-9_]*[ \t]*=|[ \t]*[&/])[^\n]*)*")
    new, n = re.subn(pattern, lambda m: f"{m.group(1)} {val}", text, count=1, flags=re.M)
    return new, n > 0


def apply_config_overrides(workdir, cfg_overrides):
    """Stamp job-level config overrides into the staged run files.

    cfg_overrides = {"namelist": {key: value}, "wps": {key: value},
                     "wind_io": "..."} — values are written verbatim (supply
    one column per domain, exactly as in the template). Overrides may only
    change entries that already exist in the template, and never geometry
    (GEOMETRY_KEYS): both misuses fail the run loudly rather than let a
    typo'd knob or a grid/static mismatch produce a silently wrong forecast.
    """
    for section, fname in (("namelist", "namelist.input"), ("wps", "namelist.wps")):
        entries = (cfg_overrides or {}).get(section) or {}
        if not entries:
            continue
        path = workdir / fname
        text = path.read_text()
        for key, val in entries.items():
            if key.lower() in GEOMETRY_KEYS:
                raise RuntimeError(
                    f"override '{key}' changes domain geometry; ship a static-set "
                    "variant (new geo_em + templates) instead of a job override")
            text, matched = _replace_namelist_entry(text, key, val)
            if not matched:
                raise RuntimeError(f"override '{key}' not present in {fname} — "
                                   "overrides can only change existing entries")
            log(f"override {fname}: {key} = {val}")
        path.write_text(text)
    if (cfg_overrides or {}).get("wind_io"):
        (workdir / "wind_io.txt").write_text(cfg_overrides["wind_io"])
        log("override wind_io.txt")


def _active_at(windows, idx, t):
    """Is domain idx integrating at time t? (windows: parse_nest_windows output)"""
    win = (windows or {}).get(idx, "always")
    if win is None:
        return False
    return win == "always" or (win[0] <= t < win[1])


def detect_checkpoint(workdir, start, end, ndomains=2, windows=None,
                      allow_at_start=False):
    """Latest complete wrfrst set strictly inside the window. A time-windowed
    nest only writes restarts while active, so it is required only at stamps
    its window covers; an off nest never is.

    `allow_at_start` admits a stamp AT the window start — the warm-branch
    case, where the caller staged a previous run's wrfrst stamped
    exactly at this run's init (config.warm_start). Same-job resume keeps
    the strict boundary: a stamp at start would mean zero progress, and
    re-running real.exe from scratch is the correct move there."""
    stamps = {}
    for f in workdir.glob("wrfrst_d0*_*"):
        parts = f.name.split("_", 2)
        if len(parts) != 3:
            continue
        stamps.setdefault(parts[2], set()).add(parts[1])
    best = None
    for ts, doms in stamps.items():
        try:
            t = datetime.strptime(ts, "%Y-%m-%d_%H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        needed = {f"d{i:02d}" for i in range(1, ndomains + 1) if _active_at(windows, i, t)}
        if needed - doms:
            continue
        if (start < t or (allow_at_start and start == t)) and t < end \
                and (best is None or t > best[0]):
            best = (t, ts)
    return best


def enable_restart(workdir, t, ndom=2, windows=None):
    """Continue the integration from a restart file, bit-continuous:
    same simulation clock (reset_simulation_start off), same everything else.
    Windowed nests keep per-domain starts: active-at-t nests resume from
    wrfrst, not-yet-started ones keep their future start (WRF opens them on
    schedule from parent interpolation), finished ones are pinned start=end
    so they never reopen.
    """
    starts = {}
    for i in range(1, ndom + 1):
        win = (windows or {}).get(i, "always")
        if win in (None, "always"):
            starts[i] = t
        elif t < win[0]:
            starts[i] = win[0]
        elif t >= win[1]:
            starts[i] = win[1]
        else:
            starts[i] = t
    ni = workdir / "namelist.input"
    txt = ni.read_text()
    txt = re.sub(r"^(\s*restart\s*=).*", r"\g<1> .true.,", txt, flags=re.M)
    txt = re.sub(r"^(\s*reset_simulation_start\s*=).*", r"\g<1> .false.", txt, flags=re.M)
    for key, fmt in [
        ("start_year", "%Y"), ("start_month", "%m"), ("start_day", "%d"),
        ("start_hour", "%H"), ("start_minute", "%M"), ("start_second", "%S"),
    ]:
        val = ",".join(format(starts[i], fmt) for i in range(1, ndom + 1))
        txt = re.sub(rf"^(\s*{key}\s*=).*", rf"\g<1> {val},", txt, flags=re.M)
    ni.write_text(txt)


# What metgrid names the soil dimension depends on how the boundary source
# reports soil. GFS sends layers bounded by two depths (ST000010 ->
# num_st_layers, 4 of them); HRRR/RAP send RUC levels at fixed depths
# (SOILT000 -> num_soilt_levels, 9 of them). real.exe interpolates either
# onto the LSM's own num_soil_layers, but num_metgrid_soil_levels has to
# name however many metgrid actually wrote. Ordered RUC-first: if a met_em
# ever carries BOTH dims (a mixed-soil config that slipped past
# Vtable.GFS_NOSOIL), the 9-level analysis is the one we meant to use, and
# set_metgrid_levels logs which dim won so the ambiguity is visible.
SOIL_DIMS = ("num_soilt_levels", "num_st_layers")


def force_sfcp_to_sfcp(workdir):
    """Pressure-level init from a terrain-honest 3 km source: have real.exe
    adjust the source's surface pressure to model terrain directly instead
    of reconstructing it from sea-level pressure — the standard setting for
    RAP-family pressure data, and doubly right here because RRFS's MSLET is
    an Eta reduction that goes soft over exactly our kind of terrain.
    Without it, and with no SLP mapping, real.exe stops with "not enough
    info for a p sfc computation"; with both in place this is belt and braces."""
    ni = workdir / "namelist.input"
    t = ni.read_text()
    if re.search(r"^\s*sfcp_to_sfcp", t, re.M):
        t = re.sub(r"^(\s*sfcp_to_sfcp\s*=).*", r"\g<1> .true.,", t, flags=re.M)
    else:
        t = re.sub(r"^(\s*&domains\s*)$", "\\1\n sfcp_to_sfcp                        = .true.,",
                   t, count=1, flags=re.M)
    ni.write_text(t)
    log("rrfs init: sfcp_to_sfcp = .true.")


def set_metgrid_levels(workdir):
    """num_metgrid_levels must match what metgrid produced."""
    from netCDF4 import Dataset

    met = sorted(workdir.glob("met_em.d01.*.nc"))[0]
    with Dataset(met) as nc:
        levels = nc.dimensions["num_metgrid_levels"].size
        present = [d for d in SOIL_DIMS if d in nc.dimensions]
        if not present:
            raise RuntimeError(
                f"{met.name} has none of {SOIL_DIMS} — soil dims present: "
                f"{[d for d in nc.dimensions if 'soil' in d or 'layer' in d]}")
        named = present[0]
        if len(present) > 1:
            log(f"met_em carries {present}; using {named} for num_metgrid_soil_levels")
        soil = nc.dimensions[named].size
    ni = workdir / "namelist.input"
    t = ni.read_text()
    t = re.sub(r"^(\s*num_metgrid_levels\s*=).*", rf"\g<1> {levels},", t, flags=re.M)
    t = re.sub(r"^(\s*num_metgrid_soil_levels\s*=).*", rf"\g<1> {soil},", t, flags=re.M)
    ni.write_text(t)
    log(f"num_metgrid_levels={levels}, soil={soil} ({named})")


def link_gribs(grib_dir, workdir):
    suffixes = []
    a, b = ord("A"), ord("A")
    # Skip dotfiles: a concurrent worker's in-flight ".<name>.<pid>.part"
    # download would otherwise be handed to ungrib as a GRIB file.
    for f in sorted(p for p in grib_dir.iterdir() if not p.name.startswith(".")):
        name = f"GRIBFILE.A{chr(a)}{chr(b)}"
        link = workdir / name
        link.unlink(missing_ok=True)
        link.symlink_to(f)
        suffixes.append(name)
        b += 1
        if b > ord("Z"):
            b = ord("A")
            a += 1
    log(f"linked {len(suffixes)} GRIB files")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default=os.environ.get("REGION", "PNW"))
    ap.add_argument("--start-day", type=int, default=int(os.environ.get("START_DAY", "0")))
    ap.add_argument("--date", default=os.environ.get("RUN_DATE"),
                    help="YYYY-MM-DD local run date (hindcast); overrides --start-day")
    ap.add_argument("--cycle", type=int, default=int(os.environ.get("GFS_CYCLE", "0")))
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--config-dir", default=None,
                    help="service-provided templates; take precedence over the image's")
    ap.add_argument("--overrides", default=None,
                    help="JSON file of job config overrides (see apply_config_overrides)")
    args = ap.parse_args()

    overrides = json.loads(Path(args.overrides).read_text()) if args.overrides else {}
    cfg = region_config(args.region, args.config_dir, overrides)
    workdir = Path(args.workdir or (BASEDIR / "runs" / args.region))
    logdir = workdir / "LOG"
    logdir.mkdir(parents=True, exist_ok=True)

    source = cfg.get("boundary_source", "gfs")
    if source not in VTABLES:
        raise RuntimeError(
            f"unknown boundary_source '{source}' ({' | '.join(sorted(VTABLES))})")
    cycle_dt, hours, start, end = forecast_window(cfg, args.start_day, args.cycle, args.date)
    if source in ("hrrr", "rrfs"):
        # Clamp the run to the available boundary data so WRF never outruns
        # its BCs: HRRR reaches f48 on synoptic cycles (f18 otherwise), RRFS
        # f84 on synoptic cycles (f18 otherwise).
        deep = 84 if source == "rrfs" else 48
        limit = deep if cycle_dt.hour in (0, 6, 12, 18) else 18
        if hours[0] >= limit:
            raise RuntimeError(
                f"{source.upper()} t{cycle_dt:%H}z reaches f{limit} but the window starts at "
                f"f{hours[0]} — forecast day too far from the cycle")
        if hours[-1] > limit:
            hours = [h for h in hours if h <= limit]
            end = cycle_dt + timedelta(hours=limit)
            log(f"{source.upper()} window clamped to f{limit} (end {end:%Y-%m-%d_%H}Z)")
    log(f"window {start:%Y-%m-%d_%H}Z..{end:%Y-%m-%d_%H}Z from {source.upper()} "
        f"{cycle_dt:%Y%m%d %H}Z f{hours[0]:03d}-f{hours[-1]:03d}")

    stage_workdir(args.region, workdir, args.config_dir, source)
    # Sidecar recording what forced this run, so an archive consumer can
    # re-fetch the matching forcing from the public GFS/HRRR archives —
    # forcing is re-fetchable forever, so it is never archived.
    (workdir / "run_meta.json").write_text(json.dumps({
        "region": args.region,
        "boundary_source": source,
        "cycle_utc": f"{cycle_dt:%Y-%m-%d_%H}",
        "window_start_utc": f"{start:%Y-%m-%d_%H:%M}",
        "window_end_utc": f"{end:%Y-%m-%d_%H:%M}",
        "forecast_hours": [hours[0], hours[-1]],
        "boundary_interval_hours": cfg["boundary_interval_hours"],
        "domains": int(cfg.get("domains", 2)),
        "nest_windows": (overrides or {}).get("nest_windows") or None,
        **({"attribution": ERA5_ATTRIBUTION.format(year=datetime.now(timezone.utc).year)}
           if source == "era5" else {}),
    }, indent=1))

    ndom = int(cfg.get("domains", 2))
    windows, eff_ndom = parse_nest_windows(cfg, overrides, ndom, start, end)
    for i in range(1, eff_ndom + 1):
        if not (workdir / f"geo_em.d{i:02d}.nc").exists():
            raise RuntimeError(f"geo_em.d{i:02d}.nc missing from {workdir} — run geogrid or stage them from the hosting service")

    edit_namelists(workdir, start, end, ndom, cfg["boundary_interval_hours"])
    if source == "rrfs":
        force_sfcp_to_sfcp(workdir)
    if windows:
        apply_nest_windows(workdir, windows, eff_ndom, ndom, start, end)
    apply_config_overrides(workdir, overrides)
    # Forcing lands in the shared cache when one is configured, so a rerun of
    # this cycle (variant A/B, a retry, a second worker on the host) reuses it.
    grib_dir = grib_cache_dir(cfg, cycle_dt, source) or (workdir / "GRIB")
    if source == "hrrr":
        download_hrrr(cfg, cycle_dt, hours, grib_dir, workdir)
    elif source == "rrfs":
        download_rrfs(cfg, cycle_dt, hours, grib_dir, workdir)
    elif source == "era5":
        download_era5(cfg, cycle_dt, hours, grib_dir)
    else:
        download_gfs(cfg, cycle_dt, hours, grib_dir)
    link_gribs(grib_dir, workdir)

    # Hi-res SST init: GeoPolar Blended 5 km as a metgrid
    # constant, replacing the boundary source's coarse-landmask SST over
    # water. Fail-open: any fetch/format problem logs and the run proceeds
    # on GFS/HRRR skin temp exactly as before. A config override
    # {"geopolar_sst": false} runs the old path — the control arm for
    # hindcast A/Bs (and the escape hatch if the feed misbehaves).
    # ERA5 defaults OFF: its own SST/skin analysis is already good, and its
    # hindcasts predate the GeoPolar archive's practical reach; an explicit
    # {"geopolar_sst": true} override still turns it on for A/Bs.
    if (overrides or {}).get("geopolar_sst", source != "era5"):
        try:
            import geopolar_sst
        except ImportError as e:
            # enrichment must never kill a run — but a missing module means the
            # image was assembled without it, and a silent miss would poison SST
            # A/B attribution. Shout.
            log(f"GEOSST module not present in this image ({e}); "
                "running with boundary-source SST")
        else:
            geopolar_sst.stage(workdir, cycle_dt, log)
    else:
        log("GEOSST disabled by config override; SST from boundary source")

    # HRRR-anchored soil init: RUC 9-level cycled land DA as the init soil,
    # GFS keeping the atmosphere ("borrowed DA" — see image/hrrr_soil.py).
    # GFS-forced runs only: HRRR/RRFS-forced ones already carry RUC soil.
    # Same fail-open posture as GEOSST; enabled per-variant, not by default,
    # so an evaluation arm owns the A/B before any production exposure.
    if (overrides or {}).get("soil_source") == "hrrr":
        if source != "gfs":
            log(f"soil_source=hrrr ignored: boundary source '{source}' already carries RUC soil")
        else:
            try:
                import hrrr_soil
            except ImportError as e:
                log(f"HRRRSOIL module not present in this image ({e}); "
                    "running with GFS soil")
            else:
                hrrr_soil.stage(workdir, cycle_dt, log)

    ranks, threads, cpus = parallel_layout(cfg)
    ranks = check_decomposition(workdir, ranks, eff_ndom)
    threads = cap_threads_for_d01(workdir, threads)
    mpi, bound = mpi_prefix(ranks, threads, cfg)
    env = wrf_environment(cfg, threads, bound)
    log(f"parallel layout [{WRF_PARALLEL}]: {ranks} MPI rank(s) x {threads} "
        f"OpenMP thread(s) across {cpus} usable CPU(s)")

    run(["ungrib.exe"], workdir, logdir / "ungrib.out")
    expect("Successful completion", logdir / "ungrib.out")
    run(["avg_tsfc.exe"], workdir, logdir / "avg_tsfc.out")
    run(["metgrid.exe"], workdir, logdir / "metgrid.out")
    expect("Successful completion", logdir / "metgrid.out")
    set_metgrid_levels(workdir)
    run(mpi + ["real.exe"], workdir, logdir / "real.out", env=env)
    expect("SUCCESS COMPLETE REAL_EM INIT", workdir / "rsl.out.0000"
           if (workdir / "rsl.out.0000").exists() else logdir / "real.out")
    # Resume from a checkpoint when one is staged (same job identity =
    # identical GFS forcing; wrfbdy was just regenerated deterministically).
    warm = bool((overrides or {}).get("warm_start"))
    ckpt = detect_checkpoint(workdir, start, end, eff_ndom, windows,
                             allow_at_start=warm)
    if ckpt:
        enable_restart(workdir, ckpt[0], ndom, windows)
        if warm and ckpt[0] == start:
            log(f"warm branch: restarting from streak state at {ckpt[1]} "
                "with fresh boundary forcing")
        else:
            log(f"resuming from checkpoint {ckpt[1]} — skipping {int((ckpt[0]-start).total_seconds()//3600)}h of recompute")
    elif warm:
        log("warm start requested but no usable wrfrst at the window start — cold start")
    # Provenance for evaluation: a warm run that silently ran cold must
    # be classifiable per job (train/meta.json inherits these keys).
    meta_path = workdir / "run_meta.json"
    meta = json.loads(meta_path.read_text())
    meta["warm_start_requested"] = warm
    meta["warm_started"] = bool(warm and ckpt and ckpt[0] == start)
    meta["restart_from"] = ckpt[1] if ckpt else None
    meta_path.write_text(json.dumps(meta, indent=1))
    run(mpi + ["wrf.exe"], workdir, logdir / "wrf.out", env=env)
    expect("SUCCESS COMPLETE WRF", workdir / "rsl.out.0000"
           if (workdir / "rsl.out.0000").exists() else logdir / "wrf.out")
    summarize_timing(workdir, logdir)

    produced = sorted(workdir.glob("wrfwind_d02*")) or sorted(workdir.glob("wrfout_d02*"))
    log(f"run complete: {len(produced)} d02 output files in {workdir}")


if __name__ == "__main__":
    main()
