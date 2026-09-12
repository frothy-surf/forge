"""Shared HRRR GRIB2 access: idx-ranged subsetting from the public archive.

Used for surface-field frames (wrfsfcf) and by image/runner.py
(pressure-level fields -> WRF boundary/initial conditions).
The .idx sidecars let us fetch only the messages we need — a wrfprsf file is
~370 MB, the boundary-condition subset ~40%; the wrfsfcf subset is ~3 MB.

Messages that are adjacent in the file are fetched in one ranged request over
a kept-alive connection: per-request TLS setup, not bandwidth, is what limits
this workload (a wrfnatf hour is 700 messages), and both together take a
forecast hour from ~10 min to ~50 s.

The S3 bucket doubles as the archive (2014->present), so hindcast runs fetch
through the same path as realtime.
"""

import http.client
import time
import urllib.error
import urllib.parse
import urllib.request

S3_ORIGINS = [
    "https://noaa-hrrr-bdp-pds.s3.amazonaws.com",
    "https://noaahrrr.blob.core.windows.net/hrrr",  # Azure mirror
]

# Ceiling on a merged byte range. Contiguous selected messages are fetched in
# one request (see _merge_runs); the cap keeps a mid-transfer reset from
# costing more than a few seconds of re-download. Well above the ~4 MB an
# average wrfnatf run spans, so it rarely binds.
MAX_RANGE_BYTES = 32_000_000


class _Session:
    """Keep-alive HTTPS fetcher, one connection per origin.

    urllib opens a fresh TLS connection per request, which dominates the cost
    of range-fetching: measured against the HRRR bucket, a 0.5 MB message
    takes 677 ms through urlopen where the same link moves 8 MB/s on a single
    connection — ~90% handshake. A wrfnatf hour selects 700 messages, so that
    is the difference between ~10 min and ~2 min per forecast hour.
    """

    def __init__(self, timeout=180):
        self.timeout = timeout
        self._conns = {}

    def _drop(self, host):
        conn = self._conns.pop(host, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def get(self, url, headers=None, attempts=4, log=print):
        parts = urllib.parse.urlsplit(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        # identity: a range served gzipped would not slice on idx offsets
        hdrs = {"User-Agent": "frothy/1.0", "Accept-Encoding": "identity",
                **(headers or {})}
        err = None
        for i in range(attempts):
            try:
                conn = self._conns.get(parts.netloc)
                if conn is None:
                    conn = self._conns[parts.netloc] = http.client.HTTPSConnection(
                        parts.netloc, timeout=self.timeout)
                conn.request("GET", path, headers=hdrs)
                resp = conn.getresponse()
                body = resp.read()  # always drain: a half-read connection is unusable
                if resp.status == 404:
                    raise urllib.error.HTTPError(url, 404, resp.reason, resp.headers, None)
                if resp.status not in (200, 206):
                    raise IOError(f"HTTP {resp.status} {resp.reason}")
                if "Range" in hdrs and resp.status != 206:
                    # a 200 here means the whole object, not the slice asked for
                    raise IOError(f"range request answered with {resp.status}")
                return body
            except urllib.error.HTTPError as e:
                self._drop(parts.netloc)
                if e.code == 404:
                    raise
                err = e
            except Exception as e:  # noqa: BLE001
                self._drop(parts.netloc)
                err = e
            log(f"GET {url} attempt {i + 1} failed: {err}")
            time.sleep(2 * (i + 1))
        raise RuntimeError(f"GET {url} failed after {attempts} attempts: {err}")


# Process-wide, so connections are reused across forecast hours too.
_SESSION = _Session()


def http_get(url, headers=None, attempts=4, log=print):
    return _SESSION.get(url, headers=headers, attempts=attempts, log=log)


def grib_url(date, cycle, fh, product, origin):
    """date 'YYYY-MM-DD', product 'wrfsfcf' | 'wrfprsf' | 'wrfnatf'."""
    return (f"{origin}/hrrr.{date.replace('-', '')}/conus/"
            f"hrrr.t{cycle:02d}z.{product}{fh:02d}.grib2")


def parse_idx(text):
    """idx line: 'n:offset:d=YYYYMMDDHH:VAR:LEVEL:time desc:' ->
    [{start, var, level, timedesc}] with computed end offsets."""
    rows = []
    for line in text.splitlines():
        p = line.split(":")
        if len(p) >= 6:
            rows.append({"start": int(p[1]), "var": p[3], "level": p[4], "timedesc": p[5]})
    for i, row in enumerate(rows):
        row["end"] = rows[i + 1]["start"] - 1 if i + 1 < len(rows) else None
    return rows


def _merge_runs(rows, max_bytes=MAX_RANGE_BYTES):
    """Selected rows -> lists of rows that are adjacent in the file.

    Offsets come from the unfiltered idx, so a skipped message breaks the run
    and no unwanted bytes are ever fetched. wrfnatf selects 700 of 1133
    messages, which collapses to ~100 runs — 7x fewer requests for the same
    bytes."""
    runs = []
    for row in rows:
        cur = runs[-1] if runs else None
        if cur is not None:
            last = cur[-1]
            span = (row["end"] if row["end"] is not None else row["start"]) - cur[0]["start"]
            if last["end"] is not None and row["start"] == last["end"] + 1 and span <= max_bytes:
                cur.append(row)
                continue
        runs.append([row])
    return runs


def _slice_run(run, blob):
    """Split one merged range's bytes back into per-message bodies."""
    base = run[0]["start"]
    end = run[-1]["end"]
    if end is not None and len(blob) != end - base + 1:
        raise IOError(f"range {base}-{end} returned {len(blob)} bytes, "
                      f"expected {end - base + 1}")
    return [(row["var"], row["level"],
             blob[row["start"] - base:
                  None if row["end"] is None else row["end"] - base + 1])
            for row in run]


def fetch_messages_from(urls, selector, log=print, what="grib"):
    """Fetch the GRIB messages selector(var, level, timedesc) approves from
    the first of `urls` (mirror candidates for one file) that serves an .idx.
    Returns [(var, level, bytes)] in file order; raises
    urllib.error.HTTPError(404) when no candidate has the file yet."""
    last_err = None
    for url in urls:
        try:
            idx = http_get(url + ".idx", log=log).decode()
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
        want = [row for row in parse_idx(idx)
                if selector(row["var"], row["level"], row["timedesc"])]
        out = []
        for run in _merge_runs(want):
            end = run[-1]["end"] if run[-1]["end"] is not None else ""
            blob = http_get(url, headers={"Range": f"bytes={run[0]['start']}-{end}"}, log=log)
            out.extend(_slice_run(run, blob))
        return out
    if isinstance(last_err, urllib.error.HTTPError) and last_err.code == 404:
        raise last_err
    raise RuntimeError(f"no origin served {what}: {last_err}")


def fetch_messages(date, cycle, fh, product, selector, log=print):
    """Fetch the GRIB messages selector(var, level, timedesc) approves.
    Returns [(var, level, bytes)] in file order. Tries each origin in turn;
    raises urllib.error.HTTPError(404) when no origin has the file yet."""
    return fetch_messages_from(
        [grib_url(date, cycle, fh, product, origin) for origin in S3_ORIGINS],
        selector, log=log, what=f"{product}{fh:02d} for {date} t{cycle:02d}z")
