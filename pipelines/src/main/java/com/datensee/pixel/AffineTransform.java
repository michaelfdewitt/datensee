package com.datensee.pixel;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.io.Serializable;

/**
 * 6-tuple geo-affine in the EE / GDAL / GeoTIFF convention.
 *
 * <p>For axis-aligned grids (always, in our use), {@code shearX} and
 * {@code shearY} are zero, {@code scaleX} is positive, and {@code scaleY}
 * is <strong>negative</strong> — that puts {@code (translateX, translateY)}
 * at the NW corner of the top-left pixel (PixelIsArea: pixel {@code (u=0,
 * v=0)} is the corner, not the centre).
 *
 * <p>Sent verbatim into the EE {@code computePixels} request body as the
 * {@code grid.affineTransform} object — the field names match.
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public record AffineTransform(
    @JsonProperty("scale_x") double scaleX,
    @JsonProperty("shear_x") double shearX,
    @JsonProperty("translate_x") double translateX,
    @JsonProperty("shear_y") double shearY,
    @JsonProperty("scale_y") double scaleY,
    @JsonProperty("translate_y") double translateY
) implements Serializable { }
