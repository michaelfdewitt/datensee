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
 * <p>Earth Engine doesn't expose a structured error code for OOM /
 * timeout — it returns HTTP 400 with a body that contains a phrase like
 * "User memory limit exceeded" or "Computation timed out". The
 * classifier in {@link EeApiException} (or, eventually, a dedicated
 * classifier) inspects the body string to disambiguate.
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
     * <p>EE's HV API returns HTTP 400 with a body string for both OOM
     * and computation-timeout — there's no structured error code, so
     * the discriminant is body-string matching on stable substrings.
     * 504 (Gateway Timeout) from the EE backend is also treated as a
     * computation timeout. Matches are case-insensitive.
     *
     * <p>If the body doesn't match a known pattern, the classifier falls
     * back to a status-only verdict so we never silently mis-classify a
     * future EE error message change.
     *
     * @param httpStatus HTTP status code from the response (-1 if not applicable, e.g. network error)
     * @param body       raw response body (may be null)
     */
    public static EeErrorKind classify(int httpStatus, String body) {
        String b = body == null ? "" : body.toLowerCase(java.util.Locale.ROOT);

        // EE-specific signatures that should drive splitting. Match
        // before any status-based fallback so we don't bucket a "memory
        // limit exceeded" 400 as plain FATAL_REQUEST.
        boolean memorySignature =
            b.contains("memory limit") || b.contains("memory capacity");
        boolean timeoutSignature =
            b.contains("timed out") || b.contains("computation timed")
            || b.contains("deadline exceeded");

        if (memorySignature) {
            return MEMORY_EXCEEDED;
        }
        if (timeoutSignature) {
            return COMPUTATION_TIMEOUT;
        }
        // 504 from the EE gateway is a backend timeout even without a
        // body match.
        if (httpStatus == 504) {
            return COMPUTATION_TIMEOUT;
        }
        if (httpStatus == 429) {
            return RATE_LIMITED;
        }
        if (httpStatus == 401) {
            return AUTH_ERROR;
        }
        if (httpStatus == 403 && (b.contains("auth") || b.contains("credential"))) {
            return AUTH_ERROR;
        }
        if (httpStatus >= 500 && httpStatus < 600) {
            return RETRYABLE_SERVER;
        }
        if (httpStatus == 400 || httpStatus == 403 || httpStatus == 404) {
            return FATAL_REQUEST;
        }
        return UNKNOWN;
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
