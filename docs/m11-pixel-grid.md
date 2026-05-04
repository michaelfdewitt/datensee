# M11: PixelGrid as the canonical export shape

## Why

M10's "axis-aware tiling" review fix correctly handles the latitude
dependence of meters-per-degree of longitude inside one export, but
introduces a corollary: the native tile size *in degrees* now varies
with the export's centroid latitude. Two exports at the same nominal
`(crs="EPSG:4326", scale_meters=30, tile_size_pixels=512)` but at
different latitudes use different `tile_size_native` values, so even
though both grids snap to the same `(0, 0)` global anchor, their
*spacings* differ — pixel-level mosaicking across two such exports
requires resampling.

The footgun only fires when composing tiles from *separate* exports at
different latitudes. Within one export and within `datensee retry`
rounds the property still holds. But "make this flow error-free" —
the user mandate — means the corner case must go away.

## Core design call

Stop trying to make `scale_meters` a guaranteed metric measure inside
geographic CRSs. Anyone who wants real pixel-to-pixel correctness
specifies a CRS and a transform, not a scale. Internally, every export
normalizes to a single canonical shape:

```
PixelGrid {
    crs_code:         string         # e.g. "EPSG:4326"
    affine_transform: AffineTransform
    dimensions:       GridDimensions # (width, height) in pixels (int32)
}
```

