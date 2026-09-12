#!/bin/bash
# Compile WPS (geogrid, ungrib, metgrid, avg_tsfc) against the WRF tree built
# by build_wrf.sh. WPS links WRF's I/O libraries, so it must use the same
# compiler — a mismatch shows up as unresolved symbols or unreadable .mod
# files, not as a clean error.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=toolchain.sh
. "$here/toolchain.sh"

cd "${WPS_SRC:-/build/WPS}"

# WPS 4.6.0's init_output_fields uses is_subgrid_var uninitialized; under
# GCC >= 14.2 this silently produces EMPTY met_em files. Same one-line init
# as upstream's fix_metgrid_output_uninit_var (in 4.7.0, not the 4.6 tag).
sed -i "s/^\( *\)optstatus = 0$/\1optstatus = 0\n\1is_subgrid_var = .false./" \
  metgrid/src/output_module.F
grep -c "is_subgrid_var = .false." metgrid/src/output_module.F

# WPS's GRIB2 decoder needs the JasPer 2 jpc_* API (see Dockerfile).
export JASPERINC=/opt/jasper/include JASPERLIB=/opt/jasper/lib

# WPS 4.6.0 ships no aarch64 configure entry (its menu filters on `uname -m`,
# so on arm it would offer nothing). The x86_64 gfortran block is entirely
# arch-neutral — clone it under an aarch64 header and select that. Guarded on
# the header so a rerun or a future WPS that grows its own entry is a no-op.
if [ "$(uname -m)" != "x86_64" ] \
    && ! grep -q '^#ARCH.*Linux aarch64, gfortran' arch/configure.defaults; then
  python3 - <<'EOF'
import re
path = "arch/configure.defaults"
text = open(path).read()
m = re.search(r"^#ARCH\s+Linux x86_64, gfortran.*?(?=^#{10,})", text, re.M | re.S)
if not m:
    raise SystemExit("x86_64 gfortran block not found in WPS configure.defaults")
block = m.group(0).replace("Linux x86_64, gfortran", "Linux aarch64, gfortran", 1)
open(path, "a").write("\n" + block + "#" * 120 + "\n")
print("appended aarch64 gfortran entry to WPS configure.defaults")
EOF
fi

opt="$(python3 "$here/select_build_option.py" wps "$TC_WPS_DESC" serial)"
echo "==> WPS configure: option $opt ($WRF_TOOLCHAIN, serial)"
printf '%s\n' "$opt" | ./configure

# Fedora keeps the netCDF Fortran module out of the default search path, and
# WPS's legacy Fortran needs modern gfortran's strictness relaxed (both are
# folded into TC_WPS_FFLAGS, which is empty for toolchains that need neither).
if [ -n "$TC_WPS_FFLAGS" ]; then
  sed -i -e "s|^\(FFLAGS[[:space:]]*=\)|\1 $TC_WPS_FFLAGS |" \
         -e "s|^\(F77FLAGS[[:space:]]*=\)|\1 $TC_WPS_FFLAGS |" configure.wps
fi
if [ -n "$TC_WPS_LDFLAGS" ]; then
  sed -i "s|^\(LDFLAGS[[:space:]]*=\)|\1 $TC_WPS_LDFLAGS |" configure.wps
fi

# WPS itself is serial, but it links WRF's I/O libraries, and those carry MPI
# references once WRF is built dmpar/dm+sm. These go at the END of WRF_LIB
# (which terminates in -lnetcdf) rather than in LDFLAGS: ld resolves left to
# right, so a library listed ahead of the objects that need it is dropped.
# Nothing calls into MPI on the code paths WPS actually uses.
tail_libs="-lnetcdff"
case "$WRF_PARALLEL" in
  dmpar|dm+sm) tail_libs="$tail_libs $TC_MPI_LINK" ;;
esac
sed -i "s|-lnetcdf$|-lnetcdf $tail_libs|" configure.wps

echo "==> effective build settings"
grep -E '^(SFC|SCC|FFLAGS|LDFLAGS|WRF_DIR)[[:space:]]*=' configure.wps || true

./compile > compile.log 2>&1 || {
  echo '=== WPS COMPILE FAILED ==='
  grep -B2 -A6 -i 'error' compile.log | head -120
  exit 1
}
tail -5 compile.log
ls -l geogrid/src/geogrid.exe ungrib/src/ungrib.exe metgrid/src/metgrid.exe \
  util/src/avg_tsfc.exe
