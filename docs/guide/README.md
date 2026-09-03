# DatensEE user guide

> **Demo software, not an official Google product.** DatensEE is a proof of
> concept built to demonstrate a pattern. It is unsupported, may break as Earth
> Engine or Dataflow evolve, and may be removed without notice. Learn from it and
> build on it; don't use it as a production tool.

A practical guide to running Earth Engine exports at scale with DatensEE:
setting up your Google Cloud project, running your first export, debugging it
when it misbehaves, and a cookbook of recipes.

New here? Read in order. Already running and stuck? Jump to
[Debugging](03-debugging.md).

1. [Project setup](01-project-setup.md): one-time Google Cloud configuration:
   APIs, Earth Engine registration, a bucket, IAM, auth.
2. [Your first export](02-first-export.md): the local demo, then the same job
   on Dataflow.
3. [Debugging](03-debugging.md): reading failures, common errors and their
   fixes, the retry loop, output validation.
4. [Cookbook](04-cookbook.md): runnable recipes: NDVI, composites, large SRTM,
   exact-grid comparison, one big COG, recovering a partial failure.

> Screenshots live in [`img/`](img/). Entries marked **📷 Screenshot** are
> placeholders to be filled from a real console; each gives the exact page to
> open and what to capture.