The shape mirrors Earth Engine's own `PixelGrid` type
([REST docs](https://developers.google.com/earth-engine/reference/rest/v1/PixelGrid)),
so it can be sent verbatim to the HV API `computePixels` endpoint.

For axis-aligned grids (always, in our use), the affine is:

```
AffineTransform {
    scale_x:     +pixel_size      # CRS units per pixel, x
    shear_x:     0
    translate_x: NW corner x      # CRS units
    shear_y:     0
    scale_y:     -pixel_size      # negative — NW-corner origin
    translate_y: NW corner y      # CRS units
}
```

`scale_y` is conventionally **negative**; that puts `(translate_x,
translate_y)` at the NW corner of the top-left pixel. EE expects this
convention; GeoTIFF / GDAL / rasterio all use it. PixelIsArea: pixel
`(u=0, v=0)` is the *corner*, not the centre.

`pixel_size` is the user's `scale_meters` converted via the equator
constant `111_320 m/°` for geographic CRSs (no latitude correction),
or passed through unchanged for projected CRSs. A user who writes
`--crs EPSG:4326 --scale 30` gets `0.000270°/px` everywhere; the
on-the-ground pixel width is ~30 m at the equator and ~15 m at lat 60°.
This is intentional — `scale_meters` becomes a nominal label, not a
guarantee. Cross-export grid alignment is now unconditional.

## Tile representation

Tiles inside the export are **integer pixel rectangles** within the
parent `PixelGrid`. Float bboxes are derived deterministically from
`transform × pixel_offsets` and never persisted.

```
TileCoordinate {
    col_px:    int    # offset within parent grid (PixelIsArea NW corner)
    row_px:    int    # offset within parent grid (rows count downward)
    width_px:  int    # tile width in pixels
    height_px: int    # tile height in pixels
    row, col:  int    # tile-grid indices (unchanged)
    out_row, out_col: int    # M6 two-tier indices (unchanged)
    lineage:   list[int]     # quadtree retry path (unchanged)
}
```

`col_px` and `row_px` are **local** to the parent grid (offsets from
its `translate_x` / `translate_y`), not absolute against a global
`(0,0)`. The parent grid's `translate_*` already encodes where the
export sits in CRS units; tile offsets within it are small.

A tile's per-fetch `PixelGrid` is derived by translating the parent
transform by the tile's pixel offset:

```python
def tile_pixel_grid(parent: PixelGrid, tile: TileCoordinate) -> PixelGrid:
    p = parent.affine_transform
    return PixelGrid(
        crs_code=parent.crs_code,
        affine_transform=AffineTransform(
            scale_x=p.scale_x,
            shear_x=p.shear_x,
            translate_x=p.translate_x + tile.col_px * p.scale_x + tile.row_px * p.shear_x,
            shear_y=p.shear_y,
            scale_y=p.scale_y,
            translate_y=p.translate_y + tile.col_px * p.shear_y + tile.row_px * p.scale_y,
        ),
        dimensions=GridDimensions(width=tile.width_px, height=tile.height_px),
    )
```

That object is what `TileFetchDoFn` sends to EE's `computePixels` — no
further conversion.

## `decompose_region` algorithm

Pseudocode with worked example (region `(minx=10, miny=20, maxx=30,
maxy=40)`, `pixel_size=1.0`, `tile_size_pixels=10`):

```
pixel_size = scale_meters / 111_320 if geographic else scale_meters

# Region bbox in pixel offsets against global (0, 0).
# Rows count DOWNWARD from origin (scale_y < 0), so the NW corner has
# the largest CRS y but the smallest row_px.
col_start_px = floor(minx / pixel_size)             # 10
col_end_px   = ceil(maxx / pixel_size)              # 30
row_start_px = floor(-maxy / pixel_size)            # -40
row_end_px   = ceil(-miny / pixel_size)             # -20

# Snap outward to tile boundaries.
col_start_tile_px = (col_start_px // tile_size) * tile_size                # 10
col_end_tile_px   = ceil(col_end_px / tile_size) * tile_size               # 30
row_start_tile_px = (row_start_px // tile_size) * tile_size                # -40
row_end_tile_px   = ceil(row_end_px / tile_size) * tile_size               # -20

width_px  = col_end_tile_px - col_start_tile_px     # 20
height_px = row_end_tile_px - row_start_tile_px     # 20

parent_grid = PixelGrid(
    crs_code=crs,
    affine_transform=AffineTransform(
        scale_x=pixel_size,       # +1.0
        scale_y=-pixel_size,      # -1.0
        translate_x=col_start_tile_px * pixel_size,    # 10
        translate_y=-row_start_tile_px * pixel_size,   # 40
        shear_x=0, shear_y=0,
    ),
    dimensions=GridDimensions(width=width_px, height=height_px),
)

# Iterate per-tile. col_px / row_px are LOCAL to parent_grid.
for row_offset_px in range(0, height_px, tile_size):
    for col_offset_px in range(0, width_px, tile_size):
        # Bbox for the region-intersection test only; not stored.
        tile_xmin = parent.translate_x + col_offset_px * scale_x
        tile_ymax = parent.translate_y + row_offset_px * scale_y
        tile_xmax = tile_xmin + tile_size * scale_x
        tile_ymin = tile_ymax + tile_size * scale_y
        if region.intersects(Polygon(tile_xmin, tile_ymin, tile_xmax, tile_ymax)):
            tiles.append(TileCoordinate(
                col_px=col_offset_px, row_px=row_offset_px,
                width_px=tile_size, height_px=tile_size,
                row=col_offset_px // tile_size,
                col=col_offset_px // tile_size,
                out_row=..., out_col=...,
            ))
```

For the worked example, four tiles are emitted at offsets `(0,0)`,
`(10,0)`, `(0,10)`, `(10,10)`, each `10×10 px`, each fully inside the
region. CRS bboxes derived from the parent transform:

| `col_px` | `row_px` | bbox |
|---|---|---|
| 0  | 0  | (10, 30, 20, 40) |
| 10 | 0  | (20, 30, 30, 40) |
| 0  | 10 | (10, 20, 20, 30) |
| 10 | 10 | (20, 20, 30, 30) |

## Wire format change

`pipeline-config.schema.json` `tile_grid` block becomes:

```json
{
  "tile_grid": {
    "pixel_grid": {
      "crs_code": "EPSG:32610",
      "affine_transform": {
        "scale_x": 30, "shear_x": 0, "translate_x": 580000,
        "shear_y": 0, "scale_y": -30, "translate_y": 4250000
      },
      "dimensions": { "width": 2048, "height": 2048 }
    },
    "tile_size_pixels": 512,
    "tiles": [
      { "col_px": 0, "row_px": 0, "width_px": 512, "height_px": 512,
        "row": 0, "col": 0, "out_row": 0, "out_col": 0 }
    ]
  }
}
```

The float bbox keys (`x_min`, `y_min`, `x_max`, `y_max`) are removed.
`scale_meters` and `crs` move out of `tile_grid` — the latter via a
`crs` property that delegates to `pixel_grid.crs_code` (back-compat for
read sites). `scale_meters` survives only in `_export_meta.json` as
the user's nominal input, for retry verification.

## Java side mirror

```java
public record PixelGrid(
    @JsonProperty("crs_code") String crsCode,
    @JsonProperty("affine_transform") AffineTransform affineTransform,
    GridDimensions dimensions
) implements Serializable { }

public record AffineTransform(
    @JsonProperty("scale_x") double scaleX,
    @JsonProperty("shear_x") double shearX,
    @JsonProperty("translate_x") double translateX,
    @JsonProperty("shear_y") double shearY,
    @JsonProperty("scale_y") double scaleY,
    @JsonProperty("translate_y") double translateY
) implements Serializable { }

public record GridDimensions(int width, int height) implements Serializable { }

public record TileCoordinate(
    @JsonProperty("col_px") int colPx,
    @JsonProperty("row_px") int rowPx,
    @JsonProperty("width_px") int widthPx,
    @JsonProperty("height_px") int heightPx,
    int row, int col,
    @JsonProperty("out_row") int outRow,
    @JsonProperty("out_col") int outCol,
    List<Integer> lineage
) implements Serializable { ... }
```

`FailedTileRecord` mirrors the same field swap (it's a strict superset
of `TileCoordinate`).

`PipelineConfig.TileGridConfig` carries the parent `PixelGrid`.

## Knock-on simplifications

These die in M11:

- `tiling.py::_pixel_sizes_native` — collapses to a `_pixel_size_native`
  one-liner; `centroid_lat_deg` parameter and the `pyproj.Geod` calls go.
- The "axis-aware tiling caveat" docstring at the top of `tiling.py`
  (M10 review fix #1) — no longer relevant.
- `AssembledCogWriter::TILE_ALIGNMENT_TOLERANCE_PX` and the off-grid /
  off-block bbox guards — pixel positions are integers by construction.
- `TileFetchDoFn`'s `pixelWidth = (xMax-xMin)/tileSize` calculation —
  use `parentGrid.scale_x` directly.
- `test_shifted_region_produces_aligned_grid`'s "centroid latitude"
  caveat in its docstring.

## Migration

Pre-1.0; no shims. `meta.read_meta` should reject old-format meta
files with a clear "regenerate via re-export" message. `_failures.json`
journals from prior versions won't parse — same message. `_retry_tiles.json`
files are short-lived; non-issue.

## Decisions already made

1. `pixel_size` is a single float (axis-symmetric). Anyone wanting
   non-square pixels supplies a transform directly.
2. `scale_meters` for geographic CRSs uses the equator constant,
   no latitude correction (the explicit "scale at origin" choice).
3. Mirror EE's `PixelGrid` / `AffineTransform` shape exactly, including
   the `scale_y < 0` NW-corner convention.
4. `TileCoordinate.col_px` / `row_px` are local to the parent grid
   (small ints), not absolute against a global `(0, 0)`.
5. Hard-fail on old-format meta / journals; no back-compat shims.
6. `runBigQuery` / `loadBigQuery` BigQuery handling stays as M10
   defined it (orthogonal to M11).

## Two-session split

### Session A — Python only

1. New types in `cli/src/datensee/config.py`: `PixelGrid`,
   `AffineTransform`, `GridDimensions`, rewritten `TileCoordinate` and
   `TileGrid`. Add `tile_grid.crs` and `tile_grid.pixel_size`
   delegating properties.
2. Rewrite `cli/src/datensee/tiling.py`: `_pixel_size_native`,
   `tile_pixel_grid`, `tile_bbox`, integer-pixel `decompose_region`.
3. Rewrite `cli/src/datensee/retry.py::split_tile`: halve
   `width_px` / `height_px`, integer math only.
4. Update `cli/src/datensee/api.py`, `submit.py` to thread the new
   shape (the `TileGrid` constructor now takes `pixel_grid=`, not
   `crs=`/`scale_meters=`).
5. Update `cli/src/datensee/meta.py::verify_retry_compatibility` —
   compare `pixel_grid` fields (or just `pixel_size` + `crs`) instead
   of `scale_meters` + `crs`. Keep `scale_meters` as the user's nominal
   input.
6. Update `cli/src/datensee/display.py`, `notebook.py` to pull display
   strings from `pixel_grid` (drop `scale_meters` UI; show
   `pixel_size` in CRS units + `crs`).
7. Update `cli/src/datensee/validation/spatial.py` (uses
   `tile_grid.crs` — keep the property delegation working).
8. Update `contract/pipeline-config.schema.json` to the new wire format.
9. Rewrite `cli/tests/test_tiling.py` — drop the centroid-latitude
   caveats, add a test for pure cross-latitude alignment, add tests
   for the `tile_pixel_grid` and `tile_bbox` helpers.
10. Update `cli/tests/test_retry.py`, `test_meta.py`,
    `test_validation_output_unit.py`, `test_validation_output_integration.py`,
    `test_integration_ee.py` to use `tile.col_px` etc. and the new
    `TileGrid` constructor.
11. Run the full Python suite — green.
12. **Java side will be broken at this point** — Java records expect
    the old wire format. Tests on the Java side WILL fail. That's the
    cost of splitting; we accept it in the intermediate commit.

Commit with a message that flags Java is broken pending Session B.

### Session B — Java only

1. Mirror types in `pipelines/src/main/java/com/datensee/`:
   `PixelGrid.java`, `AffineTransform.java`, `GridDimensions.java`,
   rewrite `TileCoordinate.java` and `FailedTileRecord.java`.
2. Update `PipelineConfig.java::TileGridConfig` to carry `PixelGrid`.
3. Simplify `AssembledCogWriter`: pure integer block math.
   - `outputColPx = (firstTile.colPx() / outputTileSize) * outputTileSize`
   - `tx = (tile.colPx() - outputColPx) / computeTileSize`
   - Delete `TILE_ALIGNMENT_TOLERANCE_PX`, off-grid bbox guard,
     off-block check.
4. Update `CogTranscoder::transcodeFromTileBlocks` to take an
   `AffineTransform` (or origin coords derived from it) instead of
   `outputTileOriginX`/`outputTileOriginY`/`pixelNative`. Same wire
   to the GeoTIFF tags, just sourced from the grid.
5. Update `TileFetchDoFn`: take parent `PixelGrid` in constructor;
   `buildRequestBody(tile)` builds the per-tile grid via translation
   and serializes verbatim. Drop the bbox-derived `pixelWidth` /
   `pixelHeight` lines.
6. Update `TileFetchTransform` and `DatensEEPipeline` to plumb the
   parent grid through.
7. Update Java tests — `TileCoordinateParserTest`, `FailedTileWriterTest`,
   `TileFetchDoFnTest`, `AssembledCogWriterTest`, `PipelineConfigTest`.
8. Run `./gradlew test` — green.
9. Re-run Python integration tests against the rebuilt JAR — green.

Commit, mark M11 ✅ in CLAUDE.md.

## Files

| File | Session A | Session B |
|---|---|---|
| `cli/src/datensee/config.py` | rewrite | — |
| `cli/src/datensee/tiling.py` | rewrite | — |
| `cli/src/datensee/retry.py` | update split_tile | — |
| `cli/src/datensee/api.py` | thread new types | — |
| `cli/src/datensee/submit.py` | TileGrid construction | — |
| `cli/src/datensee/meta.py` | verify changes | — |
| `cli/src/datensee/display.py` | UI strings | — |
| `cli/src/datensee/notebook.py` | UI strings | — |
| `cli/src/datensee/validation/spatial.py` | (small) | — |
| `contract/pipeline-config.schema.json` | rewrite | — |
| `cli/tests/test_*.py` | rewrite affected | — |
| `pipelines/.../TileCoordinate.java` | — | rewrite |
| `pipelines/.../FailedTileRecord.java` | — | rewrite |
| `pipelines/.../PixelGrid.java`, `AffineTransform.java`, `GridDimensions.java` | — | new |
| `pipelines/.../PipelineConfig.java` | — | TileGridConfig change |
| `pipelines/.../io/AssembledCogWriter.java` | — | integer math + drop guards |
| `pipelines/.../io/CogTranscoder.java` | — | geotransform from grid |
| `pipelines/.../fetch/TileFetchDoFn.java` | — | parent grid + serialize |
| `pipelines/.../fetch/TileFetchTransform.java` | — | thread grid |
| `pipelines/.../DatensEEPipeline.java` | — | thread grid |
| `pipelines/src/test/java/...` | — | update affected |

Estimated diff: ~600 lines Python (Session A), ~700 lines Java
(Session B), ~300 lines test rewrites split across both.
