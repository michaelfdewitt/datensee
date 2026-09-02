# DatensEE: massively parallel exports, without leaving Earth Engine

Earth Engine is one of the most remarkable systems ever built for
planetary-scale analysis. The catalog, the computation engine, the
expression model — there is genuinely nothing else like it. DatensEE
exists to make one specific part of the workflow — pulling large
results out to GCS — fit better into how modern cloud pipelines are
shaped.

DatensEE is a small Python package — `pip install datensee` — that takes
the computation you already wrote and runs it across thousands of Cloud
Dataflow workers. You don't change your expression. You don't move to a
different platform. You don't rewrite anything. You hand DatensEE the
expression, the region, and the scale, and you get a directory of Cloud
Optimized GeoTIFFs on GCS.

## A product of its era

`Export.image.toCloudStorage` was designed before the Cambrian explosion
of cheap, parallel, batch-friendly cloud tooling. Dataflow autoscaling,
spot workers, COG as a first-class output format, the High Volume API
itself — none of these existed when the batch export system was first
shaped. It's a single, opaque, server-side job: Earth Engine picks the
pace, manages retries internally, and reports success or failure as a
unit. For the workloads it was originally built for, that design is
perfectly reasonable.

The shape of the workload has shifted, though. A 10 m NDVI export over
the continental US, run a dozen times across different date windows, is
now a routine task. At that scale, the historically available paths are:

1. Subdivide the study area by hand into N regions, kick off N exports,
   monitor each one, and stitch the outputs.
2. Step outside Earth Engine and reimplement the analysis on a stack
   where the user controls the parallelism directly.

Both work, but each has costs. Option 1 takes meaningful operator time
and doesn't recover gracefully from per-tile failures. Option 2 gives
up the catalog and the computation engine — which are usually the
reason you reached for Earth Engine to begin with.

DatensEE is meant to be a third path that keeps Earth Engine doing what
it's best at and lets the modern Google Cloud stack handle the
fan-out, retry, and observability concerns it's now well suited for.

## The right primitive is already there

