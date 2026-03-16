package com.geedf.options;

import org.apache.beam.sdk.options.Description;
import org.apache.beam.sdk.options.PipelineOptions;
import org.apache.beam.sdk.options.Validation;

/** Pipeline options for the GEE Dataflow export pipeline. */
public interface GeeDataflowOptions extends PipelineOptions {

    @Description("Path to the pipeline-config.json file written by the Python CLI.")
    @Validation.Required
    String getConfigFile();

    void setConfigFile(String configFile);
}
