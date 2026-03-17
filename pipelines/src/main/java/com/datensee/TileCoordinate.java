package com.datensee;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.io.Serializable;

/**
 * Bounding box of a single export tile in the target CRS.
 *
 * <p>Serializable so it can travel through the Beam pipeline as a PCollection element.
 */
public record TileCoordinate(
    @JsonProperty("x_min") double xMin,
    @JsonProperty("y_min") double yMin,
    @JsonProperty("x_max") double xMax,
    @JsonProperty("y_max") double yMax,
    int row,
    int col
) implements Serializable {

    /** Returns a human-readable tile identifier for logging. */
    public String id() {
        return String.format("tile[r=%d,c=%d]", row, col);
    }
}
