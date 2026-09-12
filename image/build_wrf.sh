#!/bin/bash
# Compile WRF (real.exe, wrf.exe) from source for the configured toolchain
# and parallelism mode. Kept out of the Dockerfile so the same steps can be
# rerun by hand inside a build container.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=toolchain.sh
. "$here/toolchain.sh"

cd "${WRF_SRC:-/build/WRF}"

# MYNN psi lookup tables are indexed with int(zolf*100) guarded only on the
# high side; a blown-up z/L (or int(NaN)) goes negative and segfaults instead
# of surfacing a model error. Clamp the low side so out-of-range falls through
# to the analytic psi*_full branch.
sed -i 's|if(nzol+1 .le. 1000)then|if(nzol .ge. 0 .and. nzol+1 .le. 1000)then|' \
  phys/module_sf_mynn.F
[ "$(grep -c 'nzol .ge. 0 .and.' phys/module_sf_mynn.F)" = 4 ] || {
  echo "MYNN psi-table guard did not apply (expected 4 sites)"; exit 1; }

opt="$(python3 "$here/select_build_option.py" wrf "$TC_WRF_DESC" "$WRF_PARALLEL")"
echo "==> WRF configure: option $opt ($WRF_TOOLCHAIN, $WRF_PARALLEL)"
# Second answer is configure's nesting prompt: 1 = basic nesting.
printf '%s\n1\n' "$opt" | ./configure

# configure.defaults ships -O2 and no ISA target. CI must never use
# -march=native: the build runner is not the machine that runs the forecast.
sed -i "s|^\(FCOPTIM[[:space:]]*=\).*|\1 $TC_FCOPTIM|" configure.wrf

# On arm, ask for output that bit-matches the x86 build (upstream validated
# this define as producing identical model output between the two): it makes
# an arm build's forecasts comparable with the x86 build's rather than a third variant.
if [ "$(uname -m)" != "x86_64" ]; then
  sed -i "s|^\(ARCH_LOCAL[[:space:]]*=.*\)|\1 -DAARCH64_X86_CORRECTNESS_FIX|" configure.wrf
fi

case "$WRF_PARALLEL" in
  dmpar|dm+sm) fix_dm_wrappers ;;
esac

echo "==> effective build settings"
grep -E '^(SFC|SCC|DM_FC|DM_CC|FCOPTIM|OMP)[[:space:]]*=' configure.wrf || true

# ./compile exits 0 even when compilation fails, and ends with a few hundred
# lines of harmless symlink chatter — so the binaries are the real test, and a
# plain tail of the output shows nothing useful when it breaks.
echo "==> compiling with -j $TC_BUILD_JOBS"
./compile -j "$TC_BUILD_JOBS" em_real > compile.log 2>&1 || true
if [ ! -x main/wrf.exe ] || [ ! -x main/real.exe ]; then
  echo '=== WRF COMPILE FAILED ==='
  # WRF's build tolerates some sub-target failures and marks them
  # "Error N (ignored)" — diffwrf's netCDF F77 link is one, and it fails the
  # same way in a working build. Report the error make did NOT ignore, or the
  # whole picture goes missing under dozens of harmless linker lines.
  fatal=$(grep -nE 'Error [0-9]+$' compile.log | grep -v ignored | head -3)
  if [ -n "$fatal" ]; then
    echo "$fatal"
    line=$(echo "$fatal" | head -1 | cut -d: -f1)
    echo '--- preceding output ---'
    sed -n "$(( line > 80 ? line - 80 : 1 )),${line}p" compile.log | tail -40
  else
    # No make error at all usually means a compiler was OOM-killed: the job
    # dies without writing a diagnostic and the build limps on to the end.
    echo "(no fatal make error found — a compiler was most likely OOM-killed;"
    echo " retry with a lower BUILD_JOBS, currently $TC_BUILD_JOBS)"
    grep -nE 'error #|Fatal|undefined reference|No rule to make' compile.log | head -20
  fi
  exit 1
fi
tail -3 compile.log
ls -l main/wrf.exe main/real.exe
