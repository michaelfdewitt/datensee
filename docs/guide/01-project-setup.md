# Project setup

A one-time configuration of your Google Cloud project. Budget about 15 minutes.
At the end you will run a real export to confirm everything works.

DatensEE submits [Cloud Dataflow](https://cloud.google.com/dataflow) jobs whose
workers fetch tiles from the Earth Engine [High Volume
API](https://developers.google.com/earth-engine/cloud/highvolume) and write
Cloud Optimized GeoTIFFs to Google Cloud Storage (GCS). So the project needs those
three services turned on, an Earth Engine registration, a bucket, and the right
IAM permissions.

> **💰 Running DatensEE costs money.** Cloud exports run on Dataflow, which
> bills for the worker VMs, plus GCS storage and any egress. This holds even when
> your Earth Engine project is registered for noncommercial use: noncommercial
> status waives Earth Engine's own compute charges (EECUs), not the Google Cloud
> orchestration that DatensEE drives. Local exports (`--runner local`) use only
> your own machine and are free. The [case study](../case-study-scale-run.md) itemizes a real bill.
> The High Volume API also caches less than the batch export path, so the same
> computation can use significantly more EECU-time than `Export.image.toCloudStorage`.

## Checklist

- [ ] A Google Cloud project with an active billing account
- [ ] APIs enabled: Earth Engine, Dataflow, Compute Engine, Cloud Storage
- [ ] The project registered for Earth Engine
- [ ] A GCS bucket in the US (where Earth Engine serves from), matched to your Dataflow region
- [ ] IAM roles on you (the submitter) and on the Dataflow worker service account
- [ ] Local credentials (`gcloud auth application-default login`), or Colab

Throughout, replace `YOUR_PROJECT` with your project ID and `YOUR_BUCKET` with
your bucket name.

## 1. Project and billing

Create a project (or reuse one) and attach an active billing account. Local
exports are free, but Dataflow bills for the VMs it runs, so an active billing
account is required before any cloud export.

```bash
gcloud projects create YOUR_PROJECT        # or skip if it exists
gcloud config set project YOUR_PROJECT
# Attach an active billing account in the console: Billing > Link a billing account.
```

## 2. Enable the APIs

DatensEE talks to four Google APIs. Compute Engine is on the list because
Dataflow provisions GCE VMs for its workers.

```bash
gcloud services enable \
  earthengine.googleapis.com \
  dataflow.googleapis.com \
  compute.googleapis.com \
  storage.googleapis.com \
  --project YOUR_PROJECT
```

You do **not** need Artifact Registry or Cloud Build. Those are only for hosting
your own pipeline image; DatensEE uses a public Flex Template by default.

<!-- SCREENSHOT: apis-enabled.png -->
> 📷 **Screenshot:** the "APIs & Services > Enabled APIs" list showing the four
> APIs. Open:
> `https://console.cloud.google.com/apis/dashboard?project=YOUR_PROJECT`

## 3. Register the project for Earth Engine

Earth Engine access is granted per project. Register yours (noncommercial or
commercial) once:

- Visit <https://code.earthengine.google.com/register>.
- Choose your project and the usage type that applies to you.

Until this is done, tile fetches fail with an Earth Engine permission error even
though the API is enabled.

<!-- SCREENSHOT: ee-registration.png -->
> 📷 **Screenshot:** the Earth Engine registration confirmation page for your
> project. Open: <https://code.earthengine.google.com/register>

## 4. Create a GCS bucket (in the US, near Dataflow)

Put the bucket in the **US**. Earth Engine's High Volume API serves from the US,
so a US bucket keeps the EE-to-bucket writes on the same continent. The `US`
multi-region works with any US Dataflow region:

```bash
gcloud storage buckets create gs://YOUR_BUCKET \
  --project YOUR_PROJECT --location US
```

You will point three things at this bucket (or subpaths of it): the export
`--output`, the Dataflow `--temp-location`, and staging. One bucket is fine.

> **Keep the bucket and Dataflow together.** Run Dataflow in a US region
> (`--region-gcp us-central1`, the default) so its workers and the bucket share a
> location. A bucket on one continent with workers on another (say a
> `europe-west6` bucket and `us-central1` workers) still runs, but every tile
> written crosses regions, adding egress cost and latency.

## 5. IAM permissions

[Identity and Access Management (IAM)](https://cloud.google.com/iam/docs/overview)
decides who may do what in your project. Two identities matter: **you** (the account whose credentials submit the job)
and the **Dataflow worker service account** (the identity the workers run as).

By default the worker service account is the Compute Engine default service
account, named `PROJECT_NUMBER-compute@developer.gserviceaccount.com`. Find your
project number with:

```bash
gcloud projects describe YOUR_PROJECT --format='value(projectNumber)'
```

**You (the submitter)** need to create jobs and act as the worker SA:

```bash
ME=user:you@example.com          # your account
WORKER=PROJECT_NUMBER-compute@developer.gserviceaccount.com

gcloud projects add-iam-policy-binding YOUR_PROJECT \
  --member="$ME" --role="roles/dataflow.developer"
gcloud iam service-accounts add-iam-policy-binding "$WORKER" \
  --member="$ME" --role="roles/iam.serviceAccountUser" --project YOUR_PROJECT
```

- [`roles/dataflow.developer`](https://cloud.google.com/iam/docs/understanding-roles#dataflow.developer): create and manage
  Dataflow jobs.
- [`roles/iam.serviceAccountUser`](https://cloud.google.com/iam/docs/understanding-roles#iam.serviceAccountUser): act as the
  worker service account when launching the job.

**The worker service account** needs to run Dataflow and read/write your bucket:

```bash
gcloud projects add-iam-policy-binding YOUR_PROJECT \
  --member="serviceAccount:$WORKER" --role="roles/dataflow.worker"
gcloud storage buckets add-iam-policy-binding gs://YOUR_BUCKET \
  --member="serviceAccount:$WORKER" --role="roles/storage.objectAdmin"
```

- [`roles/dataflow.worker`](https://cloud.google.com/iam/docs/understanding-roles#dataflow.worker): run as a Dataflow worker.
- [`roles/storage.objectAdmin`](https://cloud.google.com/iam/docs/understanding-roles#storage.objectAdmin): read and write
  objects in your bucket.

The workers reach Earth Engine as this service account, so the project
registration from step 3 is what grants their EE access. (Advanced: to fetch as
a different identity, `datensee export` accepts a service account to impersonate;
see [Debugging](03-debugging.md) if EE auth fails on workers.)

<!-- SCREENSHOT: iam-roles.png -->
> 📷 **Screenshot:** the IAM page filtered to the worker service account,
> showing Dataflow Worker + Storage Object Admin. Open:
> `https://console.cloud.google.com/iam-admin/iam?project=YOUR_PROJECT`

## 6. Authenticate

**Local CLI.** DatensEE uses Application Default Credentials. Log in once:

```bash
gcloud auth application-default login
```

This is the only auth the local CLI needs; the pipeline JVM inherits the same
credentials. If your organization requires a quota project on user credentials,
also run `gcloud auth application-default set-quota-project YOUR_PROJECT`.

**Colab / Jupyter.** No `gcloud` needed. `notebook.ensure_auth()` triggers the
Colab OAuth flow and exports credentials for the JVM:

```python
from datensee import notebook
notebook.ensure_auth()
```

## 7. Verify

Install DatensEE and run the local demo (no Dataflow, no bucket, fast):

```bash
pip install datensee
datensee demo --project YOUR_PROJECT --output ./setup-check
```

A healthy run prints an export summary, fetches 9 tiles, and writes COGs to
`./setup-check`. If it fails here, the problem is auth or EE registration, not
Dataflow; see [Debugging](03-debugging.md).

Then confirm the cloud path with a tiny Dataflow export:

```bash
datensee demo --project YOUR_PROJECT \
  --output gs://YOUR_BUCKET/setup-check \
  --runner dataflow --temp-location gs://YOUR_BUCKET/tmp --region-gcp us-central1
```

Expect about two minutes of startup before any tile is written; that is normal
Dataflow provisioning. When the job reaches `JOB_STATE_DONE`, setup is complete.
Watch it with `datensee status`; if anything fails, see [Debugging](03-debugging.md).
For more exports, see the [Cookbook](04-cookbook.md).

## Common setup failures

| Symptom | Cause | Fix |
|---|---|---|
| `PERMISSION_DENIED` on Earth Engine | Project not registered for EE | Step 3 |
| Dataflow job fails instantly at launch | Dataflow/Compute API off, or no billing | Steps 1, 2 |
| `does not have storage.objects.create` | Worker SA lacks bucket access | Step 5 |
| Job submits but no workers start | Missing `roles/dataflow.worker` or a quota/stockout | Step 5, or [Debugging](03-debugging.md) |
| `Your default credentials were not found` | Not logged in | Step 6 |

See [Debugging](03-debugging.md) for reading the actual error and recovering.
