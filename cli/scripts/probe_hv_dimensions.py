"""Probe Earth Engine High Volume API for response-shape behavior across
tile sizes. Reports whether the response payload matches the dimensions
declared in the TIFF IFD.

Run from the cli/ directory:
    uv run python scripts/probe_hv_dimensions.py
"""

from __future__ import annotations

import json
import struct
import sys
import time
from pathlib import Path
from typing import Any

import google.auth
import httpx
from google.auth.transport.requests import Request

EE_HV_URL = (
    "https://earthengine-highvolume.googleapis.com/v1/projects/{project}/image:computePixels"
)
PROJECT = "datensee-testing"

# Same expression we ship in the bundled demo: Landsat 9 surface-reflectance
# median composite, NDVI = (SR_B5 − SR_B4) / (SR_B5 + SR_B4). Chosen because
# (a) Landsat 9 is widely used so this is a realistic workload, and (b) the
# median-composite reduction is what we suspect pushes EE into a regime
# where the response gets truncated.
_DEMO_EXPRESSION_PATH = Path(__file__).parent.parent / "src" / "datensee" / "data" / "demo_expression.json"

# A point in the Central Valley with full Landsat 9 coverage. All requests
# are centered here; we vary tile_size while holding pixel scale (30 m/px)
# and CRS (EPSG:4326) constant, so each request asks EE for the same
# imagery at the same resolution, only the bbox extent grows.
CENTER_LON = -120.6
CENTER_LAT = 38.0
SCALE_M_PER_PX = 30.0
SCALE_DEG_PER_PX = SCALE_M_PER_PX / 111_320.0

TILE_SIZES: list[int] = [64, 128, 192, 256, 320, 384, 448, 512, 768, 1024]


# TIFF type sizes in bytes (TIFF spec table 1)
_TIFF_TYPE_SIZES: dict[int, int] = {
    1: 1, 2: 1, 3: 2, 4: 4, 5: 8,
    6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8,
}


