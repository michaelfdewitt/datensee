package com.datensee.pixel;

import com.fasterxml.jackson.annotation.JsonCreator;
import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.io.Serializable;
import java.util.List;

/**
 * A compute tile as an integer pixel rectangle inside the parent
 * {@link PixelGrid}.
 *
 * <p>{@code colPx} / {@code rowPx} are <strong>local</strong> offsets from
 * the parent grid's {@code translateX} / {@code translateY}, rather than
 * absolute coordinates against {@code (0, 0)}. The parent translate encodes
 * where the export sits in CRS units. {@code widthPx} / {@code heightPx}
 * are the tile's pixel dimensions; for root tiles they equal
 * {@code tile_size_pixels}, while quadtree split children halve each axis.
 *
 * <p>Serializable to allow traversal through Beam PCollections.
 *
 * <p>{@code row} and {@code col} are pinned to the root compute tile
 * and do not change under adaptive splitting (row=0 is the northernmost
 * tile, col=0 the westernmost). {@code outRow}/{@code outCol} identify
 * the output COG this tile contributes to (defaulting to {@code row}/{@code col}).
 *
 * <p>{@code lineage} (adaptive quadtree retry) represents the path from the root
 * compute tile to a sub-tile. Each entry is a quadrant index 0–3,
 * layout-independent of CRS axis order: {@code 0=x-low/y-low},
 * {@code 1=x-high/y-low}, {@code 2=x-low/y-high}, {@code 3=x-high/y-high}.
 * An empty list denotes a root compute tile.
 *
 * <p>{@code @JsonIgnoreProperties(ignoreUnknown = true)} allows failures
 * journals (carrying extra fields such as {@code error_kind} and {@code attempts})
 * to be fed directly into {@code tiles_file} without separate deserialization logic.
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public record TileCoordinate(
    @JsonProperty("col_px") int colPx,
    @JsonProperty("row_px") int rowPx,
    @JsonProperty("width_px") int widthPx,
    @JsonProperty("height_px") int heightPx,
    int row,
    int col,
    @JsonProperty("out_row") int outRow,
    @JsonProperty("out_col") int outCol,
    List<Integer> lineage
) implements Serializable {

    /**
     * Jackson entry point: supplies defaults for fields that may be absent
     * in older tile JSON payloads or journals.
     */
    @JsonCreator
    public static TileCoordinate fromJson(
        @JsonProperty("col_px") int colPx,
        @JsonProperty("row_px") int rowPx,
        @JsonProperty("width_px") int widthPx,
        @JsonProperty("height_px") int heightPx,
        @JsonProperty("row") int row,
        @JsonProperty("col") int col,
        @JsonProperty("out_row") Integer outRow,
        @JsonProperty("out_col") Integer outCol,
        @JsonProperty("lineage") List<Integer> lineage
    ) {
        return new TileCoordinate(
            colPx, rowPx, widthPx, heightPx, row, col,
            outRow != null ? outRow : row,
            outCol != null ? outCol : col,
            lineage != null ? List.copyOf(lineage) : List.of()
        );
    }

    /**
     * Convenience constructor: a root compute tile (no adaptive lineage,
     * out_row/out_col equal row/col). Used by tests and pre-two-tier call sites.
     */
    public TileCoordinate(
        int colPx, int rowPx, int widthPx, int heightPx, int row, int col
    ) {
        this(colPx, rowPx, widthPx, heightPx, row, col, row, col, List.of());
    }

    /** Returns a human-readable tile identifier for logging. */
    public String id() {
        if (lineage == null || lineage.isEmpty()) {
            return String.format("tile[r=%d,c=%d]", row, col);
        }
        StringBuilder sb = new StringBuilder();
        sb.append("tile[r=").append(row).append(",c=").append(col).append(",q=");
        for (int q : lineage) {
            sb.append(q);
        }
        return sb.append(']').toString();
    }
}
