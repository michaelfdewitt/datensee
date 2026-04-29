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
    UNKNOWN
}
