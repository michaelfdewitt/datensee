package com.datensee.fetch;

import java.io.IOException;

/**
 * Exception thrown when the EE High Volume API returns a non-200 response.
 *
 * <p>Classifies HTTP status codes as retryable or fatal:
 * <ul>
 *   <li>429 (rate limited), 503 (service unavailable): retryable with backoff
 *   <li>400 (bad request), 403 (forbidden), 404 (not found): fatal, dead-letter immediately
 *   <li>Other 5xx: retryable (transient server error)
 * </ul>
 */
public final class EeApiException extends IOException {

    private final int httpStatus;
    private final boolean retryable;
    private final String truncatedBody;

    public EeApiException(int httpStatus, String tileId, String responseBody) {
        super(String.format(
            "EE HV API returned HTTP %d for %s: %s",
            httpStatus, tileId,
            responseBody.length() > 200 ? responseBody.substring(0, 200) : responseBody
        ));
        this.httpStatus = httpStatus;
        this.retryable = classifyRetryable(httpStatus);
        this.truncatedBody = responseBody.length() > 500
            ? responseBody.substring(0, 500) : responseBody;
    }

    public int httpStatus() {
        return httpStatus;
    }

    public boolean isRetryable() {
        return retryable;
    }

    public String truncatedBody() {
        return truncatedBody;
    }

    private static boolean classifyRetryable(int status) {
        return switch (status) {
            case 429, 503 -> true;
            case 400, 403, 404 -> false;
            default -> status >= 500;
        };
    }
}
