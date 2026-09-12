# frothy forge image

This repository is the source of the public compute image behind
[forge.frothy.surf](https://forge.frothy.surf):
`registry.frothy.surf/frothy-forge` (also `ghcr.io/frothy-surf/forge`).
It builds stock, unpatched [WRF](https://github.com/wrf-model/WRF) and
[WPS](https://github.com/wrf-model/WPS) from source and adds the
orchestration that turns an exported forge bundle into a finished forecast
and wind products, with no account or hosted service involved.

The image is built, signed and published **from this repository's CI**, so
what you can read here is exactly what runs:

- `image/Dockerfile` and `image/*.sh` — the WRF/WPS build (GNU toolchain,
  hybrid MPI+OpenMP, native amd64 and arm64) and the runtime image.
- `image/runner.py` — run orchestration: forcing download (GFS, HRRR, RRFS,
  ERA5), namelist stamping, WPS → real → wrf.
- `image/local_run.py` — the image entrypoint for a mounted bundle, with a
  status page and optional anonymous telemetry (`image/local_telemetry.py`
  documents exactly what is sent and how to turn it off).
- `worker/extract_wind.py` — the wind-product extraction (10 m / 2 m wind,
  gusts, per-rect JSON grids).
- `worker/crash_classify.py`, `worker/hrrr_fetch.py` — failure hints and
  HRRR index-subset downloads.
- `examples/bundle/` — a small sample bundle to run the image with.

## Running it

See [the local-runs guide](https://forge.frothy.surf/run) for the full
walkthrough. In short:

```
mkdir -p out
docker run --rm -p 8760:8760 --user "$(id -u):$(id -g)" \
  -v $(pwd)/examples/bundle:/bundle:ro \
  -v /path/to/WPS_GEOG:/geog:ro \
  -v $(pwd)/out:/out \
  registry.frothy.surf/frothy-forge:latest --hours 24
```

## Verifying what you pulled

Every published image is signed keylessly with
[Sigstore cosign](https://docs.sigstore.dev/) by this repository's workflow,
and carries an SPDX SBOM and a SLSA provenance attestation that embeds the
Dockerfile it was built from:

```
cosign verify registry.frothy.surf/frothy-forge:latest \
  --certificate-identity https://github.com/frothy-surf/forge/.github/workflows/ci.yml@refs/heads/main \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com

docker buildx imagetools inspect registry.frothy.surf/frothy-forge:latest --format '{{ json .SBOM }}'
docker buildx imagetools inspect registry.frothy.surf/frothy-forge:latest --format '{{ json .Provenance }}'
```

`scripts/verify-forge-image.sh` is what CI runs against each built image
before publishing: it checks the image contains exactly these files, runs
as an unprivileged user, and stages the sample bundle as an arbitrary uid.

## Building it yourself

```
docker build -f image/Dockerfile --target forge -t forge:local .
```

Compiling WRF takes about an hour. On arm64 pass
`--build-arg WRF_MARCH=armv8.2-a` (or `native` for a machine building an
image for itself).

## How this repository is maintained

The files here are mirrored from the frothy monorepo by a script; changes
land there first and arrive here as a sync commit, so pull requests against
this repository are not merged directly. Issues are welcome. The mirrored
files carry technical rationale only, and `scripts/check-shipped-hygiene.sh`
runs in CI to keep it that way.

## Third-party components

WRF and WPS are public-domain software from UCAR/NCAR; "WRF" is a
registered trademark of UCAR. The image also contains Fedora packages,
listed in the SBOM. Forcing data come from NOAA (GFS, HRRR, RRFS) and, for
ERA5, the Copernicus Climate Data Store under its own licence.

## License

The frothy code here is under the [frothy Run-Only License 1.0](LICENSE):
you may copy, build and run it, including commercially, and everything it
produces is yours to use however you like. You may not redistribute it,
modify it or build on it, use its code elsewhere, or run it for others as
a service. WRF and WPS remain public domain under their own terms, and the
image's Fedora packages are listed in its SBOM under theirs.
