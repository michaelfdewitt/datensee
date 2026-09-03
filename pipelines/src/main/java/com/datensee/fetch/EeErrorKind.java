package com.datensee.fetch;

/**
 * Classification of why a tile fetch failed, written into the failures
 * journal so a future {@code datensee retry --journal} can decide whether
 * to retry the tile as-is or split it into quadrants.
 *
 * <p>Conservative split allowlist for adaptive retry: only
 * {@link #MEMORY_EXCEEDED} and {@link #COMPUTATION_TIMEOUT} should ever
 * trigger a quadtree split. The other kinds are either transient
 * (retry the same tile) or permanent (don't retry at all). Splitting
 * a 5xx error would mask transient infrastructure issues; splitting an
 * auth error would just produce four more auth failures.
 *
 * <p>Earth Engine does not expose a structured error code for OOM or
 * timeout; it returns HTTP 400 with text indicating "User memory limit exceeded"
 * or "Computation timed out". The classifier inspects the response body
 * to disambiguate.
 */
public enum EeErrorKind {
    /** EE could not compute the tile within memory limits. SPLIT-ELIGIBLE. */
    MEMORY_EXCEEDED,
    /** EE timed out evaluating the expression for this tile. SPLIT-ELIGIBLE. */
    COMPUTATION_TIMEOUT,
    /** HTTP 429. Retry the same tile after backoff. */
    RATE_LIMITED,
    /** Generic 5xx. Retry the same tile. */
    RETRYABLE_SERVER,
    /** HTTP 400/403/404 with no recognizable EE-specific signature. Don't retry. */
    FATAL_REQUEST,
    /** HTTP 401/403 specifically about credentials. Don't retry; surface to user. */
    AUTH_ERROR,
    /** Anything we couldn't classify. Retry the same tile, log the body. */
    UNKNOWN;

    /**
     * Classify a (status, body) pair into an {@code EeErrorKind}.
     *
     * <p>EE's High Volume API returns HTTP 400 for both OOM and computation
     * timeouts. Classification matches substring patterns case-insensitively.
     * Split-eligible signatures are restricted to HTTP 400 responses:
     * gateway timeouts (HTTP 504) or server errors (HTTP 5xx) reflect
     * infrastructure conditions and remain categorized as
     * {@code RETRYABLE_SERVER}.
     *
     * <p>If the body does not match a known pattern, the classifier falls
     * back to a status-only classification.
     *
     * @param httpStatus HTTP status code from the response (-1 if not applicable)
     * @param body       raw response body (may be null)
     */
    public static EeErrorKind classify(int httpStatus, String body) {
        String b = body == null ? "" : body.toLowerCase(java.util.Locale.ROOT);

        return switch (httpStatus) {
            case 400 -> {
                if (b.contains("memory limit") || b.contains("memory capacity")) {
                    yield MEMORY_EXCEEDED;
                }
                if (b.contains("timed out") || b.contains("computation timed")
                    || b.contains("deadline exceeded")) {
                    yield COMPUTATION_TIMEOUT;
                }
                yield FATAL_REQUEST;
            }
            case 429 -> RATE_LIMITED;
            case 401 -> AUTH_ERROR;
            case 403 -> (b.contains("auth") || b.contains("credential")) ? AUTH_ERROR : FATAL_REQUEST;
            case 404 -> FATAL_REQUEST;
            default -> (httpStatus >= 500 && httpStatus < 600) ? RETRYABLE_SERVER : UNKNOWN;
        };
    }

    /**
     * Whether a tile that failed with this kind should trigger an
     * adaptive quadtree split when re-fed through {@code datensee retry}.
     * Conservative by design: only EE-specific complexity signals are
     * split-eligible.
     */
    public boolean isSplitEligible() {
        return this == MEMORY_EXCEEDED || this == COMPUTATION_TIMEOUT;
    }

    /**
     * Whether a tile that failed with this kind should be retried with
     * the same bbox (no split). Transient infrastructure failures, not
     * complexity ones.
     */
    public boolean isRetryable() {
        return this == RATE_LIMITED || this == RETRYABLE_SERVER || this == UNKNOWN;
    }
}
