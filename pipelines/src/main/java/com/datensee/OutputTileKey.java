package com.datensee;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.io.Serializable;

/**
 * Composite key for the M6 GroupByKey: which output tile a compute tile
 * belongs to.
 *
 * <p>Compute tiles with the same {@code (outRow, outCol)} share an output
 * COG. The Beam {@code GroupByKey} step shuffles compute tiles together
 * by this key so the assembler can stitch them into a single
 * multi-block COG.
 */
public record OutputTileKey(
    @JsonProperty("out_row") int outRow,
    @JsonProperty("out_col") int outCol
) implements Serializable, Comparable<OutputTileKey> {

    @Override
    public int compareTo(OutputTileKey other) {
        int rowCmp = Integer.compare(this.outRow, other.outRow);
        return rowCmp != 0 ? rowCmp : Integer.compare(this.outCol, other.outCol);
    }

    /** Filename for the output COG, e.g. {@code tile_r0003_c0007.tif}. */
    public String filename() {
        return String.format("tile_r%04d_c%04d.tif", outRow, outCol);
    }
}
