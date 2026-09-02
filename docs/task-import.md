# Design note: importing an EE export task description

**Status: design only — blocked on an Earth Engine Code Editor affordance.**

## The idea

Code Editor users configure exports as `Export.image.toCloudStorage(...)`
calls. If the Code Editor grows a **"copy job description"** action (an
ask to the EE team), the copied JSON is an
[`ExportImageRequest`](https://developers.google.com/earth-engine/reference/rest/v1/projects.image/export)
— the exact payload the Editor would have POSTed to `image:export`. That
JSON contains everything DatensEE needs, so the migration story for
JavaScript users becomes: copy the job description, then

```
datensee export --task task.json --project my-project
```

## Mapping (`ExportImageRequest` → `datensee.export` kwargs)

| Task field | datensee kwarg | Notes |
| --- | --- | --- |
| `expression` | `ee_expression` | Opaque passthrough, exactly as today — we never interpret it. One exception below. |
| `grid` (`PixelGrid`) | *(replaces region/scale/crs)* | EE's `PixelGrid` is already our canonical shape. When present, tile **that grid directly** — skip `decompose_region`'s snap (the user pinned an explicit grid; honoring it verbatim is the point). |
| `fileExportOptions.cloudStorageDestination` | `output` | `gs://{bucket}/{filenamePrefix}` |
| `description` | Dataflow job label | `labels={"ee-task": description}` |
| `maxPixels`, `shardSize`, `fileDimensions`, `skipEmptyTiles`, `formatOptions.cloudOptimized` | ignored | Warn once, listing each ignored knob and why (we have no pixel cap; granularity is `output_tile_size`; output is always COG). |
| `assetExportOptions` / Drive destinations | rejected | Actionable error: DatensEE exports to GCS only. |

**The region wrinkle:** when the client converts `region`/`scale`/`crs`
into the request, it either sets `grid` or wraps the expression in
`Image.clipToBoundsAndScale(geometry=..., ...)`. In the wrapper case we
peel exactly the **outermost** `clipToBoundsAndScale` node to recover the
region + scale for tiling, leave the wrapper in the expression (EE-side
masking preserved), and treat everything inside as opaque — consistent
with the "compose, don't interpret" rule.

## Shape of the implementation

No new abstraction: one adapter function
`task_to_export_kwargs(task: dict) -> dict` feeding the existing
`export(**kwargs)`, plus the `--task` CLI flag. Estimated ~100 lines +
tests. **Not built yet** on purpose — the adapter should be written
against the JSON the Code Editor actually emits, not against our guess
of it.

## Prerequisite ask to the EE team

A "copy job description" affordance in the Export dialog (or Tasks tab)
that puts the `ExportImageRequest` JSON on the clipboard. Everything
else on this page is our side of the seam.
