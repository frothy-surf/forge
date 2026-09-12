# frothy compute image: stock, unpatched WRF/WPS built from source, plus our
# run orchestration and wind-product extraction.
#
# Stages:
#   build    compile WRF + WPS from source
#   base     runtime deps + executables + stock tables (nothing of ours)
#   forge    PUBLIC local-mode image: base + local_run.py and the few
#            modules it needs — treat its contents as public.
#   runtime  the hosted-service image: base + everything (job worker,
#            evaluation tooling, region configs)
# CI builds --target runtime for the hosted service and --target forge for
# the public tag; a forge build can never pick up hosted-only files by
# accident because it does not derive from runtime.
#
# Build from the repo root (the .dockerignore allowlist keeps the context
# to image/ worker/ regions/ — untracked files there are still excluded):
#   docker build -f image/Dockerfile --target runtime -t blip/compute:PNW \
#     --build-arg WRF_MARCH=native .
#
# Architectures: the public forge image is published for linux/amd64 and
# linux/arm64 (native builds on each; nothing is emulated). WRF_MARCH is an
# ISA name for the target, so an arm64 build must pass one — CI uses
# armv8.2-a (runs on Graviton2+ and every Apple Silicon chip); a Mac building
# its own image can use native. The x86 default is refused on arm
# (image/toolchain.sh march_flag) rather than mislabelling the build.
#
# Two build axes, both benchmarkable against each other:
#   WRF_PARALLEL   dm+sm (default) | dmpar | smpar
#   WRF_TOOLCHAIN  gnu (default)   | intel
# Prod runs GNU dm+sm. smpar reproduces the pre-MPI baseline; the Intel build
# is opt-in and pulls a multi-GB oneAPI toolchain at build time.

ARG FEDORA_VERSION=42
# The multi-arch index digest of fedora:42 (amd64 + arm64) — pinned so the
# base a puller sees in the provenance is the base that was built on, and
# so a moved tag cannot change the image without a commit here. dnf still
# installs whatever the Fedora mirrors carry at build time; the SBOM records
# the exact package set. Refresh with:
#   docker buildx imagetools inspect fedora:42 --format '{{.Manifest.Digest}}'
ARG FEDORA_DIGEST=sha256:99e203b80b1c3d8f7e161ec10a68fd02b081ef83a3963553e513c82846b97814

# ---------- build stage: compile WRF + WPS from source ----------
FROM fedora:${FEDORA_VERSION}@${FEDORA_DIGEST} AS build

ARG WRF_VERSION=v4.7.1
ARG WPS_VERSION=v4.6.0
ARG WRF_MARCH=x86-64-v3
ARG WRF_TOOLCHAIN=gnu
ARG WRF_PARALLEL=dm+sm
# Compile parallelism. Left empty so toolchain.sh can pick a default sized to
# how much memory that compiler needs per source file — WRF's generated
# sources are big enough that too many jobs get the compiler OOM-killed, which
# shows up only as "Problems building executables" after an hour.
ARG BUILD_JOBS=

RUN dnf install -y \
  gcc gcc-c++ gcc-gfortran make m4 csh time which git perl hostname python3 \
  netcdf-devel netcdf-fortran-devel libpng-devel jasper-devel openmpi-devel \
  && dnf clean all

# WRF's own build uses jasper for GRIB2 I/O; Fedora's 4.x is fine there (only
# WPS's ungrib needs the removed jpc_* API — see the JasPer 2 stage below).
ENV JASPERINC=/usr/include/jasper JASPERLIB=/usr/lib64 \
  WRF_TOOLCHAIN=${WRF_TOOLCHAIN} WRF_PARALLEL=${WRF_PARALLEL} \
  WRF_MARCH=${WRF_MARCH} BUILD_JOBS=${BUILD_JOBS}

