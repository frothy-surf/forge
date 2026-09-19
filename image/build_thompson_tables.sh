#!/bin/bash
# Precompute the Thompson microphysics lookup tables so they ship in the
# image. WRF computes them at the start of any run with mp_physics 8 or 28
# (physics_suite='CONUS' selects 8) unless the three .dat files are already
# in the run directory — about five minutes on one core, which a run in a
# fresh work dir or a fresh container would otherwise pay every time.
#
# WRF has no standalone generator, so thompson_tables.f90 calls the
# scheme's own init routine, compiled and linked exactly as main/Makefile
# builds wrf.exe (same configure.wrf, same libwrflib.a). The files are
# Fortran unformatted output, so they belong to this build: generate them
# per image, never copy them between toolchains or architectures.
#
# Runs after build_wrf.sh, in the same tree. Output: $THOMPSON_OUT.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=toolchain.sh
. "$here/toolchain.sh"

wrf="${WRF_SRC:-/build/WRF}"
out="${THOMPSON_OUT:-/build/thompson_tables}"
tables=(qr_acr_qg_V4.dat qr_acr_qsV2.dat freezeH2O.dat)

cp "$here/thompson_tables.f90" "$wrf/main/thompson_tables.f90"
# A makefile beside main/Makefile, so configure.wrf's relative -L paths
# resolve the way they do for wrf.exe. WRF_SRC_ROOT_DIR, which its module
# search path is written in terms of, normally comes from the top-level
# Makefile. Recipe lines need literal tabs.
printf '%s\n' \
  'include ../configure.wrf' \
  'thompson_tables.exe: thompson_tables.f90 $(LIBWRFLIB)' \
  '	$(FC) -o thompson_tables.o -c $(FCFLAGS) $(OMP) $(INCLUDE_MODULES) $(PROMOTION) $(FCSUFFIX) thompson_tables.f90' \
  '	$(LD) -o $@ $(LDFLAGS) thompson_tables.o $(LIBWRFLIB) $(LIB)' \
  > "$wrf/main/thompson_tables.mk"
echo "==> building thompson_tables.exe"
make -C "$wrf/main" -r -f thompson_tables.mk WRF_SRC_ROOT_DIR="$wrf" thompson_tables.exe > "$wrf/thompson_tables.log" 2>&1 || {
  tail -40 "$wrf/thompson_tables.log"; exit 1; }

rm -rf "$out"
mkdir -p "$out"
cd "$out"
# WRF's MPI/communicator setup reads the domain count and the I/O-server
# layout from namelist.input before anything else runs (an MPI build aborts
# without the quilt group); nothing else in the file is consulted here.
printf '%s\n' ' &domains' ' max_dom = 1,' ' /' ' &dfi_control' ' /' \
  ' &namelist_quilt' ' nio_tasks_per_group = 0,' ' nio_groups = 1,' ' /' > namelist.input
# The graupel table (nearly all of the cost) is divided among MPI ranks and
# gathered on rank 0, which writes the files, so an MPI build can spread it
# over a few cores. Only a few: every rank allocates all three tables
# (~375 MB), and a rank per core on a wide builder is gigabytes for a step
# that four ranks finish in about a minute. The transport pins match the
# runtime image's (see Dockerfile): left alone, OpenMPI probes fabrics a
# build container does not have.
ranks="$(nproc)"
[ "$ranks" -le 4 ] || ranks=4
case "$WRF_PARALLEL" in
  dmpar|dm+sm)
    if [ "$WRF_TOOLCHAIN" = "gnu" ]; then
      export OMPI_MCA_pml=ob1 OMPI_MCA_btl=self,sm,tcp
      launch=(mpirun --allow-run-as-root --oversubscribe -np "$ranks")
    else
      launch=(mpirun -np "$ranks")
    fi ;;
  *) launch=() ;;
esac
echo "==> computing tables: ${launch[*]:-serial}"
OMP_NUM_THREADS=1 "${launch[@]}" "$wrf/main/thompson_tables.exe" > run.log 2>&1 || {
  tail -40 run.log; cat rsl.error.0000 2>/dev/null | tail -40; exit 1; }

# Read them back with reading forced: WRF silently recomputes a table it
# cannot read, so a bad file would otherwise just bring the five minutes back.
for t in "${tables[@]}"; do
  [ -s "$t" ] || { echo "thompson table $t was not written"; tail -40 run.log; exit 1; }
done
"$wrf/main/thompson_tables.exe" check > check.log 2>&1 || {
  echo "thompson tables do not read back"; tail -40 check.log; exit 1; }
find . -mindepth 1 ! -name '*.dat' -delete
ls -l "$out"
