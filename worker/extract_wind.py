#!/usr/bin/env python3
"""Extract 10 m wind fields from WRF output onto regular lat/lon grids.

Reads wrfout/wrfwind NetCDF files, rotates the grid-relative U10/V10 to
earth-relative components, and resamples each configured rectangle to a
regular lat/lon grid. Emits per-frame JSON (grid of u/v/speed/direction),
and optionally a rendered PNG and a GeoTIFF, plus a manifest.json.

The WRF grid is regular in Lambert-conformal x/y, so resampling is an exact
projection transform followed by bilinear interpolation — no scattered-data
fitting involved.

Usage:
  extract_wind.py --config regions/PNW.json --out OUT/wind wrfout_d02_* [--static geo_em.d02.nc]

Static fields (XLAT/XLONG/SINALPHA/COSALPHA/LANDMASK) are taken from the
input file when present, else from --static (a wrfout or geo_em file).
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from netCDF4 import Dataset
from pyproj import CRS, Transformer

EARTH_RADIUS_M = 6370000.0  # WRF sphere
KARMAN, GRAV, CP, RD = 0.4, 9.81, 1004.5, 287.04

# Gust over steady wind, ECMWF-style: gust = U10 + GUST_ALPHA * u*. The
# coefficient is ECMWF's 7.71 (their HTESSEL gust diagnostic), which folds the
# near-surface turbulence intensity into the friction velocity. Where the run
# also carries WSPD10MAX (nwp_diagnostics=1: max resolved 10 m wind since the
# last history write, hourly in the PNW namelists), the published gust is the
# elementwise max of the two — the similarity term supplies sub-grid
# turbulent gustiness, the max term resolved squalls the grid itself moved
# through inside the hour. Both are >= the steady speed by construction.
GUST_ALPHA = 7.71


def psi_m(zeta):
    """Businger-Dyer stability function for momentum."""
    zeta = np.clip(zeta, -10.0, 2.0)
    psi = np.where(zeta >= 0, -5.0 * zeta, 0.0)  # stable branch
    unstable = zeta < 0
    if np.any(unstable):
        x = (1.0 - 16.0 * np.where(unstable, zeta, 0.0)) ** 0.25
        pu = (2.0 * np.log((1.0 + x) / 2.0) + np.log((1.0 + x * x) / 2.0)
              - 2.0 * np.arctan(x) + np.pi / 2.0)
        psi = np.where(unstable, pu, psi)
    return psi


def wind_reduction(z0, rmol, z_from=10.0, z_to=2.0):
    """u(z_to)/u(z_from) from Monin-Obukhov similarity.

    rmol is 1/L (positive stable, negative unstable, 0 neutral).
    """
    z0 = np.clip(z0, 1e-5, 1.5)
    num = np.log(z_to / z0) - psi_m(z_to * rmol) + psi_m(z0 * rmol)
    den = np.log(z_from / z0) - psi_m(z_from * rmol) + psi_m(z0 * rmol)
    return np.clip(num / np.maximum(den, 0.1), 0.3, 1.05)


def inverse_obukhov(nc, it):
    """1/L: straight from RMOL when output, else from surface fluxes."""
    if "RMOL" in nc.variables:
        return np.asarray(nc.variables["RMOL"][it])
    required = ("UST", "HFX", "T2", "PSFC")
    if not all(v in nc.variables for v in required):
        return None
    ust = np.maximum(np.asarray(nc.variables["UST"][it]), 0.05)
    hfx = np.asarray(nc.variables["HFX"][it])
    t2 = np.asarray(nc.variables["T2"][it])
    psfc = np.asarray(nc.variables["PSFC"][it])
    rho = psfc / (RD * t2)
    theta = t2 * (100000.0 / psfc) ** 0.2857
    wt = hfx / (rho * CP)  # kinematic sensible heat flux
    if "QFX" in nc.variables:  # buoyancy contribution of moisture flux
        wt = wt + 0.61 * theta * np.asarray(nc.variables["QFX"][it]) / rho
    return -KARMAN * GRAV * wt / (theta * ust ** 3)


def wrf_crs(nc):
    """Build the model CRS from WRF global attributes, dispatching on
    MAP_PROJ (1 = Lambert, 2 = polar stereographic, 3 = Mercator — the
    projections forge can build; WRF computes on the R = 6370 km sphere in
    all of them). A Mercator or polar run pushed through a Lambert-only
    path would place every cell wrong, silently — unknown values are a
    hard error, never a fallback.

    Only the axis directions and scale matter here: Grid anchors x0/y0 at
    the domain's own corner, so each projection's origin/anchor longitude
    only needs to avoid a wraparound inside the domain (Mercator uses
    CEN_LON for exactly that reason)."""
    proj = int(getattr(nc, "MAP_PROJ", 1))
    if proj == 1:
        return CRS.from_proj4(
            f"+proj=lcc +lat_1={nc.TRUELAT1} +lat_2={nc.TRUELAT2} "
            f"+lat_0={nc.MOAD_CEN_LAT} +lon_0={nc.STAND_LON} "
            f"+R={EARTH_RADIUS_M} +units=m +no_defs"
        )
    if proj == 2:
        # hemisphere from truelat1's sign; stand_lon toward -y, like WPS
        pole = 90.0 if float(nc.TRUELAT1) >= 0 else -90.0
        return CRS.from_proj4(
            f"+proj=stere +lat_0={pole} +lat_ts={nc.TRUELAT1} "
            f"+lon_0={nc.STAND_LON} +R={EARTH_RADIUS_M} +units=m +no_defs"
        )
    if proj == 3:
        return CRS.from_proj4(
            f"+proj=merc +lat_ts={nc.TRUELAT1} +lon_0={nc.CEN_LON} "
            f"+R={EARTH_RADIUS_M} +units=m +no_defs"
        )
    raise RuntimeError(
        f"MAP_PROJ {proj} is not supported by the product extractor "
        "(1 lambert | 2 polar | 3 mercator)")


class Grid:
    """Maps lat/lon to fractional indices of the WRF mass grid."""

    def __init__(self, nc, static_nc):
        # Search the data file first, then the static file: aux history
        # streams carry coordinates but not SINALPHA/COSALPHA/LANDMASK.
        sources = [s for s in (nc, static_nc) if s is not None]

        def static(*candidates):
            for src in sources:
                for name in candidates:
                    if name in src.variables:
                        v = src.variables[name]
                        return v[0] if v.ndim == 3 else v[:]
            raise KeyError(f"none of {candidates} in any input/static file")

        self.lat = np.asarray(static("XLAT", "XLAT_M"))
        self.lon = np.asarray(static("XLONG", "XLONG_M"))
        self.sinalpha = np.asarray(static("SINALPHA"))
        self.cosalpha = np.asarray(static("COSALPHA"))
        self.landmask = np.asarray(static("LANDMASK"))

        self.crs = wrf_crs(nc if "DX" in nc.ncattrs() else src)
        self.dx = float(nc.DX if "DX" in nc.ncattrs() else src.DX)
        self.dy = float(nc.DY if "DY" in nc.ncattrs() else src.DY)
        self.to_xy = Transformer.from_crs(CRS.from_epsg(4326), self.crs, always_xy=True)
        x00, y00 = self.to_xy.transform(self.lon[0, 0], self.lat[0, 0])
        self.x0, self.y0 = float(x00), float(y00)
        self.ny, self.nx = self.lat.shape

    def frac_index(self, lons, lats):
        x, y = self.to_xy.transform(lons, lats)
        return (np.asarray(x) - self.x0) / self.dx, (np.asarray(y) - self.y0) / self.dy

    def bilinear(self, field, fi, fj):
        """Sample field at fractional indices (fi = x/west_east, fj = y/south_north)."""
        i0 = np.clip(np.floor(fi).astype(int), 0, self.nx - 2)
        j0 = np.clip(np.floor(fj).astype(int), 0, self.ny - 2)
        ti = np.clip(fi - i0, 0.0, 1.0)
        tj = np.clip(fj - j0, 0.0, 1.0)
        f00 = field[j0, i0]
        f01 = field[j0, i0 + 1]
        f10 = field[j0 + 1, i0]
        f11 = field[j0 + 1, i0 + 1]
        return (f00 * (1 - ti) * (1 - tj) + f01 * ti * (1 - tj)
                + f10 * (1 - ti) * tj + f11 * ti * tj)


# A stamp more than this far past the minute is not an adaptive-step overshoot
# (see canonical_stamp) and is left exactly as WRF wrote it.
DRIFT_GUARD_S = 30


def canonical_stamp(stamp):
    """WRF's output time, with the adaptive time step's overshoot removed.

    With `use_adaptive_time_step`, WRF writes each output at the first step at
    or AFTER the target time, so a stamp is its target plus a non-negative
    overshoot smaller than that domain's `max_time_step` (75/15/5 s for
    d01/d02/d03 in the PNW namelist). Output targets are whole minutes
    (`auxhist23_interval` is in minutes), so dropping the seconds recovers the
    target exactly. `step_to_output_time` already does this for the history
    stream — which is why stamps land clean every `history_interval` — but it
    does not target auxhist23, where the wind product comes from.

    Why it is worth removing: the overshoot differs from run to run, so the
    same valid hour reaches the viewer as 08:00:00 from one generation and
    08:00:03 from the next. The live map serves each valid time from the newest
    generation that has it, and two spellings of one time defeat that — the
    hour appears twice on the slider, three seconds apart, with the whole field
    swapping run between them, and a sensor trend alternates between two runs
    hour by hour.

    A larger offset would mean the premise no longer holds (a domain whose
    max_time_step exceeds a minute, say). Snapping then would invent a time a
    minute off the real one, so such a stamp is passed through and reported.
    """
    if len(stamp) != 19 or stamp[16] != ":":
        return stamp
    sec = stamp[17:19]
    if sec == "00" or not sec.isdigit():
        return stamp
    if int(sec) > DRIFT_GUARD_S:
        print(f"WARNING: {stamp} sits {sec}s past the minute, too far to be an "
              f"adaptive-step overshoot; leaving it as written", flush=True)
        return stamp
    return stamp[:17] + "00"


def frame_times(nc):
    raw = nc.variables["Times"][:]
    return [canonical_stamp("".join(t.astype(str)).replace("_", "T")) for t in raw]


def rect_grid(rect, step):
    lats = np.arange(rect["south"], rect["north"] + step / 2, step)
    lons = np.arange(rect["west"], rect["east"] + step / 2, step)
    lon2d, lat2d = np.meshgrid(lons, lats)
    return lats, lons, lon2d, lat2d


def render_png(path, lats, lons, speed_kts, u, v, land, label="10 m wind (kts)"):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 8 * (lats[-1] - lats[0]) / (lons[-1] - lons[0]) * 1.5))
    spd = np.ma.masked_where(land > 0.5, speed_kts)
    mesh = ax.pcolormesh(lons, lats, spd, cmap="viridis", vmin=0, vmax=30, shading="auto")
    ax.pcolormesh(lons, lats, np.ma.masked_where(land <= 0.5, land),
                  cmap="Greys", vmin=0, vmax=3, shading="auto")
    stride = max(1, len(lats) // 20)
    ax.quiver(lons[::stride], lats[::stride], u[::stride, ::stride], v[::stride, ::stride],
              color="white", scale=400, width=0.0025)
    fig.colorbar(mesh, ax=ax, label=label, shrink=0.8)
    ax.set_aspect(1.0 / np.cos(np.radians(np.mean(lats))))
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def write_geotiff(path, lats, lons, u, v, speed_kts):
    try:
        import rasterio
        from rasterio.transform import from_origin
    except ImportError:
        print("rasterio unavailable; skipping GeoTIFF", file=sys.stderr)
        return False
    step_lon = lons[1] - lons[0]
    step_lat = lats[1] - lats[0]
    transform = from_origin(lons[0] - step_lon / 2, lats[-1] + step_lat / 2, step_lon, step_lat)
    with rasterio.open(
        path, "w", driver="GTiff", height=len(lats), width=len(lons),
        count=3, dtype="float32", crs="EPSG:4326", transform=transform,
        compress="deflate",
    ) as dst:
        # north-up rasters: flip the south-to-north arrays
        dst.write(np.flipud(u).astype("float32"), 1)
        dst.write(np.flipud(v).astype("float32"), 2)
        dst.write(np.flipud(speed_kts).astype("float32"), 3)
        dst.update_tags(1, name="u10_ms"), dst.update_tags(2, name="v10_ms")
        dst.update_tags(3, name="speed_kts")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+", help="wrfout/wrfwind NetCDF files")
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--static", help="file providing XLAT/SINALPHA/... if inputs lack them")
    ap.add_argument("--append", action="store_true",
                    help="merge frames into an existing manifest (progressive publishing)")
    ap.add_argument("--stations",
                    help="JSON list of {id, lat, lon}: sample the model at these points "
                         "each frame and accumulate <out>/stations.json (point timeseries "
                         "for the frontend's sensor cards)")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text())
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    # one delivery timestamp per invocation: everything in this pass uploads together
    extracted_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "")
    opts = cfg.get("output", {})
    step = float(opts.get("grid_deg", 0.01))

    static_nc = Dataset(args.static) if args.static else None
    grid = None
    # schema: the published data contract version.
    # Bump ONLY on breaking shape changes; additive optional keys don't.
    manifest = {"schema": 1, "region": cfg["region"], "grid_deg": step, "frames": []}
    mpath = out_root / "manifest.json"
    if args.append and mpath.exists():
        manifest = json.loads(mpath.read_text())
        manifest.setdefault("schema", 1)

    # Point timeseries: stations sampled straight off the model grid (no
    # double interpolation through the display rects). Nested domains both
    # sample a station they cover; the finer sample (smaller dx) wins.
    stations = json.loads(Path(args.stations).read_text()) if args.stations else []
    spath = out_root / "stations.json"
    stations_out = {}
    if stations and args.append and spath.exists():
        stations_out = json.loads(spath.read_text()).get("stations", {})
    st_ids, st_fi, st_fj = [], None, None

    for input_path in sorted(args.inputs):
        nc = Dataset(input_path)
        if grid is None:
            grid = Grid(nc, static_nc)
            if stations:
                fi, fj = grid.frac_index([s["lon"] for s in stations],
                                         [s["lat"] for s in stations])
                inside = [k for k in range(len(stations))
                          if 0 <= fi[k] <= grid.nx - 1 and 0 <= fj[k] <= grid.ny - 1]
                st_ids = [stations[k]["id"] for k in inside]
                st_fi = np.asarray([fi[k] for k in inside])
                st_fj = np.asarray([fj[k] for k in inside])
                for k in inside:
                    s = stations[k]
                    stations_out.setdefault(
                        s["id"], {"lat": s["lat"], "lon": s["lon"], "samples": {}})
        times = frame_times(nc)
        for it, stamp in enumerate(times):
            u10 = np.asarray(nc.variables["U10"][it])
            v10 = np.asarray(nc.variables["V10"][it])
            # rotate grid-relative to earth-relative
            ue = u10 * grid.cosalpha - v10 * grid.sinalpha
            ve = v10 * grid.cosalpha + u10 * grid.sinalpha

            # 2 m wind via Monin-Obukhov similarity from the 10 m fields.
            # Roughness from the model (ZNT) when present, else by landmask;
            # stability from RMOL or derived from surface fluxes; neutral
            # (rmol=0) as the last resort.
            if "ZNT" in nc.variables:
                z0 = np.asarray(nc.variables["ZNT"][it])
            else:
                z0 = np.where(grid.landmask > 0.5, 0.1, 2e-4)
            rmol = inverse_obukhov(nc, it)
            if rmol is None:
                rmol = np.zeros_like(u10)
            reduction = wind_reduction(z0, rmol)
            # Downwelling shortwave, for stratus burn-off evaluation; present
            # once SWDOWN is in the
            # wind_io stream, absent from older runs — never required.
            swdown = (np.asarray(nc.variables["SWDOWN"][it])
                      if "SWDOWN" in nc.variables else None)

            # Gust on the model grid (m/s), from UST once it is in the wind_io
            # stream; absent from older runs — never required. Interpolating
            # the combined field (rather than recombining after interpolation)
            # keeps gust >= the published steady speed everywhere: the linear
            # sample of speed+alpha*ust dominates |(interp u, interp v)|.
            gust = None
            mixed = None
            if "UST" in nc.variables:
                ust = np.asarray(nc.variables["UST"][it])
                spd10 = np.hypot(u10, v10)
                gust = spd10 + GUST_ALPHA * ust
                if "WSPD10MAX" in nc.variables:
                    gust = np.maximum(gust, np.asarray(nc.variables["WSPD10MAX"][it]))
                # "Mixed" wind: mean + w * (gust - mean), where w is the
                # fraction of gust momentum that plausibly reaches a rider.
                # Two mixing engines: convection
                # (stability, via zeta = z/L at 10 m — unstable/neutral air
                # lets gusts through, a stable marine layer blocks them) and
                # mechanical shear — but the shear override applies over
                # WATER ONLY: u* is large over rough land because the land
                # is rough, not because gusts reach the surface (a canopy
                # absorbs them; forested slopes at a 10 kt mean saturated
                # u*/0.6 and painted near-full gusts). Over water, large u*
                # really is mechanically mixed gap flow. Coefficients remain a
                # documented first
                # guess, to be calibrated against sensor gust factors.
                zeta10 = np.clip(10.0 * rmol, -5.0, 5.0)
                w_stab = 0.85 / (1.0 + 4.0 * np.maximum(zeta10, 0.0))
                w_mech = np.where(grid.landmask < 0.5,
                                  0.9 * np.clip(ust / 0.6, 0.0, 1.0), 0.0)
                w = np.clip(np.maximum(w_stab, w_mech), 0.15, 0.9)
                mixed = spd10 + w * (gust - spd10)

            if st_ids:
                su = grid.bilinear(ue, st_fi, st_fj)
                sv = grid.bilinear(ve, st_fi, st_fj)
                sred = grid.bilinear(reduction, st_fi, st_fj)
                sgust = grid.bilinear(gust, st_fi, st_fj) if gust is not None else None
                sspd = np.hypot(su, sv) * 1.9438445
                sdir = (np.degrees(np.arctan2(-su, -sv)) + 360.0) % 360.0
                dx = int(round(grid.dx))
                for k, sid in enumerate(st_ids):
                    prev = stations_out[sid]["samples"].get(stamp)
                    if prev is None or prev[3] >= dx:  # finer domain wins
                        stations_out[sid]["samples"][stamp] = [
                            round(float(sspd[k]), 1),
                            round(float(sspd[k] * sred[k]), 1),
                            int(round(float(sdir[k]))) % 360,
                            dx,
                            (round(float(sgust[k]) * 1.9438445, 1)
                             if sgust is not None else None),
                        ]

            for rect in cfg["rects"]:
                # rect-level grid: a 2 km-domain rect published at the global
                # 0.01 deg would just oversample; let it declare its own step
                step_r = float(rect.get("grid_deg", step))
                lats, lons, lon2d, lat2d = rect_grid(rect, step_r)
                fi, fj = grid.frac_index(lon2d, lat2d)
                u = grid.bilinear(ue, fi, fj)
                v = grid.bilinear(ve, fi, fj)
                red = grid.bilinear(reduction, fi, fj)
                land = grid.bilinear(grid.landmask, fi, fj)
                speed_ms = np.hypot(u, v)
                speed_kts = speed_ms * 1.9438445
                speed2_kts = speed_kts * red
                dir_from = (np.degrees(np.arctan2(-u, -v)) + 360.0) % 360.0

                tdir = out_root / rect["id"]
                tdir.mkdir(parents=True, exist_ok=True)
                base = stamp.replace(":", "")
                files = {}

                payload = {
                    "time_utc": stamp,
                    "rect": rect,
                    "grid_deg": step_r,
                    "lats": [round(float(x), 4) for x in lats],
                    "lons": [round(float(x), 4) for x in lons],
                    "u10_ms": np.round(u, 2).tolist(),
                    "v10_ms": np.round(v, 2).tolist(),
                    "speed_kts": np.round(speed_kts, 1).tolist(),
                    "speed2m_kts": np.round(speed2_kts, 1).tolist(),
                    "dir_from_deg": np.round(dir_from, 0).tolist(),
                    "landmask": np.round(land, 0).astype(int).tolist(),
                }
                if swdown is not None:
                    sw = grid.bilinear(swdown, fi, fj)
                    payload["swdown_wm2"] = np.round(sw, 0).astype(int).tolist()
                if gust is not None:
                    gs = grid.bilinear(gust, fi, fj)
                    payload["gust_kts"] = np.round(gs * 1.9438445, 1).tolist()
                if mixed is not None:
                    mx = grid.bilinear(mixed, fi, fj)
                    payload["mixed_kts"] = np.round(mx * 1.9438445, 1).tolist()
                jpath = tdir / f"wind_{base}.json"
                jpath.write_text(json.dumps(payload, separators=(",", ":")))
                files["json"] = str(jpath.relative_to(out_root))

                if opts.get("png", True):
                    ppath = tdir / f"wind_{base}.png"
                    render_png(ppath, lats, lons, speed_kts, u, v, land)
                    files["png"] = str(ppath.relative_to(out_root))
                    p2path = tdir / f"wind2m_{base}.png"
                    render_png(p2path, lats, lons, speed2_kts, u * red, v * red, land,
                               label="2 m wind (kts)")
                    files["png_2m"] = str(p2path.relative_to(out_root))

                if opts.get("geotiff", False):
                    gpath = tdir / f"wind_{base}.tif"
                    if write_geotiff(gpath, lats, lons, u, v, speed_kts):
                        files["geotiff"] = str(gpath.relative_to(out_root))

                # Frame provenance: when this frame was extracted (progressive
                # publishing delivers a run over hours, and re-claims refresh
                # single chunks — one run-level timestamp can't tell newer
                # frames from stale ones) and which domain it was sampled from
                # (time-windowed nests mean composition varies per frame).
                manifest["frames"].append({
                    "time_utc": stamp,
                    "rect_id": rect["id"],
                    "domain": rect.get("domain") or cfg.get("domain"),
                    "generated": extracted_at,
                    "files": files,
                })
        nc.close()

    # dedupe on (rect, time): re-extraction of a chunk wins over older entries
    seen = {}
    for f in manifest["frames"]:
        seen[(f["rect_id"], f["time_utc"])] = f
    manifest["frames"] = sorted(seen.values(), key=lambda f: (f["rect_id"], f["time_utc"]))
    manifest["model"] = cfg.get("model")
    if cfg.get("attribution"):
        # data-licence credit (ERA5-forced runs: Copernicus CC-BY-4.0);
        # readers that show the run's provenance show this line too
        manifest["attribution"] = cfg["attribution"]
    if cfg.get("nest_windows") is not None:
        # run-level nest schedule (job config, threaded through the product
        # slice by the hosted worker) — lets readers tell a windowed nest from a
        # rect that simply hasn't published yet
        manifest["nests"] = cfg["nest_windows"]
    manifest["generated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    mpath.write_text(json.dumps(manifest, indent=1))
    if stations:
        for rec in stations_out.values():
            rec["samples"] = dict(sorted(rec["samples"].items()))
        spath.write_text(json.dumps(
            {"stations": stations_out,
             "fields": ["speed_kts", "speed2m_kts", "dir_from_deg", "dx_m", "gust_kts"],
             "generated": manifest["generated"]},
            separators=(",", ":")))
    print(f"wrote {len(manifest['frames'])} frames to {out_root}")


if __name__ == "__main__":
    main()
