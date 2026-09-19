#!/bin/bash
# Prove a built forge image carries exactly the local path and boots as an
# unprivileged user. Runs in CI on every per-arch build before the manifest
# is assembled and published.
#
#   scripts/verify-forge-image.sh ghcr.io/frothy-surf/forge:<sha>-amd64
#
# Runs the image with its own architecture (docker picks it from the ref),
# so on the arm64 runner this also proves the arm64 build boots.
set -euo pipefail
image="${1:?usage: verify-forge-image.sh <image-ref>}"

docker run --rm --entrypoint sh "$image" -c '
set -e
# exactly the local path, nothing else: three modules beside the runner,
# three worker helpers, no region configs, no stray build-context files
[ ! -e /opt/blip/regions ] || { echo "region configs present in forge image"; exit 1; }
want_bin="local_run.py local_telemetry.py runner.py"
got_bin="$(cd /opt/blip/bin && ls *.py | sort | tr "\n" " " | sed "s/ $//")"
[ "$got_bin" = "$want_bin" ] || { echo "bin/ python set is [$got_bin], expected [$want_bin]"; exit 1; }
want_worker="crash_classify.py extract_wind.py hrrr_fetch.py"
got_worker="$(cd /opt/blip/worker && ls -A | sort | tr "\n" " " | sed "s/ $//")"
[ "$got_worker" = "$want_worker" ] || { echo "worker/ set is [$got_worker], expected [$want_worker]"; exit 1; }
for f in tables/GEOGRID.TBL.ARW tables/METGRID.TBL tables/Vtable.GFS tables/Vtable.HRRR tables/Vtable.RRFS tables/Vtable.ERA5 bin/wrf.exe bin/real.exe bin/geogrid.exe bin/ungrib.exe bin/metgrid.exe; do
  [ -e "/opt/blip/$f" ] || { echo "missing from forge image: $f"; exit 1; }
done
[ -z "${REGION:-}" ] || { echo "REGION env leaked into forge image: $REGION"; exit 1; }
if find /opt/blip -name "__pycache__" -o -name ".env*" -o -name ".wrangler" -o -name ".DS_Store" | grep .; then
  echo "stray build-context files above"; exit 1
fi
# the binaries must be for this machine, and the model id must say so
arch="$(uname -m)"
case "$arch:$BLIP_MODEL_ID" in
  x86_64:*_x86-64-v3_*|aarch64:*_armv8.2-a_*|aarch64:*_native_*) ;;
  *) echo "BLIP_MODEL_ID $BLIP_MODEL_ID does not match architecture $arch"; exit 1 ;;
esac
python3 -c "import sys; sys.path.insert(0, \"/opt/blip/bin\"); import runner, local_run, local_telemetry"
python3 /opt/blip/bin/local_run.py --help > /dev/null
# unprivileged by default, with a HOME any --user uid can write to
[ "$(id -u)" != 0 ] || { echo "forge image runs as root"; exit 1; }
[ -w "$HOME" ] || { echo "HOME=$HOME is not writable for uid $(id -u)"; exit 1; }
echo "forge image OK on $arch ($BLIP_MODEL_ID) as uid $(id -u)"'

# The run path itself, as a Linux host would drive it: the sample bundle
# from this checkout, an out dir owned by an arbitrary host uid, --user set
# to that uid (no passwd entry in the image). --dry-run stages the workdir
# (namelists, tables, WRF run files) and touches no network.
here="$(cd "$(dirname "$0")" && pwd)"
bundle="$here/../examples/bundle"
out="$(mktemp -d)"
chmod 777 "$out"
docker run --rm --user 4242:4242 \
  -v "$bundle:/bundle:ro" -v "$out:/out" \
  "$image" --dry-run --no-serve --no-telemetry --hours 6 > "$out/dry-run.log" 2>&1 \
  || { cat "$out/dry-run.log"; echo "dry run failed as uid 4242"; exit 1; }
grep -q "dry run: workdir staged" "$out/dry-run.log" || { cat "$out/dry-run.log"; exit 1; }
# namelists/tables are copies; WRF run files (LANDUSE.TBL, ...) are symlinks
# to /opt/blip/wrf_run, dangling from the host's point of view — hence -L
# The three .dat files are the precomputed Thompson microphysics tables:
# without them staged, wrf.exe spends minutes recomputing them per run.
for f in namelist.wps namelist.input GEOGRID.TBL METGRID.TBL Vtable LANDUSE.TBL \
         qr_acr_qg_V4.dat qr_acr_qsV2.dat freezeH2O.dat; do
  [ -e "$out/work/$f" ] || [ -L "$out/work/$f" ] \
    || { echo "dry run did not stage $f"; ls "$out/work"; exit 1; }
done
owner="$(stat -c %u "$out/work/namelist.input" 2>/dev/null || stat -f %u "$out/work/namelist.input")"
[ "$owner" = 4242 ] || { echo "staged files owned by uid $owner, expected 4242"; exit 1; }
echo "dry run staged a bundle as uid 4242 with host-owned output"
# the staged files belong to 4242, so the host user can't delete them; let
# that uid do it, and don't let cleanup fail a check that already passed
docker run --rm --user 4242:4242 -v "$out:/out" --entrypoint rm "$image" -rf /out/work || true
rm -rf "$out" 2>/dev/null || true