# Intel oneAPI, only when asked for: several GB of compiler the GNU build has
# no use for. ifx/icx come from Intel's own repo (Fedora ships neither).
# Package signatures are still checked (gpgcheck); repo_gpgcheck is off
# because Intel does not sign the repo metadata in a way dnf5 accepts, and
# with it on the metadata silently loads empty — every package "not found".
RUN if [ "$WRF_TOOLCHAIN" = "intel" ]; then \
      printf '%s\n' \
        '[oneAPI]' \
        'name=Intel oneAPI' \
        'baseurl=https://yum.repos.intel.com/oneapi' \
        'enabled=1' \
        'gpgcheck=1' \
        'repo_gpgcheck=0' \
        'gpgkey=https://yum.repos.intel.com/intel-gpg-keys/GPG-PUB-KEY-INTEL-SW-PRODUCTS.PUB' \
        > /etc/yum.repos.d/oneAPI.repo \
      && dnf install -y intel-oneapi-compiler-fortran \
           intel-oneapi-compiler-dpcpp-cpp intel-oneapi-mpi-devel \
      && dnf clean all; \
    fi

WORKDIR /build
COPY image/build_netcdf_fortran.sh /build/image/
RUN if [ "$WRF_TOOLCHAIN" = "intel" ]; then bash image/build_netcdf_fortran.sh; fi

RUN git clone --branch ${WRF_VERSION} --depth 1 https://github.com/wrf-model/WRF.git \
  && git clone --branch ${WPS_VERSION} --depth 1 https://github.com/wrf-model/WPS.git

# Copied immediately before use so that editing the WPS build (below) does not
# invalidate this ~60 min layer.
COPY image/toolchain.sh image/select_build_option.py image/build_wrf.sh /build/image/
RUN bash image/build_wrf.sh

# WPS's GRIB2 decoder needs JasPer's internal jpc_* API, removed in JasPer 3+
# (Fedora ships 4.x). Build 2.0.33 statically for WPS alone; WRF above is
# jasper-free, so this sits after the long WRF layer to keep it cached.
RUN dnf install -y cmake && dnf clean all \
  && curl -sL https://github.com/jasper-software/jasper/archive/refs/tags/version-2.0.33.tar.gz | tar xz \
  && cd jasper-version-2.0.33 \
  && cmake -B build -DCMAKE_INSTALL_PREFIX=/opt/jasper -DCMAKE_INSTALL_LIBDIR=lib \
       -DJAS_ENABLE_SHARED=false -DJAS_ENABLE_OPENGL=false -DJAS_ENABLE_DOC=false \
  && cmake --build build -j 8 && cmake --install build \
  && cd .. && rm -rf jasper-version-2.0.33

COPY image/build_wps.sh /build/image/
RUN bash image/build_wps.sh

# ---------- base stage: runtime deps, executables, stock tables ----------
# Nothing frothy-specific lives here beyond the build-identity env; both the
# public forge image and the hosted-service image start from it.
FROM fedora:${FEDORA_VERSION}@${FEDORA_DIGEST} AS base
ARG IMAGE_REV=dev
ARG WRF_VERSION=v4.7.1
ARG WPS_VERSION=v4.6.0
ARG WRF_MARCH=x86-64-v3
ARG WRF_TOOLCHAIN=gnu
ARG WRF_PARALLEL=dm+sm
# Bump MODEL_EPOCH when changing anything that alters model physics/results
# outside the namelists (source patches, compiler flags, dataset swaps):
# it invalidates outstanding checkpoints, forcing clean cold starts.
ARG MODEL_EPOCH=1

RUN dnf install -y \
  netcdf netcdf-fortran libpng jasper libgomp csh time hostname procps-ng \
  openmpi \
  python3-numpy python3-netcdf4 python3-pyproj python3-matplotlib \
  python3-scipy python3-tifffile \
  eccodes python3-eccodes \
  wgrib2 \
  && (dnf install -y python3-rasterio || echo "rasterio unavailable; GeoTIFF disabled") \
  && (dnf install -y python3-sentry-sdk || echo "sentry-sdk unavailable; crash events skipped") \
  && dnf clean all

