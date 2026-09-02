"""Input-resolution tests for the export surface: the Code-Editor bundle,
the exact-grid options, and the scale/pixel_grid mutex.

These are pure (no network, no JVM) — they exercise argument resolution in
``main`` and the validation/branching in ``api.export`` up to the dry-run
return, so the grid contract is pinned from the user's entry point down."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from datensee import api
from datensee.config import AffineTransform, GridDimensions, PipelineConfig, PixelGrid
from datensee.main import _build_pixel_grid, _load_expression_and_region

_EXPR = {"result": "0", "values": {"0": {"constantValue": 1}}}
_REGION = {
    "type": "Polygon",
    "coordinates": [
        [[-122.5, 37.75], [-122.25, 37.75], [-122.25, 38.0], [-122.5, 38.0], [-122.5, 37.75]]
    ],
}


# --- bundle vs two-file resolution --------------------------------------


def test_bare_expression_plus_region_file(tmp_path: Path) -> None:
    expr = tmp_path / "e.json"
    expr.write_text(json.dumps(_EXPR))
    region = tmp_path / "r.geojson"
    region.write_text(json.dumps(_REGION))
    ee_expr, geom = _load_expression_and_region(expr, region)
    assert json.loads(ee_expr) == _EXPR
    assert geom == _REGION


def test_bundle_carries_both(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps({"expression": _EXPR, "region": _REGION}))
    ee_expr, geom = _load_expression_and_region(bundle, None)
    assert json.loads(ee_expr) == _EXPR
    assert geom == _REGION


def test_bundle_with_string_expression(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps({"expression": json.dumps(_EXPR), "region": _REGION}))
    ee_expr, geom = _load_expression_and_region(bundle, None)
    assert json.loads(ee_expr) == _EXPR


def test_bundle_plus_region_file_is_rejected(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps({"expression": _EXPR, "region": _REGION}))
    region = tmp_path / "r.geojson"
    region.write_text(json.dumps(_REGION))
    with pytest.raises(ValueError, match="separate REGION_FILE must not"):
        _load_expression_and_region(bundle, region)


def test_bundle_without_region_returns_none(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps({"expression": _EXPR}))
    ee_expr, geom = _load_expression_and_region(bundle, None)
    assert geom is None


def test_invalid_json_expression_is_actionable(tmp_path: Path) -> None:
    bad = tmp_path / "e.json"
    bad.write_text("{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        _load_expression_and_region(bad, None)


# --- exact-grid option parsing ------------------------------------------


def test_build_pixel_grid_none_when_absent() -> None:
    assert _build_pixel_grid("EPSG:4326", None, None) is None


def test_build_pixel_grid_full() -> None:
    g = _build_pixel_grid("EPSG:32610", "10,0,500000,0,-10,4200000", "1024x768")
    assert g is not None
    assert g.crs_code == "EPSG:32610"
    assert g.affine_transform.scale_x == 10.0
    assert g.affine_transform.translate_x == 500000.0
    assert g.affine_transform.scale_y == -10.0
    assert (g.dimensions.width, g.dimensions.height) == (1024, 768)


@pytest.mark.parametrize(
    ("transform", "dims", "match"),
    [
        ("10,0,0,0,-10,0", None, "must be given together"),
        (None, "512x512", "must be given together"),
        ("10,0,0,0,-10", "512x512", "6 comma-separated"),
        ("10,0,0,0,-10,0,99", "512x512", "6 comma-separated"),
        ("10,0,x,0,-10,0", "512x512", "non-numeric"),
        ("10,0,0,0,-10,0", "512", "WIDTHxHEIGHT"),
        ("10,0,0,0,-10,0", "0x512", "positive"),
    ],
)
def test_build_pixel_grid_errors(transform, dims, match) -> None:
    with pytest.raises(ValueError, match=match):
        _build_pixel_grid("EPSG:4326", transform, dims)


# --- api.export branching (dry-run: no network) --------------------------


def _grid() -> PixelGrid:
    return PixelGrid(
        crs_code="EPSG:32610",
        affine_transform=AffineTransform(
            scale_x=10.0, translate_x=500000.0, scale_y=-10.0, translate_y=4200000.0
        ),
        dimensions=GridDimensions(width=1024, height=1024),
    )


def _dry_export(**kw) -> PipelineConfig:
    captured: list[PipelineConfig] = []
    kw.setdefault("project", "p")
    kw.setdefault("output", "/tmp/x")
    kw.setdefault("runner", "local")
    api.export(
        ee_expression=json.dumps(_EXPR),
        dry_run=True,
        confirm_callback=captured.append,
        **kw,
    )
    return captured[0]


def test_scale_and_pixel_grid_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        _dry_export(region=_REGION, scale=30.0, pixel_grid=_grid())


def test_region_required_without_grid() -> None:
    with pytest.raises(ValueError, match="region is required"):
        _dry_export(scale=30.0)


def test_exact_grid_needs_no_region_and_is_used_verbatim() -> None:
    cfg = _dry_export(pixel_grid=_grid())
    assert cfg.tile_grid.pixel_grid == _grid()
    assert cfg.tile_grid.tile_size_pixels == 512
    assert len(cfg.tile_grid.tiles) == 4


def test_exact_grid_with_region_filters_tiles() -> None:
    # A region covering the whole grid keeps all four.
    from pyproj import Transformer

    to_wgs = Transformer.from_crs("EPSG:32610", "EPSG:4326", always_xy=True)
    corners = [to_wgs.transform(e, n) for e in (499000, 511000) for n in (4188000, 4201000)]
    lons, lats = [c[0] for c in corners], [c[1] for c in corners]
    region = {
        "type": "Polygon",
        "coordinates": [
            [
                [min(lons), min(lats)],
                [max(lons), min(lats)],
                [max(lons), max(lats)],
                [min(lons), max(lats)],
                [min(lons), min(lats)],
            ]
        ],
    }
    cfg = _dry_export(pixel_grid=_grid(), region=region)
    assert len(cfg.tile_grid.tiles) == 4


def test_default_scale_applied_when_neither_given() -> None:
    cfg = _dry_export(region=_REGION)
    # 30 m over a geographic grid via the equator constant.
    assert cfg.tile_grid.pixel_grid.crs_code == "EPSG:4326"
    assert cfg.tile_grid.pixel_grid.affine_transform.scale_x == pytest.approx(
        30 / 111_320, rel=1e-6
    )


def test_exact_grid_meta_records_the_grid(tmp_path: Path) -> None:
    """The meta sidecar (used by retry) captures the exact grid, not a scale."""
    cfg = _dry_export(pixel_grid=_grid())
    # scale_x is metres in UTM; meta.scale_meters derives from it.
    assert cfg.tile_grid.pixel_grid.affine_transform.scale_x == 10.0
