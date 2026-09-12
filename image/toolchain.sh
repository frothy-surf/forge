# Toolchain-specific build environment, sourced by build_wrf.sh / build_wps.sh.
#
# Inputs (env, set from Dockerfile build args):
#   WRF_TOOLCHAIN  gnu | intel
#   WRF_PARALLEL   smpar | dmpar | dm+sm
#   WRF_MARCH      ISA target, e.g. x86-64-v3 (never 'native' in CI)
#
# Exports the compilers, the ./configure menu description to match, the
# optimization flags, and the MPI wrapper fixups each toolchain needs.

set -euo pipefail

: "${WRF_TOOLCHAIN:=gnu}"
: "${WRF_PARALLEL:=dm+sm}"
: "${WRF_MARCH:=x86-64-v3}"

# Does $FC accept this ISA flag? ifx is LLVM-based and generally takes the
# same -march= names as gcc, but that is a promise about a compiler version
# we do not pin, and a silent fallback beats a build failure. Deliberately
# -march= and never -xHost/-xCORE-AVX2: Intel's -x flags emit a CPU dispatch
# check that refuses to run on AMD, and we do not control which vendor's
# silicon the replica lands on.
probe_fc_flag() {
  local flag="$1" tmp
  tmp="$(mktemp -d)"
  printf 'program t\nend program t\n' > "$tmp/t.f90"
  if (cd "$tmp" && $FC $flag t.f90 -o t) >/dev/null 2>&1; then
    rm -rf "$tmp"; return 0
  fi
  rm -rf "$tmp"; return 1
}

# ISA flag for this build. x86 spells it -march=; on aarch64 a CPU name
# (neoverse-n1, apple-m1) goes through -mcpu= and an ISA level (armv8.2-a,
# the portable public-image baseline: Graviton2 and every Apple Silicon
# chip) through -march=. WRF_MARCH=native is right for an image built on
# the machine that will run it (a host building an image for itself) and is tried
# ONLY when asked for — CI must never use it, and a silent fallback to it
# would ship an image tuned to whatever the build runner happened to be.
#
# The Dockerfile default (x86-64-v3) is an x86 name: an arm build must be
# handed an arm one explicitly, and stops here rather than baking a
# BLIP_MODEL_ID that claims an ISA the binaries do not have.
march_flag() {
  local f candidates
  case "$(uname -m)" in
  aarch64|arm64)
    case "$WRF_MARCH" in
      x86*|core-avx*|*znver*|*haswell*|*skylake*)
        echo "WRF_MARCH='$WRF_MARCH' is an x86 target but this is $(uname -m):" \
             "pass --build-arg WRF_MARCH=armv8.2-a (portable) or =native" >&2
        exit 2 ;;
      native) candidates=("-mcpu=native") ;;
      *)      candidates=("-mcpu=${WRF_MARCH}" "-march=${WRF_MARCH}" "-march=armv8.2-a") ;;
    esac
    ;;
  *)
    candidates=("-march=${WRF_MARCH}" "-march=core-avx2")
    ;;
  esac
  for f in "${candidates[@]}"; do
    if probe_fc_flag "$f"; then echo "$f"; return; fi
  done
  echo "WARNING: no usable -march flag; building for the generic baseline" >&2
  echo ""
}