def parse_tiff_ifd(data: bytes) -> dict[str, Any]:
    """Parse just enough TIFF to capture dimensions, sample structure,
    layout, and the sum of strip/tile byte counts."""
    if len(data) < 8:
        return {"error": f"truncated ({len(data)} bytes total)"}
    bom = data[:2]
    if bom == b"II":
        e = "<"
    elif bom == b"MM":
        e = ">"
    else:
        return {"error": f"bad byte-order mark: {bom!r}"}

    magic = struct.unpack(f"{e}H", data[2:4])[0]
    if magic != 42:
        return {"error": f"bad TIFF magic: {magic}"}

    ifd_offset = struct.unpack(f"{e}I", data[4:8])[0]
    n_entries = struct.unpack(f"{e}H", data[ifd_offset : ifd_offset + 2])[0]

    entries: dict[int, tuple[int, int, int]] = {}
    for i in range(n_entries):
        off = ifd_offset + 2 + i * 12
        tag, type_, count = struct.unpack(f"{e}HHI", data[off : off + 8])
        size = _TIFF_TYPE_SIZES.get(type_, 1)
        total = size * count
        if total <= 4:
            value_pos = off + 8
        else:
            value_pos = struct.unpack(f"{e}I", data[off + 8 : off + 12])[0]
        entries[tag] = (type_, count, value_pos)

    def read_array(tag: int) -> list[int] | None:
        if tag not in entries:
            return None
        type_, count, pos = entries[tag]
        fmt = {1: "B", 3: "H", 4: "I"}.get(type_)
        if fmt is None:
            return None
        size = _TIFF_TYPE_SIZES[type_]
        return list(struct.unpack(f"{e}{count}{fmt}", data[pos : pos + size * count]))

    width = (read_array(256) or [0])[0]
    height = (read_array(257) or [0])[0]
    bps = (read_array(258) or [1])[0]
    compression = (read_array(259) or [1])[0]
    samples = (read_array(277) or [1])[0]
    sample_format = (read_array(339) or [1])[0]

    if 273 in entries:
        layout = "strip"
        counts = read_array(279) or []
    elif 324 in entries:
        layout = "tile"
        counts = read_array(325) or []
    else:
        return {"error": "no StripOffsets nor TileOffsets in IFD"}

    bytes_per_sample = max(bps // 8, 1)
    expected = width * height * samples * bytes_per_sample
    actual = sum(counts)

    return {
        "width": width,
        "height": height,
        "bps": bps,
        "samples": samples,
        "sample_format": sample_format,  # 1=uint, 2=int, 3=float
        "compression": compression,
        "layout": layout,
        "n_chunks": len(counts),
        "actual_bytes": actual,
        "expected_bytes": expected,
        "match": actual == expected,
        "ratio_actual_to_expected": (actual / expected) if expected else float("nan"),
    }


def get_token() -> str:
    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(Request())
    return creds.token


def probe(client: httpx.Client, token: str, tile_size: int) -> dict[str, Any]:
    """Issue one computePixels request at the given tile_size and return
    a structured result."""
    half = tile_size * SCALE_DEG_PER_PX / 2
    x_min = CENTER_LON - half
    x_max = CENTER_LON + half
    y_min = CENTER_LAT - half
    y_max = CENTER_LAT + half
    pixel_w = (x_max - x_min) / tile_size
    pixel_h = (y_max - y_min) / tile_size

    request_body = {
        "expression": json.loads(_DEMO_EXPRESSION_PATH.read_text()),
        "fileFormat": "GEO_TIFF",
        "grid": {
            "dimensions": {"width": tile_size, "height": tile_size},
            "affineTransform": {
                "scaleX": pixel_w,
                "shearX": 0,
                "translateX": x_min,
                "shearY": 0,
                "scaleY": -pixel_h,
                "translateY": y_max,
            },
            "crsCode": "EPSG:4326",
        },
    }

    t0 = time.monotonic()
    response = client.post(
        EE_HV_URL.format(project=PROJECT),
        json=request_body,
        headers={
            "Authorization": f"Bearer {token}",
            # ADC end-user creds require an explicit quota project for
            # earthengine.googleapis.com — without this we get HTTP 403
            # PERMISSION_DENIED.
            "x-goog-user-project": PROJECT,
        },
        timeout=180,
    )
    elapsed = time.monotonic() - t0

    out: dict[str, Any] = {
        "tile_size": tile_size,
        "status": response.status_code,
        "elapsed_s": round(elapsed, 2),
    }
    if response.status_code != 200:
        out["error_body"] = response.text[:500]
        return out

    body = response.content
    out["response_bytes"] = len(body)
    out.update(parse_tiff_ifd(body))
    return out


def main() -> None:
    print(f"# EE HV computePixels probe — {PROJECT}")
    print(f"# expression: Landsat 9 SR median NDVI ({_DEMO_EXPRESSION_PATH.name})")
    print(f"# center:     ({CENTER_LON}, {CENTER_LAT}) EPSG:4326")
    print(f"# scale:      {SCALE_M_PER_PX} m/px ({SCALE_DEG_PER_PX:.6f} deg/px)")
    print(f"# tile sizes: {TILE_SIZES}")
    print()

    token = get_token()
    results: list[dict[str, Any]] = []

    with httpx.Client() as client:
        for ts in TILE_SIZES:
            print(f"probing tile_size={ts}…", end=" ", flush=True)
            try:
                r = probe(client, token, ts)
                results.append(r)
                if r["status"] == 200 and "match" in r:
                    print(
                        f"{r['response_bytes']:>10}B  "
                        f"IFD={r['width']}×{r['height']}  "
                        f"actual={r['actual_bytes']}  "
                        f"expected={r['expected_bytes']}  "
                        f"ratio={r['ratio_actual_to_expected']:.3f}  "
                        f"{'OK' if r['match'] else 'MISMATCH'}  "
                        f"({r['elapsed_s']}s)"
                    )
                else:
                    print(f"HTTP {r['status']} ({r['elapsed_s']}s)")
            except Exception as exc:
                print(f"EXCEPTION: {type(exc).__name__}: {exc}")
                results.append({"tile_size": ts, "exception": repr(exc)})

    print()
    print("=" * 88)
    print("SUMMARY")
    print("=" * 88)
    header = (
        f"{'tile':>6} {'status':>6} {'sec':>5} {'resp_bytes':>11} "
        f"{'IFD W×H':>11} {'actual':>10} {'expected':>10} "
        f"{'ratio':>6} {'layout':>6} {'sf':>3} {'bps':>3}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        if r.get("status") == 200 and "match" in r:
            print(
                f"{r['tile_size']:>6} "
                f"{r['status']:>6} "
                f"{r['elapsed_s']:>5} "
                f"{r['response_bytes']:>11} "
                f"{r['width']:>4}×{r['height']:<5} "
                f"{r['actual_bytes']:>10} "
                f"{r['expected_bytes']:>10} "
                f"{r['ratio_actual_to_expected']:>6.3f} "
                f"{r['layout']:>6} "
                f"{r['sample_format']:>3} "
                f"{r['bps']:>3}"
            )
        else:
            print(
                f"{r['tile_size']:>6} "
                f"{r.get('status','-'):>6} "
                f"{r.get('elapsed_s','-'):>5} "
                f"  HTTP error / exception"
            )

    print()
    print("# Raw JSON results follow:")
    json.dump(results, sys.stdout, indent=2, default=str)
    print()


if __name__ == "__main__":
    main()