# python3-eccodes dlopens the unversioned <prefix>/lib64/libeccodes.so with a
# hardcoded path (gribapi/bindings.py, no env override), but Fedora ships that
# symlink only in eccodes-devel — which drags gcc, gfortran and binutils
# (~230 MB) into the runtime image. Point it at whatever soname the runtime
# eccodes package installed instead, and import it here so a moved library
# fails the build rather than the first HRRR fetch.
RUN lib="$(ldconfig -p | awk '$1 ~ /^libeccodes\.so\./ {print $NF; exit}')" \
  && [ -n "$lib" ] && ln -sf "$lib" /usr/lib64/libeccodes.so \
  && python3 -c 'import eccodes; print("eccodes", eccodes.codes_get_api_version())'

# Intel-built executables need Intel's Fortran and MPI redistributables.
RUN if [ "$WRF_TOOLCHAIN" = "intel" ]; then \
      printf '%s\n' \
        '[oneAPI]' \
        'name=Intel oneAPI' \
        'baseurl=https://yum.repos.intel.com/oneapi' \
        'enabled=1' \
        'gpgcheck=1' \
        'repo_gpgcheck=0' \
        'gpgkey=https://yum.repos.intel.com/intel-gpg-keys/GPG-PUB-KEY-INTEL-SW-PRODUCTS.PUB' \
        > /etc/yum.repos.d/oneAPI.repo \
      && dnf install -y intel-oneapi-runtime-fortran intel-oneapi-mpi \
      && dnf clean all; \
    fi

# BLIP_MODEL_ID is the checkpoint-compatibility key (the hosted worker's
# checkpoint fingerprint). Toolchain and parallelism belong in it because either
# changes results, so switching variants cold-starts rather than resuming a
# checkpoint written by a different build.
# OpenMPI transport. Left to choose for itself, it loads libfabric's PSM3
# provider even though no such fabric exists in a container, and PSM3 fills the
# stock 64 MB /dev/shm with psm3_shm segments until MPI_Init dies of SIGBUS --
# a bus error whose stack trace never mentions shared memory. Pinning the
# plain shared-memory path is both the fix and the faster intra-node route
# (8 ranks then use ~4 MB of /dev/shm). Override per deploy if that changes.
ENV OMPI_MCA_pml=ob1 \
  OMPI_MCA_btl=self,sm,tcp
ENV BASEDIR=/opt/blip \
  BLIP_IMAGE_REV=${IMAGE_REV} \
  BLIP_MODEL_ID=WRF${WRF_VERSION}_WPS${WPS_VERSION}_${WRF_MARCH}_${WRF_TOOLCHAIN}_${WRF_PARALLEL}_e${MODEL_EPOCH} \
  BLIP_WRF_PARALLEL=${WRF_PARALLEL} \
  OMP_STACKSIZE=512M
WORKDIR $BASEDIR

# Executables
COPY --from=build /build/WRF/main/wrf.exe /build/WRF/main/real.exe $BASEDIR/bin/
COPY --from=build /build/WPS/geogrid/src/geogrid.exe /build/WPS/ungrib/src/ungrib.exe \
  /build/WPS/metgrid/src/metgrid.exe /build/WPS/util/src/avg_tsfc.exe $BASEDIR/bin/
