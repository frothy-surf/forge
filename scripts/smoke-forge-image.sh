#!/bin/bash
# Run one real forecast through a forge image, end to end, the way a user
# would: a bundle, a WPS_GEOG mount, an out dir — geogrid, a live GFS fetch,
# ungrib, metgrid, real, wrf, and the wind products. CI runs it per
# architecture before anything is published; it works the same by hand.
#
#   scripts/smoke-forge-image.sh <image-ref> <input-dir> [stations.json]
#
# The input is a forge bundle sized to integrate in seconds (see
# examples/smoke/). Static data is UCAR's low-resolution mandatory set
# (~150 MB), fetched once into $SMOKE_HOME and run with --geog-res lowres:
# the test validates the image's machinery, not the terrain.
#
# A third argument is a stations list in the shape extract_wind.py's
# --stations takes ({id, lat, lon} points, {..., polygon} areas). The
# extractor then runs a second time over the forecast's output with it, and
# the pass fails unless every point got a timeseries and every polygon was
# read as a mean over native water cells. A plain forecast never calls that
# sampling path; this is how a change to it is proven before an image ships.
#
# The image's own entrypoint is bypassed (python3 + the run script are at
# the same paths in every image built from image/Dockerfile), so a hosted
# image that starts its job loop by default is smoked the same way.
# SMOKE_USER sets the uid:gid the containers run as; the default is yours,
# so the output is owned by you. An image with no unprivileged user runs as
# `root`.
#
# Everything lives under $SMOKE_HOME (default ~/.cache/forge-smoke) rather
# than a system temp dir: Docker on macOS and Windows shares the home
# directory with its VM, not /tmp.
set -euo pipefail
image="${1:?usage: smoke-forge-image.sh <image-ref> <input-dir> [stations.json]}"
input="$(cd "${2:?usage: smoke-forge-image.sh <image-ref> <input-dir> [stations.json]}" && pwd)"
stations=""
if [ -n "${3:-}" ]; then
  [ -f "$3" ] || { echo "stations file not found: $3"; exit 2; }
  stations="$(cd "$(dirname "$3")" && pwd)/$(basename "$3")"
fi
user="${SMOKE_USER:-$(id -u):$(id -g)}"
home="${SMOKE_HOME:-$HOME/.cache/forge-smoke}"
geog="$home/WPS_GEOG_LOW_RES"
out="$home/out/$(basename "$input")"
GEOG_URL="${SMOKE_GEOG_URL:-https://www2.mmm.ucar.edu/wrf/src/wps_files/geog_low_res_mandatory.tar.gz}"

mkdir -p "$home"
if [ ! -d "$geog/topo_gmted2010_5m" ]; then
  echo "fetching low-resolution WPS_GEOG from UCAR"
  curl -fsSL --retry 3 -o "$home/geog.tgz" "$GEOG_URL"
  tar -xzf "$home/geog.tgz" -C "$home"
  rm -f "$home/geog.tgz"
fi
rm -rf "$out"
mkdir -p "$out"

hours="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["forecast_hours"])' "$input/region.json")"
t0=$(date +%s)
docker run --rm --user "$user" --entrypoint python3 \
  -v "$input:/bundle:ro" -v "$geog:/geog:ro" -v "$out:/out" \
  "$image" /opt/blip/bin/local_run.py --hours "$hours" --geog-res lowres --no-serve --no-telemetry 2>&1 | tee "$out/run.log" \
  || { echo "smoke run failed"; exit 1; }
echo "run took $(( $(date +%s) - t0 ))s"

python3 - "$out" "$input" "$hours" <<'PY'
import glob, json, math, os, sys
out, inp, hours = sys.argv[1], sys.argv[2], int(sys.argv[3])
fail = lambda m: sys.exit(f"smoke FAILED: {m}")

log = open(os.path.join(out, "run.log")).read()
for stage in ("geogrid.exe", "ungrib.exe", "metgrid.exe", "real.exe"):
    if not any("exec:" in line and stage in line for line in log.splitlines()):
        fail(f"{stage} never ran")
if "timing total:" not in log:  # the runner's summary of wrf.exe's integration
    fail("wrf.exe did not integrate")
if "run complete" not in log:
    fail("runner did not report completion")

manifest = json.load(open(os.path.join(out, "wind", "manifest.json")))
if manifest.get("schema") != 1:
    fail(f"manifest schema is {manifest.get('schema')!r}, expected 1")
