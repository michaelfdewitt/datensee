package com.datensee;

import com.datensee.fetch.EeErrorKind;
import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.io.Serializable;
import java.time.Instant;

/**
 * One line in the failures journal.
 *
 * <p>This record is the wire contract for {@code _failures.json} (NDJSON,
 * one record per line). It is a *superset* of {@link TileCoordinate}: a
 * future {@code datensee retry --journal} command can feed the journal
 * back in via the existing {@code tiles_file} input path because
 * {@code TileCoordinate} ignores the extra fields. Keeping the formats
 * compatible means failure → retry is a one-liner with no parallel
 * deserialization code path.
 *
 * <p>The {@code errorKind} field is the load-bearing part for adaptive
 * retry: only {@link EeErrorKind#MEMORY_EXCEEDED} and
 * {@link EeErrorKind#COMPUTATION_TIMEOUT} should trigger a quadtree
 * split; the rest should retry the tile unchanged. See
 * {@link EeErrorKind} for the full table.
 *
 * <p>{@code attempts} counts retry rounds *across* journal cycles so
 * we can enforce a depth budget (e.g. give up after N adaptive splits).
 *
 * <p>Sketch only at present — the dead-letter side output in
 * {@link com.datensee.fetch.TileFetchDoFn} still emits raw
 * {@code TileCoordinate}; classifying and populating these fields is a
 * follow-up. Today {@link com.datensee.io.FailedTileWriter} emits
 * placeholder values (errorKind=UNKNOWN, attempts=0) so the on-disk
 * format is stable now and the only thing left to wire is the
 * classifier.
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public record FailedTileRecord(
    @JsonProperty("x_min") double xMin,
    @JsonProperty("y_min") double yMin,
    @JsonProperty("x_max") double xMax,
    @JsonProperty("y_max") double yMax,
    int row,
    int col,
    @JsonProperty("out_row") int outRow,
    @JsonProperty("out_col") int outCol,
    @JsonProperty("lineage") java.util.List<Integer> lineage,
    @JsonProperty("error_kind") EeErrorKind errorKind,
    @JsonProperty("error_message") String errorMessage,
    @JsonProperty("http_status") Integer httpStatus,
    int attempts,
    @JsonProperty("first_seen") Instant firstSeen,
    @JsonProperty("last_seen") Instant lastSeen
) implements Serializable {

    /** Build a record from a TileCoordinate with placeholder error metadata. */
    public static FailedTileRecord fromTile(TileCoordinate tile) {
        return fromTileWithError(tile, EeErrorKind.UNKNOWN, null, null, 0);
    }

    /**
     * Build a record from a TileCoordinate plus classified error context.
     * Used by {@code TileFetchDoFn}'s dead-letter side output after a
     * tile exhausts its retry budget.
     */
    public static FailedTileRecord fromTileWithError(
        TileCoordinate tile,
        EeErrorKind errorKind,
        String errorMessage,
        Integer httpStatus,
        int attempts
    ) {
        Instant now = Instant.now();
        return new FailedTileRecord(
            tile.xMin(), tile.yMin(), tile.xMax(), tile.yMax(),
            tile.row(), tile.col(),
            tile.outRow(), tile.outCol(),
            tile.lineage() != null ? tile.lineage() : java.util.List.of(),
            errorKind,
            errorMessage,
            httpStatus,
            attempts,
            now,
            now
        );
    }
}