# Static tables WRF reads at runtime (landuse, radiation, ozone, ...)
COPY --from=build /build/WRF/run/ $BASEDIR/wrf_run/
# WPS tables: stock geogrid/metgrid tables and the boundary-source variable
# tables. WPS names the RAP/HRRR table Vtable.raphrrr; the runner selects it
# as Vtable.HRRR for boundary_source=hrrr (see runner.py stage_workdir and
# the field selectors above download_hrrr).
COPY --from=build /build/WPS/geogrid/GEOGRID.TBL.ARW $BASEDIR/tables/GEOGRID.TBL.ARW
COPY --from=build /build/WPS/metgrid/METGRID.TBL.ARW $BASEDIR/tables/METGRID.TBL
COPY --from=build /build/WPS/ungrib/Variable_Tables/Vtable.GFS $BASEDIR/tables/Vtable.GFS
COPY --from=build /build/WPS/ungrib/Variable_Tables/Vtable.raphrrr $BASEDIR/tables/Vtable.HRRR
# RRFS forces from prslev (isobaric) + 2dfld (surface/RUC soil). No stock
# WPS table maps that pair — RAP.pressure has no soil rows ("Soil fields
# are in the hybrid-coordinate files"), so we ship a composite: RAP
# pressure atmosphere + raphrrr's RUC soil (see image/Vtable.RRFS).
COPY image/Vtable.RRFS $BASEDIR/tables/Vtable.RRFS
COPY image/Vtable.GFS_NOSOIL $BASEDIR/tables/Vtable.GFS_NOSOIL
# ERA5 (reanalysis hindcasts): the stock WPS Vtable.ECMWF, vendored to the
# repo so the mapping is reviewable next to Vtable.RRFS (image/Vtable.ERA5).
COPY image/Vtable.ERA5 $BASEDIR/tables/Vtable.ERA5

# Both MPI stacks are listed; only the one this image was built against
# exists, and a missing PATH/LD_LIBRARY_PATH entry is simply ignored.
ENV PATH=$BASEDIR/bin:/usr/lib64/openmpi/bin:/opt/intel/oneapi/mpi/latest/bin:$PATH \
  LD_LIBRARY_PATH=/usr/lib64/openmpi/lib:/opt/intel/oneapi/mpi/latest/lib \
  WORKER_DIR=$BASEDIR/worker \
  RUNNER=$BASEDIR/bin/runner.py

# ---------- public local-mode image ----------
# local_run.py takes a mounted forge bundle + WPS_GEOG and runs geogrid → wrf
# → product extraction with no hosted service and no uploads. Published as
# registry.frothy.surf/frothy-forge.
#
# Anything COPYed here is extractable by anyone who pulls the image, so this
# stage carries exactly what the local path executes and nothing else:
#   bin/     runner.py (orchestration), local_run.py, local_telemetry.py
#   worker/  extract_wind.py (products), crash_classify.py (failure hints),
#            hrrr_fetch.py (HRRR idx-subset download used by runner)
# Deliberately absent: regions/ (the hosted service's own region configs — a
# bundle is the only region source), the hosted job worker and its API
# client, evaluation and observation tooling, and initialization
# enrichments used only by the hosted service. ci-compute verifies that
# list against the built image.
FROM base AS forge
COPY image/runner.py image/local_run.py image/local_telemetry.py $BASEDIR/bin/
COPY worker/extract_wind.py worker/crash_classify.py worker/hrrr_fetch.py $BASEDIR/worker/
# Unprivileged by default. Everything the run writes goes under /out (a
# bind mount), so nothing in the image needs to be writable. The home is
# world-writable because `docker run --user <uid>:<gid>` — the way a Linux
# host gets output files owned by itself — lands on a uid with no passwd
# entry, and OpenMPI/PMIx and a mounted ~/.cdsapirc lookup both want a HOME
# that exists. The hosted-service image keeps running as root.
RUN useradd --uid 1000 --user-group --create-home --home-dir /home/frothy \
      --shell /sbin/nologin frothy \
  && chmod 1777 /home/frothy
ENV HOME=/home/frothy
USER frothy
ENTRYPOINT ["python3", "/opt/blip/bin/local_run.py"]
CMD []

# ---------- hosted-service image ----------
FROM base AS runtime
ARG REGION=PNW
ENV REGION=${REGION}

# All region/variant configs ship in the hosted image; the job's region
# field selects one at runtime.
COPY regions/ $BASEDIR/regions/

# Orchestration, extraction and the hosted job worker
COPY worker/ $BASEDIR/worker/
COPY image/runner.py image/local_run.py image/local_telemetry.py image/bench_layout.py image/geopolar_sst.py image/hrrr_soil.py $BASEDIR/bin/

CMD ["python3", "/opt/blip/worker/claim_loop.py"]