case "$WRF_TOOLCHAIN" in
gnu)
  export CC=gcc CXX=g++ FC=gfortran F77=gfortran
  export PATH=/usr/lib64/openmpi/bin:$PATH
  export LD_LIBRARY_PATH=/usr/lib64/openmpi/lib:${LD_LIBRARY_PATH:-}
  export NETCDF=/usr
  # The generic GNU entry lists every arch including aarch64, so one
  # description works everywhere (and it carries the FCCOMPAT hook WRF's
  # configure uses to slip in gfortran>=10 compat flags — the dedicated
  # "GCC: Aarch64" entry does not). WPS 4.6.0 ships no aarch64 entry at all;
  # build_wps.sh appends one cloned from this x86_64 block, under this name.
  TC_WRF_DESC='GNU \(gfortran/gcc\)'
  case "$(uname -m)" in
    aarch64|arm64) TC_WPS_DESC='Linux aarch64, gfortran' ;;
    *)             TC_WPS_DESC='Linux x86_64, gfortran' ;;
  esac
  # Fedora keeps gfortran's netCDF module out of the default search path.
  TC_WPS_FFLAGS='-I/usr/lib64/gfortran/modules -fallow-argument-mismatch -fallow-invalid-boz'
  TC_WPS_LDFLAGS='-fopenmp -L/usr/lib64/openmpi/lib'
  # rpath, not just -L: WPS links with plain gfortran rather than the mpif90
  # wrapper, so nothing records where these libraries live and the
  # executables would resolve them only when LD_LIBRARY_PATH happens to be set.
  TC_MPI_LINK='-L/usr/lib64/openmpi/lib -Wl,-rpath,/usr/lib64/openmpi/lib -lmpi_mpifh -lmpi'
  TC_FCOPTIM="-O3 $(march_flag) -funroll-loops"
  # WRF's generated sources are enormous; gfortran's f951 peaked near 1.2 GB
  # on one of them at -O3. Parallel jobs multiply that, and an OOM-killed
  # compiler surfaces only as "Problems building executables" at the very end.
  TC_BUILD_JOBS="${BUILD_JOBS:-6}"

  # configure.defaults hardcodes `mpif90 -f90=$(SFC)` / `mpicc -cc=$(SCC)`.
  # Those are MPICH/Intel-MPI wrapper flags; OpenMPI's wrappers pass them
  # through to the compiler, which rejects them outright ("unrecognized
  # command-line option '-f90=gfortran'"). Strip the flags and keep the
  # wrapper, which already picks up gfortran/gcc by default.
  fix_dm_wrappers() {
    sed -i -e 's| -f90=\$(SFC)||' -e 's| -cc=\$(SCC)||' configure.wrf
  }
  ;;
intel)
  if [ "$(uname -m)" != "x86_64" ]; then
    echo "WRF_TOOLCHAIN=intel is x86_64-only (ifx/icx have no aarch64 port)" >&2
    exit 2
  fi
  # setvars.sh references plenty of unset variables; -u must be off for it.
  set +u
  # shellcheck disable=SC1091
  . /opt/intel/oneapi/setvars.sh >/dev/null
  set -u
  export CC=icx CXX=icpx FC=ifx F77=ifx
  # Fedora's netcdf-fortran is gfortran-built, and ifx cannot read gfortran
  # .mod files — build_netcdf_fortran.sh rebuilds it with ifx into /opt/netcdf.
  export NETCDF=/opt/netcdf
  TC_WRF_DESC='INTEL \(ifx/icx\)'
  TC_WPS_DESC='Linux x86_64, Intel oneAPI compilers'
  TC_WPS_FFLAGS=''
  TC_WPS_LDFLAGS=''
  # See the GNU branch: WPS needs the MPI library path recorded in the binary.
  _mpi_lib="${I_MPI_ROOT:-/opt/intel/oneapi/mpi/latest}/lib"
  TC_MPI_LINK="-L$_mpi_lib -Wl,-rpath,$_mpi_lib -lmpifort -lmpi"
  TC_FCOPTIM="-O3 $(march_flag)"
  # ifx's backend is far hungrier than gfortran's: xfortcom was OOM-killed at
  # 6.4 GB resident on a single generated source here. Even a few of those at
  # once will exhaust a 16 GB CI runner, so the Intel build stays narrow.
  TC_BUILD_JOBS="${BUILD_JOBS:-2}"

  # Intel MPI's mpif90 wraps *gfortran*; mpiifx/mpiifort wrap ifx. Name the
  # Intel wrapper explicitly rather than relying on -f90= dispatch.
  _mpifc=mpiifx; command -v mpiifx >/dev/null || _mpifc=mpiifort
  _mpicc=mpiicx; command -v mpiicx >/dev/null || _mpicc=mpiicc
  fix_dm_wrappers() {
    sed -i -e "s|^\(DM_FC[[:space:]]*=\).*|\1 $_mpifc|" \
           -e "s|^\(DM_CC[[:space:]]*=\).*|\1 $_mpicc|" configure.wrf
  }
  ;;
*)
  echo "unknown WRF_TOOLCHAIN '$WRF_TOOLCHAIN' (gnu | intel)" >&2
  exit 2
  ;;
esac

export TC_WRF_DESC TC_WPS_DESC TC_WPS_FFLAGS TC_WPS_LDFLAGS TC_MPI_LINK \
  TC_FCOPTIM TC_BUILD_JOBS
