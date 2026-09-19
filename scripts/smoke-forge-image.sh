#!/bin/bash
# Run one real forecast through a forge image, end to end, the way a user
# would: a bundle, a WPS_GEOG mount, an out dir — geogrid, a live GFS fetch,
# ungrib, metgrid, real, wrf, and the wind products. CI runs it per
# architecture before anything is published; it works the same by hand.
#
#   scripts/smoke-forge-image.sh <image-ref> <input-dir>
#
# The input is a forge bundle sized to integrate in seconds (see
# examples/smoke/). Static data is UCAR's low-resolution mandatory set
# (~150 MB), fetched once into $SMOKE_HOME and run with --geog-res lowres:
# the test validates the image's machinery, not the terrain.
#
# Everything lives under $SMOKE_HOME (default ~/.cache/forge-smoke) rather
# than a system temp dir: Docker on macOS and Windows shares the home
# directory with its VM, not /tmp.
set -euo pipefail
image="${1:?usage: smoke-forge-image.sh <image-ref> <input-dir>}"
input="$(cd "${2:?usage: smoke-forge-image.sh <image-ref> <input-dir>}" && pwd)"
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
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$input:/bundle:ro" -v "$geog:/geog:ro" -v "$out:/out" \
  "$image" --hours "$hours" --geog-res lowres --no-serve --no-telemetry 2>&1 | tee "$out/run.log" \
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
