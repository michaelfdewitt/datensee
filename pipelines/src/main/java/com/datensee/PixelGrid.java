package com.datensee;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.io.Serializable;

/**
 * Canonical export grid: CRS code + 6-tuple affine transform + integer
 * dimensions. Mirrors Earth Engine's own {@code PixelGrid} type so it
 * can be sent verbatim to the {@code computePixels} endpoint.
 *
 * <p>Tiles inside the export are integer pixel rectangles within this
 * grid; each tile's per-fetch grid is derived by translating this
 * parent's affine by the tile's pixel offset.
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public record PixelGrid(
    @JsonProperty("crs_code") String crsCode,
    @JsonProperty("affine_transform") AffineTransform affineTransform,
    GridDimensions dimensions
) implements Serializable {

    /**
     * Return the per-tile {@link PixelGrid} obtained by translating this
     * parent's affine to the tile's NW corner and resizing dimensions to
     * the tile's pixel size. The output is what gets serialized into the
     * {@code grid} field of an EE {@code computePixels} request.
     */
    public PixelGrid forTile(TileCoordinate tile) {
        AffineTransform p = affineTransform;
        AffineTransform shifted = new AffineTransform(
            p.scaleX(),
            p.shearX(),
            p.translateX() + tile.colPx() * p.scaleX() + tile.rowPx() * p.shearX(),
            p.shearY(),
            p.scaleY(),
            p.translateY() + tile.colPx() * p.shearY() + tile.rowPx() * p.scaleY()
        );
        return new PixelGrid(
            crsCode,
            shifted,
            new GridDimensions(tile.widthPx(), tile.heightPx())
        );
    }
}
