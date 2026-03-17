package com.datensee.fetch;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.datensee.FetchedTile;
import com.datensee.TileCoordinate;
import com.google.auth.oauth2.GoogleCredentials;
import com.google.common.util.concurrent.RateLimiter;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.Collections;
import org.apache.beam.sdk.transforms.DoFn;
import org.apache.beam.sdk.values.TupleTag;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * DoFn that fetches a single tile from the EE High Volume API.
 *
 * <p>Auth: uses Application Default Credentials with the Earth Engine scope.
 * Credentials are initialized once per worker in {@code @Setup} and refreshed
 * as needed before each request.
 *
 * <p>Rate limiting: each worker uses a Guava {@link RateLimiter} configured
 * as {@code maxQps / maxWorkers}. The existing 429 backoff handles overflow
 * if the estimate is too aggressive.
 *
 * <p>Error classification: 429/503/5xx are retried with exponential backoff;
 * 400/403/404 are dead-lettered immediately. See {@link EeApiException}.
 *
 * <p>Partial failure: after all retries are exhausted, failed tiles are emitted
 * to {@link #FAILED_TAG} instead of crashing the pipeline.
 */
public final class TileFetchDoFn extends DoFn<TileCoordinate, FetchedTile> {

    /** Tag for successfully fetched tiles. */
    public static final TupleTag<FetchedTile> SUCCESS_TAG = new TupleTag<>() { };

    /** Tag for tiles that failed all retries (dead-letter). */
    public static final TupleTag<TileCoordinate> FAILED_TAG = new TupleTag<>() { };

    private static final Logger LOG = LoggerFactory.getLogger(TileFetchDoFn.class);
    private static final String HV_ENDPOINT =
        "https://earthengine-highvolume.googleapis.com/v1/projects/%s/image:computePixels";
    private static final String EE_SCOPE = "https://www.googleapis.com/auth/earthengine";
    private static final int MAX_RETRIES = 5;
    private static final Duration BACKOFF_429 = Duration.ofSeconds(1);
    private static final Duration BACKOFF_503 = Duration.ofSeconds(5);
    private static final Duration BACKOFF_DEFAULT = Duration.ofSeconds(2);

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final String eeExpression;
    private final String geeProject;
    private final int tileSizePixels;
    private final String crs;
    private final double perWorkerQps;

    // Transient: not serialized by Beam; recreated on each worker in @Setup.
    private transient HttpClient httpClient;
    private transient GoogleCredentials credentials;
    private transient RateLimiter rateLimiter;

    /**
     * @param eeExpression serialized EE computation (opaque JSON)
     * @param geeProject   GCP project ID for HV API
     * @param tileSizePixels tile edge size in pixels
     * @param crs          target CRS code
     * @param maxQps       project-wide QPS cap for the HV API
     * @param maxWorkers   expected number of concurrent workers
     */
    public TileFetchDoFn(
        String eeExpression,
        String geeProject,
        int tileSizePixels,
        String crs,
        double maxQps,
        int maxWorkers
    ) {
        this.eeExpression = eeExpression;
        this.geeProject = geeProject;
        this.tileSizePixels = tileSizePixels;
        this.crs = crs;
        this.perWorkerQps = Math.max(1.0, maxQps / Math.max(1, maxWorkers));
    }

    @Setup
    public void setup() throws IOException {
        credentials = GoogleCredentials.getApplicationDefault()
            .createScoped(Collections.singleton(EE_SCOPE));
        httpClient = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(30))
            .build();
        rateLimiter = RateLimiter.create(perWorkerQps);
        LOG.info(
            "Worker setup: project={}, rate={} qps/worker",
            geeProject, perWorkerQps
        );
    }

    @ProcessElement
    public void processElement(
        @Element TileCoordinate tile,
        MultiOutputReceiver out
    ) {
        try {
            byte[] imageBytes = fetchWithRetry(tile);
            out.get(SUCCESS_TAG).output(
                new FetchedTile(tile, imageBytes, tileSizePixels, tileSizePixels)
            );
        } catch (IOException | InterruptedException e) {
            LOG.error("{}: dead-lettered after all retries: {}", tile.id(), e.getMessage());
            out.get(FAILED_TAG).output(tile);
        }
    }

    private byte[] fetchWithRetry(TileCoordinate tile)
        throws IOException, InterruptedException {
        IOException lastException = null;

        for (int attempt = 1; attempt <= MAX_RETRIES; attempt++) {
            try {
                rateLimiter.acquire();
                return fetchTile(tile);
            } catch (EeApiException e) {
                if (!e.isRetryable()) {
                    throw e;
                }
                lastException = e;
                Duration backoff = initialBackoff(e.httpStatus());
                long backoffMs = backoff.toMillis() * (1L << (attempt - 1));
                long jitter = (long) (Math.random() * backoffMs * 0.2);
                LOG.warn(
                    "{}: attempt {}/{} failed (HTTP {}), retrying in {}ms",
                    tile.id(), attempt, MAX_RETRIES, e.httpStatus(), backoffMs + jitter
                );
                if (attempt < MAX_RETRIES) {
                    Thread.sleep(backoffMs + jitter);
                }
            } catch (IOException e) {
                lastException = e;
                long backoffMs = BACKOFF_DEFAULT.toMillis() * (1L << (attempt - 1));
                long jitter = (long) (Math.random() * backoffMs * 0.2);
                LOG.warn(
                    "{}: attempt {}/{} failed: {}, retrying in {}ms",
                    tile.id(), attempt, MAX_RETRIES, e.getMessage(), backoffMs + jitter
                );
                if (attempt < MAX_RETRIES) {
                    Thread.sleep(backoffMs + jitter);
                }
            }
        }

        throw new IOException(
            String.format("All %d fetch attempts failed for %s", MAX_RETRIES, tile.id()),
            lastException
        );
    }

    private static Duration initialBackoff(int httpStatus) {
        return switch (httpStatus) {
            case 429 -> BACKOFF_429;
            case 503 -> BACKOFF_503;
            default -> BACKOFF_DEFAULT;
        };
    }

    private byte[] fetchTile(TileCoordinate tile) throws IOException, InterruptedException {
        credentials.refreshIfExpired();
        String token = credentials.getAccessToken().getTokenValue();
        String requestBody = buildRequestBody(tile);

        HttpRequest request = HttpRequest.newBuilder()
            .uri(URI.create(String.format(HV_ENDPOINT, geeProject)))
            .header("Content-Type", "application/json")
            .header("Authorization", "Bearer " + token)
            .header("x-goog-user-project", geeProject)
            .POST(HttpRequest.BodyPublishers.ofString(requestBody))
            .timeout(Duration.ofSeconds(120))
            .build();

        HttpResponse<byte[]> response = httpClient.send(
            request,
            HttpResponse.BodyHandlers.ofByteArray()
        );

        int status = response.statusCode();
        if (status != 200) {
            String body = new String(response.body());
            throw new EeApiException(status, tile.id(), body);
        }

        LOG.debug("{}: fetched {} bytes", tile.id(), response.body().length);
        return response.body();
    }

    /**
     * Build the EE computePixels JSON request body using Jackson.
     *
     * <p>The {@code eeExpression} is a JSON string containing the serialized
     * EE computation graph. We parse it into a {@link JsonNode} and embed it
     * as the {@code expression} field — the HV API expects a JSON object there,
     * not a quoted string.
     */
    private String buildRequestBody(TileCoordinate tile) throws IOException {
        JsonNode expressionNode = MAPPER.readTree(eeExpression);

        double pixelWidth = (tile.xMax() - tile.xMin()) / tileSizePixels;
        double pixelHeight = (tile.yMax() - tile.yMin()) / tileSizePixels;

        ObjectNode affine = MAPPER.createObjectNode();
        affine.put("scaleX", pixelWidth);
        affine.put("shearX", 0.0);
        affine.put("translateX", tile.xMin());
        affine.put("shearY", 0.0);
        affine.put("scaleY", -pixelHeight);
        affine.put("translateY", tile.yMax());

        ObjectNode dimensions = MAPPER.createObjectNode();
        dimensions.put("width", tileSizePixels);
        dimensions.put("height", tileSizePixels);

        ObjectNode grid = MAPPER.createObjectNode();
        grid.set("dimensions", dimensions);
        grid.set("affineTransform", affine);
        grid.put("crsCode", crs);

        ObjectNode requestNode = MAPPER.createObjectNode();
        requestNode.set("expression", expressionNode);
        requestNode.put("fileFormat", "GEO_TIFF");
        requestNode.set("grid", grid);

        return MAPPER.writeValueAsString(requestNode);
    }
}