rects = [r["id"] for r in json.load(open(os.path.join(inp, "product.json")))["rects"]]
for rect in rects:
    frames = sorted(glob.glob(os.path.join(out, "wind", rect, "wind_*.json")))
    if len(frames) < hours:  # hourly history: init + each hour, spin-up frame may be skipped
        fail(f"rect {rect}: {len(frames)} frame(s) for a {hours} h run")
    listed = [f for f in manifest["frames"] if f["rect_id"] == rect]
    if len(listed) != len(frames):
        fail(f"rect {rect}: manifest lists {len(listed)} frames, disk has {len(frames)}")
    for path in frames:
        speeds = [v for row in json.load(open(path))["speed_kts"] for v in row if v is not None]
        if not speeds:
            fail(f"{os.path.basename(path)} has no wind cells")
        if not all(math.isfinite(v) and 0 <= v < 150 for v in speeds):
            fail(f"{os.path.basename(path)} has non-physical wind speeds")
    print(f"rect {rect}: {len(frames)} frames, last max {max(speeds):.1f} kt over {len(speeds)} cells")

# Thompson microphysics (the CONUS suite's scheme) reads its lookup tables
# from the run directory and quietly computes them — minutes of startup —
# when they are missing or unreadable. The image ships them; a run that
# computed one means they were not built, not staged, or not readable.
for path in glob.glob(os.path.join(out, "work", "rsl.*.0000")) + glob.glob(os.path.join(out, "work", "LOG", "*")):
    if "ThompMP: computing" in open(path, errors="replace").read():
        fail(f"wrf.exe computed Thompson lookup tables ({os.path.basename(path)}): the image's copies were not used")

if not glob.glob(os.path.join(out, "work", "wrfout_d01_*")):
    fail("no wrfout files in the work dir")
print("smoke OK")
PY

[ -n "$stations" ] || exit 0

# ---- stations pass: the extractor over the run's own output, sampling the
# listed points and areas off the native grid (the stations.json the hosted
# service publishes beside the frames).
echo "stations pass with $(basename "$stations")"
docker run --rm --user "$user" --entrypoint sh \
  -v "$input:/bundle:ro" -v "$(dirname "$stations"):/stations:ro" -v "$out:/out" \
  "$image" -c "exec python3 /opt/blip/worker/extract_wind.py --config /bundle/product.json \
    --static /out/work/geo_em.d01.nc --out /out/wind-stations \
    --stations /stations/$(basename "$stations") /out/work/wrfout_d01_*" > "$out/stations.log" 2>&1 \
  || { cat "$out/stations.log"; echo "stations pass failed"; exit 1; }
cat "$out/stations.log"

python3 - "$out" "$stations" "$hours" <<'PY'
import json, math, os, sys
out, spath, hours = sys.argv[1], sys.argv[2], int(sys.argv[3])
fail = lambda m: sys.exit(f"stations FAILED: {m}")

wanted = json.load(open(spath))
series = json.load(open(os.path.join(out, "wind-stations", "stations.json")))["stations"]
log = open(os.path.join(out, "stations.log")).read()
for s in wanted:
    kind = "area" if s.get("polygon") else "point"
    rec = series.get(s["id"])
    if not rec or not rec.get("samples"):
        fail(f"{kind} {s['id']}: no timeseries")
    samples = rec["samples"]
    if len(samples) < hours:  # init + each hour, spin-up frame may be skipped
        fail(f"{kind} {s['id']}: {len(samples)} sample(s) for a {hours} h run")
    for stamp, v in samples.items():
        spd = v[0]
        if not (isinstance(spd, (int, float)) and math.isfinite(spd) and 0 <= spd < 150):
            fail(f"{kind} {s['id']} at {stamp}: non-physical speed {spd!r}")
    print(f"{kind} {s['id']}: {len(samples)} samples, last {spd:.1f} kt")

# Every polygon must have been read as an area over water cells, not have
# fallen back to its centroid: the extractor says so on one line.
areas = [s["id"] for s in wanted if s.get("polygon")]
if areas:
    line = next((l for l in log.splitlines() if "insight area(s) read over native water cells" in l), None)
    if line is None:
        fail("polygon stations were not read as areas (the extractor logged no area line)")
    for sid in areas:
        if f"{sid} (" not in line:
            fail(f"area {sid} fell back to a point; areas read: {line.split(':', 1)[-1].strip()}")
    print(line.split('] ', 1)[-1])
print("stations OK")
PY