The Earth Engine team has been quietly building exactly the primitive
this kind of workflow needs: the [High Volume API](https://developers.google.com/earth-engine/cloud/highvolume).
It evaluates an expression at a specific tile, at a specific scale, in a
specific projection, and returns a GeoTIFF. It's quota-friendly,
parallel-friendly, and stateless — a clean per-tile contract that maps
beautifully onto a distributed fetcher.

Crucially: **you don't need to understand the expression to use it.**
Earth Engine has already promised to evaluate it. You just need to call
it, a lot, in parallel, with proper retries and backoff.

That's the entire premise of DatensEE.

```
                          ┌───────────────────────────┐
                          │  Earth Engine HV backend  │
                          │  evaluates per-tile       │
                          └────────────▲──────────────┘
                                       │ thousands of concurrent calls
                  ┌────────────────────┴──────────────────────┐
                  │   Cloud Dataflow (Apache Beam)            │
                  │   tile coords → fetch → COG → GCS         │
                  └───────────────────────────────────────────┘
```

DatensEE is a tile fetcher with a very specific opinion about
parallelism. It is **not** a computation framework. It does not parse,
optimize, or rewrite your EE expression. The expression is opaque JSON
that we hand back to the EE backend per tile. When EE adds new
operators, when it improves an existing one, when its catalog grows —
DatensEE picks all of that up for free.

## Why Dataflow

Three reasons:

1. **It's the right shape.** A massively parallel tile fetch is exactly
   what Beam's `ParDo` was designed for. We don't need streaming
   semantics, we don't need windowing, we don't need state — we just
   need the autoscaler.
2. **It composes with the rest of the GCP story.** Auth, GCS output,
   Cloud Logging, billing, IAM all just work.
3. **It makes the failure model legible.** Each tile is an independent
   work item. Beam handles per-element retry, and we attach a
   dead-letter side-output for tiles that exceed our retry budget. A
   partial failure produces a partial result plus a manifest of exactly
   which tiles didn't make it, ready for a targeted re-run.

For small jobs (under ~100 tiles) DatensEE also ships a local Beam
runner. Spinning up Dataflow VMs takes ~2 minutes regardless of
workload size, and during iterative development that startup latency
dominates wall-clock time — running locally means edits feel instant
instead of waiting on a VM provisioner.

## The output: Cloud Optimized GeoTIFF

For each output tile, the pipeline writes a Cloud Optimized GeoTIFF
directly to GCS. COG was the right choice because it's the format
Earth Engine itself can read back via `ee.Image.loadGeoTIFF()` —
closing the loop. You can export, post-process locally, and feed the
result back into Earth Engine for a follow-up analysis without ever
materializing the data into a different format. There's no manifest
file: COGs are self-describing GeoTIFFs that any modern GIS reads
directly. Want one giant COG instead of many? Set
`output_tile_size_pixels` large enough to cover your region.

## Costs and quotas

Earth Engine itself stays on the same terms it always has. Non-commercial
users (research, education, nonprofit, journalism) keep their free
access — and that includes calls made through the High Volume API.
DatensEE doesn't change anything about how EE bills compute; it just
calls the public HV endpoint on your behalf.

What does cost money is **Dataflow**. To run an export at scale you'll
need a GCP project with billing enabled, and the Dataflow workers,
shuffle, and GCS storage are charged at standard rates. For
non-commercial EE users this is usually the only new bill — your EE
usage stays free, but the workers fetching tiles are real VMs. For
commercial users with billing already set up, it's another line item on
the same invoice. DatensEE prints a cost estimate before submitting any
job above a threshold so there are no surprises.

On **quotas**: the HV API has per-project request budgets, and a
realistic continental-scale export will saturate the default very
quickly. Quota uplifts go through the same channel they always have —
see the [Earth Engine usage and quota docs](https://developers.google.com/earth-engine/guides/usage)
for the current process. DatensEE deliberately ships no client-side
rate limiter: EE's quota system is the rate-shaping signal, and workers
respond to 429s with exponential backoff. To run conservatively on
default quotas, cap the worker count; scale it up after a quota review.

## Gotchas (the honest part)

This is the section you skip in most marketing posts. You shouldn't.

**Earth Engine's COG validator is strict.** The first internal-format-
directory has to live at offset 8 of the file, immediately after the
TIFF header. A vanilla TIFF writer puts it at the end (which is a
perfectly valid TIFF, just not a COG). EE rejects those with "The first
IFD does not immediately follow the TIFF header" and every pixel comes
back masked. We had to write our own COG transcoder in pure Java to
control the layout precisely. This is the kind of bug you only catch by
writing tests that decode your output with an *independent* reader.

**LZW compression is a footgun.** TIFF-LZW is loosely specified and
implementations disagree on edge cases (early code-table updates, EOI
handling, MSB-first vs LSB-first packing). Our hand-rolled encoder
worked against our own decoder but rounded-tripped through GDAL,
imagecodecs, and Earth Engine's loader as "Corrupted tile". We default
to deflate now (it's `java.util.zip.Deflater`, there's nothing to get
wrong) and treat LZW as a future cleanup.

**Auth at the right project boundary.** When DatensEE is driven
service-side (e.g. from a webapp acting on behalf of a user), the
Dataflow `createJob` call has to attribute its quota and API-enablement
checks to the **user's** project, not the webapp's service account
project. Setting the credential alone is not enough — you have to
explicitly inject `x-goog-user-project` into the HTTP request metadata,
because some auth-library versions don't propagate it through the URI-
taking variant of `getRequestMetadata`. Empirical answer-key, not
documentation.

**Tile alignment is a feature, not an accident.** DatensEE snaps tile
grids to a fixed global origin in the target CRS. Two independent
exports at the same scale and CRS produce *identical* pixel grids in
any overlapping area. This is a hard requirement for any downstream
analysis that compares regions to each other (change detection,
mosaicking, cross-region statistics). It's tested by shifting a region
by N pixels, fetching tiles from both grids, and asserting pixel-by-
pixel equality in the overlap.

**Partial failures need a UX.** If 50 tiles out of 10,000 fail after
exhausting retries, the right thing to do is finish the other 9,950 and
hand back a manifest of what failed. Aborting the whole job is
user-hostile. So is silently dropping the failures.

**Don't add `earthengine-api` as a dependency.** It is tempting to use
`ee.serializer` server-side, but it would balloon the install footprint
and pin DatensEE to a specific EE client version. We accept expressions
as opaque JSON, full stop. Users who want to author expressions
`pip install earthengine-api` separately. The two packages coexist
peacefully.

## What's next

Two things already work that we're still polishing, and one that's ahead:

- **Two-tier tiling.** *Compute tiles* (small, sized for the EE HV API)
  are separate from *output tiles* (large, sized for practical file
  counts): compute tiles are grouped with a Beam `GroupByKey` and
  assembled into larger multi-block COGs at write time. Set
  `output_tile_size_pixels` to dial output granularity from "one COG
  per fetch" to "one COG per region".
- **Adaptive retry.** When a tile fails because the expression hit EE's
  per-tile memory or timeout limit, `datensee retry` splits it into 4
  quadrant children and re-fetches at smaller area-per-call — recursive,
  depth-capped, driven by a structured failures journal. Transient
  failures retry as-is; the journal is always the complete picture of
  what's still missing.
- **Proactive adaptive tiling.** A regular grid wastes requests in
  sparse regions and pushes memory limits in dense ones. Choosing tile
  sizes *up front* from data density (rather than reactively on
  failure) is the obvious next step but has real complexity cost — we
  want to do it once, well.

## Feedback

> _Stub — we'll fill this in once we settle on a channel. For now,
> reach out via the team chat or open an issue on the repo._

If you've got an export workload that DatensEE can't handle, or one
where it's significantly slower / more expensive than it should be,
that's exactly the kind of feedback we want.
