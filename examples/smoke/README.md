# Smoke inputs

Forge bundles sized to integrate in seconds. CI runs each one through every
freshly built image — a complete forecast, static data to wind products —
before the image is published:

    scripts/smoke-forge-image.sh registry.frothy.surf/frothy-forge:latest examples/smoke/tiny-lcc

The script fetches UCAR's low-resolution WPS_GEOG (~150 MB) on first use and
runs with `--geog-res lowres`, so it needs no static-data download of your
own. It is also the quickest way to check that the image works on your
machine.

| input | what it covers |
|---|---|
| `tiny-lcc` | one 31×31 Lambert domain at 30 km, stock CONUS physics, GFS forcing, 3 h |

A new input is a directory here with the four bundle files, plus its name in
the smoke job's `input` matrix.
