#!/bin/bash
# Rebuild netCDF-Fortran with the Intel compiler (Intel toolchain only).
#
# Fortran .mod files are compiler-private: Fedora's netcdf-fortran is built
# with gfortran and ifx cannot read its modules, so an Intel WRF build fails
# at the first `use netcdf`. netCDF's C library is compiler-neutral, so only
# the Fortran layer is rebuilt; both are collected under one prefix because
# WRF expects a single $NETCDF with include/ and lib/.
set -euo pipefail

: "${NETCDF_FORTRAN_VERSION:=4.6.1}"
prefix=/opt/netcdf

set +u
# shellcheck disable=SC1091
. /opt/intel/oneapi/setvars.sh >/dev/null
set -u
export CC=icx FC=ifx F77=ifx

# System netCDF-C, adopted into the single prefix WRF will be pointed at.
mkdir -p "$prefix/include" "$prefix/lib"
cp -a /usr/include/netcdf.h "$prefix/include/"
cp -a /usr/lib64/libnetcdf.so* "$prefix/lib/"

curl -sL "https://downloads.unidata.ucar.edu/netcdf-fortran/${NETCDF_FORTRAN_VERSION}/netcdf-fortran-${NETCDF_FORTRAN_VERSION}.tar.gz" \
  | tar xz
cd "netcdf-fortran-${NETCDF_FORTRAN_VERSION}"
./configure --prefix="$prefix" --disable-shared --enable-static \
  CPPFLAGS="-I$prefix/include" LDFLAGS="-L$prefix/lib" LIBS="-lnetcdf"
make -j "${BUILD_JOBS:-8}"
make install
cd ..
rm -rf "netcdf-fortran-${NETCDF_FORTRAN_VERSION}"

ls -l "$prefix/lib/libnetcdff.a" "$prefix/include/netcdf.mod"
