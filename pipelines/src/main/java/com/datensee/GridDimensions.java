package com.datensee;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import java.io.Serializable;

/** Pixel dimensions of a grid (mirrors EE's {@code GridDimensions}). */
@JsonIgnoreProperties(ignoreUnknown = true)
public record GridDimensions(int width, int height) implements Serializable { }
