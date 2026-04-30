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
}
