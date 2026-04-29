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
 * <p>The {@code journalReason} field is the latest retry-policy verdict
 * for this entry — what the system decided to do (or not do) about it
 * the last time the journal was touched. Distinct from {@code errorKind}
 * (which says what EE told us went wrong); this field says why the
 * record is still in the journal. Mutates across retry rounds — a tile
 * that's {@link #JOURNAL_REASON_FAILED} in round 1 may become
 * {@link #JOURNAL_REASON_DEPTH_CAP} after round 3 once it's exhausted
 * the split budget. The journal reflects the latest verdict, not history.
 *
 * <p>{@code attempts} counts retry rounds *across* journal cycles so
 * we can enforce a depth budget (e.g. give up after N adaptive splits).
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
    @JsonProperty("last_seen") Instant lastSeen,
    @JsonProperty("journal_reason") String journalReason
) implements Serializable {

    // Canonical journal_reason values. Strings (not an enum) so the wire
    // format stays human-readable in `jq` and stable across language
    // boundaries — the Python retry CLI uses identical literals.

    /** Fresh failure from the pipeline's fetch path. Retry-eligible. */
    public static final String JOURNAL_REASON_FAILED = "failed";

    /**
     * Was split-eligible ({@code MEMORY_EXCEEDED} / {@code COMPUTATION_TIMEOUT})
     * but the lineage already reached {@code max_depth}. Bumping the
     * depth cap on a future {@code datensee retry} run can rescue these.
     */
    public static final String JOURNAL_REASON_DEPTH_CAP = "depth_cap";

    /**
     * {@code error_kind} is in the terminal set
     * ({@code AUTH_ERROR} / {@code FATAL_REQUEST}). Never retried regardless
     * of config — surface to the user.
     */
    public static final String JOURNAL_REASON_TERMINAL = "terminal";

    /**
     * {@code error_kind} isn't recognized by the current retry policy
     * (e.g. EE added a new error mode). Held in the journal deliberately
     * so a new failure mode never silently disappears.
     */
    public static final String JOURNAL_REASON_UNKNOWN_KIND = "unknown_kind";

    /** Build a record from a TileCoordinate with placeholder error metadata. */
    public static FailedTileRecord fromTile(TileCoordinate tile) {
        return fromTileWithError(tile, EeErrorKind.UNKNOWN, null, null, 0);
    }

    /**
     * Build a record from a TileCoordinate plus classified error context.
     * Used by {@code TileFetchDoFn}'s dead-letter side output after a
     * tile exhausts its retry budget. Always stamps
     * {@code journal_reason = "failed"} since the pipeline only sees
     * fresh failures; the retry CLI is responsible for re-stamping
     * carried-over records with {@code "depth_cap"} / {@code "terminal"} /
     * {@code "unknown_kind"} as appropriate.
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
            now,
            JOURNAL_REASON_FAILED
        );
    }
}
