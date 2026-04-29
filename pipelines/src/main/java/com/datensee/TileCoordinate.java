package com.datensee;

import com.fasterxml.jackson.annotation.JsonCreator;
import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.io.Serializable;
import java.util.List;

/**
 * Bounding box of a single compute tile in the target CRS.
 *
 * <p>Serializable so it can travel through the Beam pipeline as a PCollection
 * element.
 *
 * <p>{@code row} and {@code col} are pinned to the *root* compute tile and do
 * not change under adaptive splitting. {@code outRow} and {@code outCol} are
 * M6 two-tier-tiling indices identifying the output COG this tile contributes
 * to (defaults equal {@code row}/{@code col}).
 *
 * <p>{@code lineage} (sketch — adaptive quadtree retry) is the path from the
 * root compute tile to a sub-tile produced by adaptive splitting. Each entry
 * is a quadrant index 0–3, layout-independent of CRS axis order:
 * 0=x-low/y-low, 1=x-high/y-low, 2=x-low/y-high, 3=x-high/y-high.
 * Empty list means root compute tile (the common case). The bounding box
 * is the geometric truth used by the assembler; lineage is metadata for
 * the failure journal and the retry-decision logic.
 *
 * <p>{@code @JsonIgnoreProperties(ignoreUnknown = true)} lets a failures
 * journal (which carries extra fields like {@code error_kind}, {@code
 * attempts}, {@code last_seen}) be fed back in via {@code tiles_file}
 * without a parallel deserialization path.
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public record TileCoordinate(
    @JsonProperty("x_min") double xMin,
    @JsonProperty("y_min") double yMin,
    @JsonProperty("x_max") double xMax,
    @JsonProperty("y_max") double yMax,
    int row,
    int col,
    @JsonProperty("out_row") int outRow,
    @JsonProperty("out_col") int outCol,
    List<Integer> lineage
) implements Serializable {

    /**
     * Jackson entry point — supplies defaults for fields that may be absent
     * in older inline-tiles JSON or in tile coordinate files written before
     * the {@code out_row}/{@code out_col}/{@code lineage} additions. Without
     * this constructor those fields would deserialize as 0/0/null, which is
     * fine for {@code outRow}/{@code outCol} but not for {@code lineage}
     * (NPE risk in downstream consumers expecting an immutable list).
     */
    @JsonCreator
    public static TileCoordinate fromJson(
        @JsonProperty("x_min") double xMin,
        @JsonProperty("y_min") double yMin,
        @JsonProperty("x_max") double xMax,
        @JsonProperty("y_max") double yMax,
        @JsonProperty("row") int row,
        @JsonProperty("col") int col,
        @JsonProperty("out_row") Integer outRow,
        @JsonProperty("out_col") Integer outCol,
        @JsonProperty("lineage") List<Integer> lineage
    ) {
        return new TileCoordinate(
            xMin, yMin, xMax, yMax, row, col,
            outRow != null ? outRow : row,
            outCol != null ? outCol : col,
            lineage != null ? List.copyOf(lineage) : List.of()
        );
    }

    /**
     * Convenience constructor: a root compute tile (no adaptive lineage,
     * out_row/out_col equal row/col). Used by tests and pre-M6 call sites.
     */
    public TileCoordinate(
        double xMin, double yMin, double xMax, double yMax, int row, int col
    ) {
        this(xMin, yMin, xMax, yMax, row, col, row, col, List.of());
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
