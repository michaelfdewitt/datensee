package com.datensee.options;

import org.apache.beam.sdk.options.Description;
import org.apache.beam.sdk.options.PipelineOptions;
import org.apache.beam.sdk.options.Validation;

/** Pipeline options for the DatensEE export pipeline. */
public interface DatensEEOptions extends PipelineOptions {

    @Description("Path to the pipeline-config.json file written by the Python CLI.")
    @Validation.Required
    String getConfigFile();

    void setConfigFile(String configFile);

    /**
     * Inheritable file descriptor (in this JVM's process) from which to read
     * the caller's OAuth access token. When set, the pipeline reads the FD
     * to EOF, installs the bytes as a {@code GoogleCredentials} onto the
     * {@code GcpOptions}, and zeroes the buffer. When unset (null or
     * negative), the pipeline falls back to application-default credentials.
     *
     * <p>The FD approach avoids putting bearer tokens on argv (visible in
     * {@code /proc/<pid>/cmdline}) or the environment (visible in
     * {@code /proc/<pid>/environ}) — the FD number itself is fine to expose.
     * See {@code datensee.submit} for the parent side of this contract.
     */
    @Description(
        "Inheritable read-end FD carrying the caller's OAuth access token. "
        + "When set, the pipeline reads the FD to EOF and installs the bytes "
        + "as a GoogleCredentials on the GcpOptions. When unset, falls back "
        + "to application default credentials."
    )
    Integer getUserTokenFd();

    void setUserTokenFd(Integer fd);

    /**
     * Optional email of a service account to impersonate for EE auth on
     * workers. When set, each worker constructs an
     * {@link com.google.auth.oauth2.ImpersonatedCredentials} that uses
     * its own ADC (the worker SA) to mint short-lived (1 h) tokens as
     * the target SA via the IAM API. The worker SA must have
     * {@code roles/iam.serviceAccountTokenCreator} on the target SA.
     *
     * <p>Use this when the worker SA itself isn't the right principal
     * for EE — e.g. when a dedicated, pre-blessed runner SA is the only
     * identity configured for the project's EE setup, but you don't
     * want to actually run Dataflow workers AS that SA (which would
     * require giving it Dataflow + GCS permissions too).
     *
     * <p>When unset, workers use their own ADC directly.
     */
    @Description(
        "Email of a service account to impersonate for EE auth on workers. "
        + "Worker SA must have roles/iam.serviceAccountTokenCreator on the "
        + "target. Unset = use worker ADC directly."
    )
    String getEeImpersonateSa();

    void setEeImpersonateSa(String value);
}
