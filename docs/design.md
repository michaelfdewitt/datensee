# Why isn't this fifty lines?

"Call `computePixels` in parallel and stitch the results" sounds like a
`ThreadPoolExecutor` and rasterio. That version exists, and it is the right tool
up to roughly a few GB of output on one machine. DatensEE is what remains after
committing to the regime where that falls over: terabyte-scale exports,
thousands of concurrent workers, and hours-long jobs where partial failure is
the normal case rather than the exception. Nearly all of the code follows from
three commitments.

## 1. No native dependencies on workers

Dataflow workers do not ship GDAL, and custom containers plus native-library
versioning were not worth the operational cost. So COG output is a pure-Java
TIFF transcoder (`CogTranscoder`), and stitching thousands of tiles into
multi-block COGs (including merging retried tiles into *existing* COGs) is a
streaming assembler (`AssembledCogWriter`), not an in-memory mosaic.

## 2. Partial failure is the steady state

When 50 of 10,000 fetches fail, abort-and-rerun is not an answer. That buys:
429-driven exponential backoff, classification of EE's error signatures
(transient vs. too-complex vs. terminal), a structured failures journal,
quadtree splitting of tiles EE rejects as too expensive, and a
`retry --until-done` loop that converges instead of retrying into the same wall
forever. The naive version's answer to failure is "run it again," which at this
scale means hours and real money.

## 3. Precision guarantees that are invisible until violated

Every tile is an integer pixel rectangle in one canonical grid, so a retried
sub-tile lands pixel-exact in an existing COG by arithmetic, not float
tolerance. And every worker sees the same snapshot of every EE asset (snapshot
pinning); without it, a collection that mutates mid-export gives tile A and tile
B different worlds, and the output is silently wrong. Both bugs exist in the
naive version, and nobody notices until they diff outputs.

## The rest

Job submission and polling, cost estimation before you spend money, Colab auth,
and progress display are deliberate product surface: the API is the product, and
EE users are not infra engineers. The tests outnumber the source, which is the
price of the word "pixel-exact."

If your exports fit comfortably on one machine, use
[xee](https://github.com/google/xee) or `ee.batch.Export`. DatensEE exists for
when they stop fitting, and for nothing else: no catalog, no compute framework,
no new way to write your science.
