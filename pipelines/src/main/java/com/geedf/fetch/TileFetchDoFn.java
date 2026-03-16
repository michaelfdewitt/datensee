package com.geedf.fetch;

import com.geedf.FetchedTile;
import com.geedf.TileCoordinate;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import org.apache.beam.sdk.transforms.DoFn;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * DoFn that fetches a single tile from the EE High Volume API.
 *
 * <p>Auth: expects Application Default Credentials to be available on the worker.
 * Rate limiting: each worker self-limits via a simple sleep-based backoff.
 * TODO: Replace per-worker rate limiting with coordinated global rate limiting
 * (Beam state or Memorystore token bucket) once the basic flow works.
 */
public final class TileFetchDoFn extends DoFn<TileCoordinate, FetchedTile> {

    private static final Logger LOG = LoggerFactory.getLogger(TileFetchDoFn.class);
    private static final String HV_ENDPOINT =
        "https://earthengine-highvolume.googleapis.com/v1/projects/{project}/image:computePixels";
    private static final int MAX_RETRIES = 5;
    private static final Duration INITIAL_BACKOFF = Duration.ofSeconds(1);

    private final String eeExpression;

    // Transient so Beam can serialize this DoFn without capturing the HttpClient.
    private transient HttpClient httpClient;

    public TileFetchDoFn(String eeExpression) {
        this.eeExpression = eeExpression;
    }

    @Setup
    public void setup() {
        httpClient = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(30))
            .build();
    }

    @ProcessElement
    public void processElement(
        @Element TileCoordinate tile,
        OutputReceiver<FetchedTile> out
    ) throws IOException, InterruptedException {
        byte[] imageBytes = fetchWithRetry(tile);
        // TODO: parse width/height from GeoTIFF header instead of hardcoding.
        out.output(new FetchedTile(tile, imageBytes, 512, 512));
    }

    private byte[] fetchWithRetry(TileCoordinate tile)
        throws IOException, InterruptedException {
        Duration backoff = INITIAL_BACKOFF;
        IOException lastException = null;

        for (int attempt = 1; attempt <= MAX_RETRIES; attempt++) {
            try {
                return fetchTile(tile);
            } catch (IOException e) {
                lastException = e;
                LOG.warn("{}: fetch attempt {}/{} failed: {}", tile.id(), attempt, MAX_RETRIES, e.getMessage());
                if (attempt < MAX_RETRIES) {
                    Thread.sleep(backoff.toMillis());
                    backoff = backoff.multipliedBy(2);
                }
            }
        }

        throw new IOException(
            String.format("All %d fetch attempts failed for %s", MAX_RETRIES, tile.id()),
            lastException
        );
    }

    private byte[] fetchTile(TileCoordinate tile) throws IOException, InterruptedException {
        // TODO: Replace placeholder with real EE HV API request body.
        // See: https://developers.google.com/earth-engine/reference/rest/v1/projects.image/computePixels
        String requestBody = buildRequestBody(tile);

        HttpRequest request = HttpRequest.newBuilder()
            .uri(URI.create(HV_ENDPOINT.replace("{project}", "earthengine-public")))
            .header("Content-Type", "application/json")
            // TODO: Add Authorization header using ADC token.
            .POST(HttpRequest.BodyPublishers.ofString(requestBody))
            .timeout(Duration.ofSeconds(120))
            .build();

        HttpResponse<byte[]> response = httpClient.send(
            request,
            HttpResponse.BodyHandlers.ofByteArray()
        );

        if (response.statusCode() != 200) {
            throw new IOException(
                String.format("EE HV API returned HTTP %d for %s", response.statusCode(), tile.id())
            );
        }

        return response.body();
    }

    private String buildRequestBody(TileCoordinate tile) {
        // TODO: Build proper EE computePixels request body.
        // The eeExpression is passed as-is; EE evaluates it bounded to this tile.
        return String.format(
            """
            {
              "expression": %s,
              "fileFormat": "GEO_TIFF",
              "bandIds": [],
              "grid": {
                "dimensions": { "width": 512, "height": 512 },
                "affineTransform": {
                  "scaleX": %f,
                  "shearX": 0,
                  "translateX": %f,
                  "shearY": 0,
                  "scaleY": %f,
                  "translateY": %f
                },
                "crsCode": "EPSG:4326"
              }
            }
            """,
            eeExpression,
            (tile.xMax() - tile.xMin()) / 512.0,
            tile.xMin(),
            -(tile.yMax() - tile.yMin()) / 512.0,
            tile.yMax()
        );
    }
}
