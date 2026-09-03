package com.datensee.pixel.fetch;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.datensee.fetch.EeApiException;
import com.datensee.fetch.EeAuthRemediation;
import com.datensee.fetch.EeErrorKind;
import com.datensee.pixel.AffineTransform;
import com.datensee.pixel.FailedTileRecord;
import com.datensee.pixel.FetchedTile;
import com.datensee.pixel.PixelGrid;
import com.datensee.pixel.TileCoordinate;
import com.google.auth.oauth2.GoogleCredentials;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
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
 * <p>Rate shaping: throughput is bounded by worker concurrency (worker count
 * × harness threads × HTTP client concurrency). When Earth Engine quota is
 * reached, the API returns HTTP 429 and the retry path applies exponential
 * backoff with jitter.
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
    public static final TupleTag<FailedTileRecord> FAILED_TAG = new TupleTag<>() { };

    private static final Logger LOG = LoggerFactory.getLogger(TileFetchDoFn.class);
    private static final String HV_ENDPOINT =
        "https://earthengine-highvolume.googleapis.com/v1/projects/%s/image:computePixels";
    private static final String EE_SCOPE = "https://www.googleapis.com/auth/earthengine";

    // Retry budget configured for bounded latency under sustained 429 backpressure.
    //
    // Extended sleep times risk blocking Beam worker harness threads, which can
    // degrade autoscaling metrics. To prevent thread starvation, per-attempt
    // backoff sleeps and total wall-clock time per tile are bounded. Failing
    // tiles dead-letter into the failures journal, allowing recovery via
    // `datensee retry`.
    //
    // Backoff sequence (capped at 10s): 1s, 2s, 4s, 8s, 10s, 10s.
    // Cumulative max sleep = 35s. HTTP_REQUEST_TIMEOUT is set to 90s to
    // accommodate Earth Engine queueing under contention. MAX_PER_TILE_BUDGET
    // (180s) caps total wall-clock duration per tile.
    private static final int MAX_RETRIES = 6;
    private static final Duration BACKOFF_429 = Duration.ofSeconds(1);
    private static final Duration BACKOFF_503 = Duration.ofSeconds(2);
    private static final Duration BACKOFF_DEFAULT = Duration.ofSeconds(1);
    private static final Duration BACKOFF_CAP = Duration.ofSeconds(10);
    private static final Duration MAX_PER_TILE_BUDGET = Duration.ofSeconds(180);
    private static final Duration HTTP_REQUEST_TIMEOUT = Duration.ofSeconds(90);
    private static final Duration HTTP_CONNECT_TIMEOUT = Duration.ofSeconds(15);

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final String eeExpression;
    private final String geeProject;
    private final PixelGrid parentGrid;

    // Transient: not serialized by Beam; recreated on each worker in @Setup.
    private transient HttpClient httpClient;
    private transient GoogleCredentials credentials;

    // Captured once per worker for AUTH_ERROR remediation messaging. Null
    // when the metadata server is unavailable (e.g. Direct runner).
    private transient String workerServiceAccount;

    // Limits the multi-line remediation message to one emission per DoFn
    // instance; subsequent AUTH_ERRORs log a single line.
    private transient boolean firstAuthLogged;

    // Logs the first HV request body in full at INFO for inspection.
    // Subsequent requests log only the per-tile grid at DEBUG.
    private transient boolean firstRequestLogged;

    /**
     * @param eeExpression serialized EE computation (opaque JSON)
     * @param geeProject   GCP project ID for HV API
     * @param parentGrid   parent {@link PixelGrid} for the export (tile grids
     *                     are derived by translating the parent affine)
     */
    public TileFetchDoFn(
        String eeExpression,
        String geeProject,
        PixelGrid parentGrid
    ) {
        this.eeExpression = eeExpression;
        this.geeProject = geeProject;
        this.parentGrid = parentGrid;
    }

    @Setup
    public void setup() throws IOException {
        // Build the source credential from worker ADC. On Dataflow
        // this resolves ComputeEngineCredentials; on local DirectRunner
        // it uses application default credentials.
        GoogleCredentials sourceCreds = GoogleCredentials.getApplicationDefault();
        if (sourceCreds.createScopedRequired()) {
            sourceCreds = sourceCreds.createScoped(Collections.singleton(EE_SCOPE));
        }

        credentials = sourceCreds;

        httpClient = HttpClient.newBuilder()
            .connectTimeout(HTTP_CONNECT_TIMEOUT)
            .build();
        workerServiceAccount = EeAuthRemediation.discoverWorkerServiceAccount(httpClient);
        firstAuthLogged = false;
        firstRequestLogged = false;
        LOG.info(
            "Worker setup: project={}, workerSa={}, credType={}",
            geeProject, workerServiceAccount,
            credentials.getClass().getSimpleName()
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
                new FetchedTile(tile, imageBytes, tile.widthPx(), tile.heightPx())
            );
        } catch (IOException | InterruptedException e) {
            FailedTileRecord record = classifyFailure(
                tile, e, workerServiceAccount, geeProject
            );
            if (record.errorKind() == EeErrorKind.AUTH_ERROR && !firstAuthLogged) {
                LOG.error(
                    "{}: AUTH_ERROR on EE HV API.{}",
                    tile.id(),
                    EeAuthRemediation.formatLogMessage(workerServiceAccount, geeProject)
                );
                firstAuthLogged = true;
            } else {
                LOG.error("{}: dead-lettered after all retries: {}", tile.id(), e.getMessage());
            }
            out.get(FAILED_TAG).output(record);
        }
    }

    /**
     * Build a {@link FailedTileRecord} for the dead-letter
     * stream. Walks the cause chain to find the underlying
     * {@link EeApiException} (the retry loop wraps it in a generic
     * {@code IOException}), classifies its (status, body) pair into an
     * {@link EeErrorKind}, and stamps the record with {@code attempts =
     * MAX_RETRIES}. When the failure isn't an {@code EeApiException}
     * (network error, interrupt, etc.) we record kind=UNKNOWN with the
     * exception message.
     *
     * <p>For {@code AUTH_ERROR} the {@code error_message} field is
     * rewritten to lead with a remediation hint naming the worker
     * service account and the gcloud command that fixes it. The original
     * EE body is preserved at the tail so debuggers don't lose context.
     */
    static FailedTileRecord classifyFailure(
        TileCoordinate tile,
        Exception e,
        String workerServiceAccount,
        String geeProject
    ) {
        Throwable cause = e;
        while (cause != null && !(cause instanceof EeApiException)) {
            cause = cause.getCause();
        }
        if (cause instanceof EeApiException ee) {
            EeErrorKind kind = EeErrorKind.classify(ee.httpStatus(), ee.truncatedBody());
            String message = kind == EeErrorKind.AUTH_ERROR
                ? EeAuthRemediation.formatJournalMessage(
                    workerServiceAccount, geeProject, ee.truncatedBody()
                )
                : ee.truncatedBody();
            return FailedTileRecord.fromTileWithError(
                tile,
                kind,
                message,
                ee.httpStatus(),
                MAX_RETRIES
            );
        }
        return FailedTileRecord.fromTileWithError(
            tile,
            EeErrorKind.UNKNOWN,
            e.getMessage(),
            null,
            MAX_RETRIES
        );
    }

    private byte[] fetchWithRetry(TileCoordinate tile)
        throws IOException, InterruptedException {
        IOException lastException = null;
        long deadlineMs = System.currentTimeMillis() + MAX_PER_TILE_BUDGET.toMillis();

        for (int attempt = 1; attempt <= MAX_RETRIES; attempt++) {
            // Wall-clock budget short-circuit. Without this a tile stuck on
            // sustained 429s could pile up minutes of cumulative backoff
            // sleep, holding a worker harness thread idle while the
            // autoscaler interpreted near-zero throughput as "scale down".
            // Dead-lettering after the budget gives the worker a chance to
            // move on; the user reruns via `datensee retry --journal` to
            // recover.
            if (System.currentTimeMillis() >= deadlineMs) {
                throw new IOException(
                    String.format(
                        "Per-tile budget %ds exceeded for %s",
                        MAX_PER_TILE_BUDGET.toSeconds(), tile.id()
                    ),
                    lastException
                );
            }
            try {
                return fetchTile(tile);
            } catch (EeApiException e) {
                if (!e.isRetryable()) {
                    throw e;
                }
                lastException = e;
                long backoffMs = capBackoff(initialBackoff(e.httpStatus()), attempt);
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
                long backoffMs = capBackoff(BACKOFF_DEFAULT, attempt);
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

    /**
     * Exponential backoff with a per-sleep cap. Capping backoff limits tail
     * sleep latency across concurrent failing tiles.
     */
    private static long capBackoff(Duration base, int attempt) {
        long ms = base.toMillis() * (1L << (attempt - 1));
        return Math.min(ms, BACKOFF_CAP.toMillis());
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
            .POST(HttpRequest.BodyPublishers.ofString(requestBody, StandardCharsets.UTF_8))
            .timeout(HTTP_REQUEST_TIMEOUT)
            .build();

        HttpResponse<byte[]> response = httpClient.send(
            request,
            HttpResponse.BodyHandlers.ofByteArray()
        );

        int status = response.statusCode();
        if (status != 200) {
            String body = new String(response.body());
            // Log response headers on non-200 to inspect rate limit, quota,
            // or timing details.
            String hdrs = response.headers().map().entrySet().stream()
                .map(e -> e.getKey() + "=" + String.join(",", e.getValue()))
                .collect(java.util.stream.Collectors.joining(" | "));
            LOG.warn(
                "{}: HTTP {} response headers: {}",
                tile.id(), status, hdrs
            );
            throw new EeApiException(status, tile.id(), body);
        }

        LOG.debug("{}: fetched {} bytes", tile.id(), response.body().length);
        return response.body();
    }

    /**
     * Build the EE computePixels JSON request body using Jackson.
     *
     * <p>The {@code eeExpression} is a JSON string containing the serialized
     * EE computation graph, parsed into a {@link JsonNode} and embedded
     * as the {@code expression} object.
     *
     * <p>The per-tile grid is derived from the parent {@link PixelGrid} by
     * translating its affine to the tile's NW corner.
     *
     * <p>Logging: the first request per worker logs the full body at
     * INFO. Subsequent requests log the per-tile grid at DEBUG.
     */
    private String buildRequestBody(TileCoordinate tile) throws IOException {
        JsonNode expressionNode = MAPPER.readTree(eeExpression);

        PixelGrid tileGrid = parentGrid.forTile(tile);
        AffineTransform a = tileGrid.affineTransform();

        ObjectNode affine = MAPPER.createObjectNode();
        affine.put("scaleX", a.scaleX());
        affine.put("shearX", a.shearX());
        affine.put("translateX", a.translateX());
        affine.put("shearY", a.shearY());
        affine.put("scaleY", a.scaleY());
        affine.put("translateY", a.translateY());

        ObjectNode dimensions = MAPPER.createObjectNode();
        dimensions.put("width", tileGrid.dimensions().width());
        dimensions.put("height", tileGrid.dimensions().height());

        ObjectNode grid = MAPPER.createObjectNode();
        grid.set("dimensions", dimensions);
        grid.set("affineTransform", affine);
        grid.put("crsCode", tileGrid.crsCode());

        ObjectNode requestNode = MAPPER.createObjectNode();
        requestNode.set("expression", expressionNode);
        requestNode.put("fileFormat", "GEO_TIFF");
        requestNode.set("grid", grid);

        String body = MAPPER.writeValueAsString(requestNode);

        // Operator-facing wire-format sanity check. Once per worker at
        // INFO + the full body; per-tile grid at DEBUG forever after.
        if (!firstRequestLogged) {
            LOG.info(
                "{}: first HV request body (subsequent requests log just"
                + " the per-tile grid at DEBUG): {}",
                tile.id(), body
            );
            firstRequestLogged = true;
        } else if (LOG.isDebugEnabled()) {
            LOG.debug("{}: HV request grid = {}", tile.id(), grid);
        }

        return body;
    }
}
