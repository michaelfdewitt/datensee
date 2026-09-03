package com.datensee.fetch;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

/**
 * Builds actionable remediation strings for {@link EeErrorKind#AUTH_ERROR}
 * failures on the EE High Volume API.
 *
 * <p>Dataflow workers run under a service account that requires
 * {@code roles/earthengine.viewer} (or equivalent) on the Earth Engine project.
 * When workers receive HTTP 403 errors, this helper formats remediation
 * instructions.
 */
public final class EeAuthRemediation {

    private static final String METADATA_SA_EMAIL_URL =
        "http://metadata.google.internal/computeMetadata/v1/instance/"
        + "service-accounts/default/email";

    private EeAuthRemediation() { }

    /**
     * Best-effort discovery of the worker's service-account email via the
     * GCE metadata server. Returns {@code null} when unavailable (e.g.
     * Direct runner in local development); callers must handle null.
     */
    public static String discoverWorkerServiceAccount(HttpClient httpClient) {
        try {
            HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(METADATA_SA_EMAIL_URL))
                .header("Metadata-Flavor", "Google")
                .timeout(Duration.ofSeconds(2))
                .GET()
                .build();
            HttpResponse<String> response = httpClient.send(
                request, HttpResponse.BodyHandlers.ofString()
            );
            if (response.statusCode() == 200) {
                String body = response.body().trim();
                return body.isEmpty() ? null : body;
            }
        } catch (Exception ignored) {
            // Metadata server unavailable (local mode, network blocked,
            // etc). The caller falls back to a generic remediation hint.
        }
        return null;
    }

    /**
     * Single-line summary suitable for a JSON journal record.
     *
     * <p>The remediation is concise (no embedded newlines) so it lands
     * cleanly in {@code _failures.json}; the original truncated EE body
     * is appended after the hint so the underlying error is still
     * visible to anyone debugging.
     */
    public static String formatJournalMessage(
        String workerServiceAccount,
        String geeProject,
        String originalBody
    ) {
        String sa = workerServiceAccount != null
            ? workerServiceAccount
            : "the Dataflow worker service account";
        String hint = String.format(
            "AUTH_ERROR: %s lacks Earth Engine access on project %s. "
            + "Run: gcloud projects add-iam-policy-binding %s "
            + "--member=serviceAccount:%s --role=roles/earthengine.viewer "
            + "(and confirm the EE API is enabled on %s).",
            sa, geeProject, geeProject, sa, geeProject
        );
        if (originalBody == null || originalBody.isEmpty()) {
            return hint;
        }
        return hint + " EE response: " + originalBody;
    }

    /**
     * Multi-line message intended for {@code LOG.error}, where line
     * breaks render legibly in Cloud Logging. Logged at most once per
     * worker so the user sees one clear instruction rather than
     * thousands of repeats.
     */
    public static String formatLogMessage(String workerServiceAccount, String geeProject) {
        String sa = workerServiceAccount != null
            ? workerServiceAccount
            : "the Dataflow worker service account (could not auto-detect)";
        return String.format(
            "%n"
            + "  Earth Engine HV API returned 403: worker has no access.%n"
            + "  Worker service account : %s%n"
            + "  EE project             : %s%n"
            + "  Fix:%n"
            + "    gcloud projects add-iam-policy-binding %s \\%n"
            + "        --member=serviceAccount:%s \\%n"
            + "        --role=roles/earthengine.viewer%n"
            + "  Also confirm the EE API is enabled:%n"
            + "    gcloud services enable earthengine.googleapis.com --project=%s",
            sa, geeProject, geeProject, sa, geeProject
        );
    }
}
