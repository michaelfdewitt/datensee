package com.datensee.fetch;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

/**
 * Tests for {@link EeErrorKind#classify(int, String)}.
 *
 * <p>EE returns HTTP 400 for both OOM and computation-timeout, so the
 * discriminant is body-string matching. Pin the signatures we rely on
 * (substrings that appear in real EE responses) so a regression in the
 * classifier would surface here, not as silently-wrong split decisions.
 */
class EeErrorKindTest {

    @Test
    void memoryLimitExceededBecomesMemoryExceeded() {
        assertEquals(
            EeErrorKind.MEMORY_EXCEEDED,
            EeErrorKind.classify(400, "User memory limit exceeded.")
        );
        assertEquals(
            EeErrorKind.MEMORY_EXCEEDED,
            EeErrorKind.classify(400, "Earth Engine memory capacity exceeded")
        );
    }

    @Test
    void timedOutBecomesComputationTimeout() {
        assertEquals(
            EeErrorKind.COMPUTATION_TIMEOUT,
            EeErrorKind.classify(400, "Computation timed out.")
        );
        assertEquals(
            EeErrorKind.COMPUTATION_TIMEOUT,
            EeErrorKind.classify(400, "Request timed out after 60s")
        );
        assertEquals(
            EeErrorKind.COMPUTATION_TIMEOUT,
            EeErrorKind.classify(400, "Deadline exceeded while evaluating expression")
        );
    }

    @Test
    void gatewayTimeoutsAreRetryableNotSplitEligible() {
        // COMPUTATION_TIMEOUT drives quadtree splitting, so it must only
        // come from EE's own complexity verdict (HTTP 400 + signature).
        // A 504 — or any 5xx whose body mentions a timeout — is
        // infrastructure trouble; splitting on it would turn a transient
        // storm into a 4x request cascade.
        assertEquals(
            EeErrorKind.RETRYABLE_SERVER,
            EeErrorKind.classify(504, "Gateway timeout")
        );
        assertEquals(
            EeErrorKind.RETRYABLE_SERVER,
            EeErrorKind.classify(504, "")
        );
        assertEquals(
            EeErrorKind.RETRYABLE_SERVER,
            EeErrorKind.classify(503, "upstream request timed out")
        );
        assertEquals(
            EeErrorKind.RETRYABLE_SERVER,
            EeErrorKind.classify(500, "deadline exceeded")
        );
    }

    @Test
    void httpStatus429IsRateLimited() {
        assertEquals(EeErrorKind.RATE_LIMITED, EeErrorKind.classify(429, "Too many requests"));
    }

    @Test
    void httpStatus5xxIsRetryableServer() {
        assertEquals(EeErrorKind.RETRYABLE_SERVER, EeErrorKind.classify(500, ""));
        assertEquals(EeErrorKind.RETRYABLE_SERVER, EeErrorKind.classify(502, ""));
        assertEquals(EeErrorKind.RETRYABLE_SERVER, EeErrorKind.classify(503, ""));
    }

    @Test
    void httpStatus401IsAuthError() {
        assertEquals(EeErrorKind.AUTH_ERROR, EeErrorKind.classify(401, "Unauthorized"));
    }

    @Test
    void plain400IsFatalRequestUnlessBodyMatches() {
        // Generic 400 with no recognizable signature: don't retry.
        assertEquals(
            EeErrorKind.FATAL_REQUEST,
            EeErrorKind.classify(400, "Invalid expression: unknown operator 'foo'")
        );
    }

    @Test
    void unrecognizedStatusBecomesUnknown() {
        assertEquals(EeErrorKind.UNKNOWN, EeErrorKind.classify(-1, ""));
        assertEquals(EeErrorKind.UNKNOWN, EeErrorKind.classify(200, ""));
    }

    @Test
    void splitAllowlistIsConservative() {
        // Only EE complexity signals split. Generic 5xx, rate-limit, auth
        // never trigger splitting.
        assertTrue(EeErrorKind.MEMORY_EXCEEDED.isSplitEligible());
        assertTrue(EeErrorKind.COMPUTATION_TIMEOUT.isSplitEligible());
        assertFalse(EeErrorKind.RATE_LIMITED.isSplitEligible());
        assertFalse(EeErrorKind.RETRYABLE_SERVER.isSplitEligible());
        assertFalse(EeErrorKind.AUTH_ERROR.isSplitEligible());
        assertFalse(EeErrorKind.FATAL_REQUEST.isSplitEligible());
        assertFalse(EeErrorKind.UNKNOWN.isSplitEligible());
    }

    @Test
    void retryAllowlistCoversTransientInfra() {
        assertTrue(EeErrorKind.RATE_LIMITED.isRetryable());
        assertTrue(EeErrorKind.RETRYABLE_SERVER.isRetryable());
        assertTrue(EeErrorKind.UNKNOWN.isRetryable());  // err on the side of retry
        assertFalse(EeErrorKind.AUTH_ERROR.isRetryable());
        assertFalse(EeErrorKind.FATAL_REQUEST.isRetryable());
        assertFalse(EeErrorKind.MEMORY_EXCEEDED.isRetryable());  // split, don't retry-same
        assertFalse(EeErrorKind.COMPUTATION_TIMEOUT.isRetryable());
    }

    @Test
    void caseInsensitiveBodyMatching() {
        assertEquals(
            EeErrorKind.MEMORY_EXCEEDED,
            EeErrorKind.classify(400, "USER MEMORY LIMIT EXCEEDED")
        );
        assertEquals(
            EeErrorKind.COMPUTATION_TIMEOUT,
            EeErrorKind.classify(400, "Computation TIMED OUT")
        );
    }
}
