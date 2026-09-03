# Debugging

Exports fail. At scale, some tiles failing is normal and recoverable; a whole
job failing usually means one fixable thing. This page is a map: read the
reason, find the right logs, match the error, apply the fix.

Most of what follows comes from real failures, not hypotheticals.

## Step 1: read the failure reason

When a Dataflow job fails, `datensee status` prints why. Always start here:

```bash
datensee status JOB_ID --project YOUR_PROJECT --region-gcp us-central1
```

On a failed job the status table ends with one or more **Reason** rows, lifted
from the Dataflow job messages:

```
Job           2026-09-02_05_43_59-...
State         JOB_STATE_FAILED
Reason        Dataflow: Startup of the worker pool in us-central1 failed to
              bring up any of the desired 4 workers. ... ZONE_RESOURCE_POOL_
              EXHAUSTED: ... does not have enough resources available ...
Reason        Dataflow: Workflow failed.
```

That is usually enough to identify the problem. If the failure happened before
any worker ran (a launcher failure), the last Reason row points you at the
launcher log in GCS (see below).

## Step 2: know which stage failed

A DatensEE job passes through three stages, and each keeps its errors in a
different place.

```
  submit (Python)          launcher (container)          workers (Dataflow)
  validate + tile          parse config, build graph     fetch tiles, write COGs
  |                        |                             |
  errors: your terminal    errors: console_logs in GCS   errors: Dataflow console
                           (NOT Cloud Logging)           + Cloud Logging
```

- **Submit** runs on your machine. Validation errors (bad region, missing
  `--temp-location`, unreadable expression) print in your terminal immediately,
  before any cloud resource is created.
- **Launcher** is a short-lived VM that parses your config and builds the Beam
  graph. Its stack traces are written to
  `gs://YOUR_TEMP/staging/template_launches/JOB_ID/console_logs`, **not** Cloud
  Logging. `datensee status` prints the exact `gcloud storage cat` command for
  this path when it detects a launcher failure.
- **Workers** do the actual fetching. Their logs are in the Dataflow console and
  Cloud Logging.

## Step 3: find the logs

| Where | What is there | How to open |
|---|---|---|
| Your terminal | Submit-time validation, the failure Reason | (already printed) |
| Dataflow console | Job graph, worker counts, worker logs | `https://console.cloud.google.com/dataflow/jobs/us-central1/JOB_ID?project=YOUR_PROJECT` |
| Launcher log (GCS) | Launcher stack trace | `gcloud storage cat gs://YOUR_TEMP/staging/template_launches/JOB_ID/console_logs` |
| `_failures.json` | Per-tile failures (dead-letter journal) | `gcloud storage cat gs://YOUR_OUTPUT/_failures.json` |
| Cloud Logging | Full worker logs, queryable | Console > Logging, filter `resource.labels.job_id="JOB_ID"` |

<!-- SCREENSHOT: dataflow-console-graph.png -->
> 📷 **Screenshot:** the Dataflow job page showing the graph and the worker-count
> panel over time. Open the job URL above.

<!-- SCREENSHOT: dataflow-logs-panel.png -->
> 📷 **Screenshot:** the Logs panel on the job page, with the severity filter set
> to Error. Same page, bottom.

## Common errors and fixes

### `ZONE_RESOURCE_POOL_EXHAUSTED` (no workers start)

Google Cloud is out of your requested VM type in that zone. The job retries for
a few minutes, then fails with "failed to bring up any of the desired N
workers." This is capacity, not your code.

**Fix:** switch machine family or region.

```bash
# try a different family
datensee export ... --machine-type e2-standard-4
# or a different region (keep your bucket in the US either way)
datensee export ... --region-gcp us-east1
```

We hit this repeatedly on `n2-standard-4` in `us-central1`; `e2-standard-4` or
`us-east1` cleared it every time.

### Earth Engine `PERMISSION_DENIED`

Tiles fail with an Earth Engine permission error even though the API is enabled.
The project is not registered for Earth Engine, or the workers are running as an
identity without EE access.

**Fix:** register the project (setup [step 3](01-project-setup.md)). If you fetch
as a custom service account, make sure that account has EE access.

### Storage `403` / `does not have storage.objects.create`

The Dataflow worker service account cannot write your bucket.

**Fix:** grant it `roles/storage.objectAdmin` on the bucket (setup
[step 5](01-project-setup.md)).

### `Your default credentials were not found`

No Application Default Credentials on the submitting machine.

**Fix:** `gcloud auth application-default login` (setup
[step 6](01-project-setup.md)). In Colab, call `notebook.ensure_auth()`.

### Launcher fails immediately (`UnrecognizedPropertyException` and similar)

The launcher could not parse the config. For end users on the pinned template
this is rare; it usually means a CLI and template version mismatch.

**Fix:** upgrade to the latest `datensee` (`pip install -U datensee`), which
pins itself to a matching template. Read the launcher log for the exact parse
error (`datensee status` prints the `gcloud storage cat` line).

### Local runner: `java` not found or too old

The local runner needs a Java 21+ runtime. DatensEE checks before launching and
says so.

**Fix:** install a JDK 21 or newer (for example Temurin), or use
`--runner dataflow`, which needs no local Java.

### Quota exceeded

Dataflow or Compute Engine quota (CPUs, in-use IP addresses, disk) caps how many
workers can start. The Reason names the quota.

**Fix:** lower `--max-workers`, or request a quota increase in the console
(IAM & Admin > Quotas).

## Partial failure: the journal and retry

When a job finishes but some tiles failed, DatensEE does not fail the whole run.
Every failed fetch or write is dead-lettered into `_failures.json` in the output
prefix, with an error kind per record. The output is otherwise complete.

Recover the missing tiles in place:

```bash
datensee retry --output gs://YOUR_OUTPUT \
  --runner dataflow --region-gcp us-central1 \
  --temp-location gs://YOUR_BUCKET/tmp --until-done
```

`retry` reads `_failures.json`, decides what to do per record (split tiles Earth
Engine rejected as too expensive into quadrants, re-fetch transient failures as
is, carry over terminal ones), and re-runs. `--until-done` loops rounds until the
journal is clear or nothing more can be recovered. Retried tiles overlay the
existing COGs exactly, so the output is filled in place, not rebuilt.

If a retry round cannot make progress (for example an authentication error that
splitting will never fix), it stops and tells you, rather than looping forever.

## Validating output

To confirm an export is complete and correct:

```bash
datensee validate gs://YOUR_OUTPUT
```

The integrity check (free) confirms every expected COG exists, or is accounted
for in `_failures.json`, with the right dimensions, CRS, and origin. Add
`--pixels` to also re-fetch a sample of tiles from Earth Engine and compare band
values, which verifies the whole chain at the cost of a few EECUs.

Validation catches problems the pipeline cannot see itself. On the scale run in
the [case study](../case-study-scale-run.md), `validate` flagged a config that
declared `float32` for int16 SRTM data, all 21,316 files at once, before anyone
opened the output in a GIS tool.

## Getting help

If you are stuck, gather: the `datensee status` output, the Dataflow console URL,
and the relevant log (launcher `console_logs` for launch failures, or the
Cloud Logging worker errors otherwise). A minimal reproduction, ideally the
`datensee demo` command against your project, tells the difference between a
setup problem and a bug fast.
