# Validation record — notebook parity correction

## 2026-10-14: approved NASA DN comparison and zero local threshold

Current offline suite: **37 passed, none skipped**. The test runner and missing
test dependencies were installed only under `/private/tmp/cyan-nasa-test-deps`;
no production or remote environment was modified.

New checks cover DN 0/1/250/251/252/253/254/255, counting TIFF nodata 255,
actual affine pixel area (including a non-300 m synthetic fixture), whole-lake
multi-tile coverage, overlap deduplication, conflict/misalignment rejection,
genuine zero versus blank measurements, unavailable/network failures, a covering
single-tile shortcut, regional fallback without double counting, invalid-download
cleanup, cached-file reuse without authentication, UTC date selection, weekly
product exclusion, and positive local CI below the former threshold.
Existing worker/publication tests now verify the 51-column statistics and
three-member ZIP contract, previous-master backup, crash recovery and concurrent
workers. All frozen notebook function-body checks still pass; the explicitly
approved detection constants and output schema have changed.

Real-file read-only check: a 12 km by 12 km test polygon crossing the existing
2024-08-14 CONUS 6_2/6_3 seam produced exactly the same counts from the two tiles
as from the complete CONUS GeoTIFF: 1,600 cells, 1,469 DN0, 0 DN1–253, 4 DN254,
127 DN255, and 144.0 km². NASA's live public CMR catalogue selected both tiles.
This test polygon is not a named lake and is not a full scientific scene validation.

The environment emitted Rasterio/NumPy deprecation warnings and an extension
ABI-size warning on import; all tests passed. Authenticated NASA downloading,
real Alaska raster handling, and a fresh Linux OCSSW scene have not been exercised.
See [NASA comparison](NASA_CYAN_COMPARISON.md) for the new contract and data sources.

## Historical notebook parity evidence (before the zero-threshold extension)

Local validation: 25 tests passed, none skipped, using an isolated temporary
Python 3.12 environment. No remote Conda environment was modified.

Covered:

- 57 function bodies checked against normalized hashes extracted independently
  from the authoritative notebook, including adaptive windows and final exporters.
- Real metadata configuration hash: USA + 1.3 km2 + adaptive notebook defaults
  reproduces 58f41b2c1318eaf1. WORLD is intentionally different.
- Regional admission regression: reject a scene outside the requested state even
  when another part of a large candidate lake intersects it; count regional lakes.
- Adaptive window ordering, maximum-window, area-fraction, dateline and explicit
  full-scene fallback cases.
- Synthetic native masks, bloom rescue, unchanged compact statistics and window IDs.
- Duplicate overlapping native observations are removed in final share statistics.
- Same-lake mapped NetCDF export and final 50-column scene/master contract.
- End-to-end synthetic worker: one download, two L2Gen calls with window bboxes,
  persisted native windows, one share ZIP, one master publication.
- State attributes preserve HydroLAKES coordinates and ambiguous boundaries stay blank.
- Concurrent claims, five isolated workers, three total attempts, publication
  idempotency, prior-master backup, crash recovery, failed-scratch retention,
  rejection of changed references/universes and refusal to relabel old CSV schemas.

Actual supplied-scene downstream comparison:

The implemented export module was run against supplied standalone native NC for
08d76a5e-d66c-4e3c-b59f-d0bdd3d463c7. All 41 numeric Colab columns match for all
791 shared lake IDs (rtol=1e-12, atol=1e-15; exact counts). This comparison deliberately
matches IDs for diagnosis and does not filter production output by the reference CSV.
The global input still has 5,331 lakes; matching the supplied run requires its USA
universe. See supplied_scene_comparison.json for per-column results.

The test environment emitted one NumPy extension ABI-size warning on first import;
tests and the supplied-data comparison passed. This warning is recorded, not evidence
that the Linux environment has been validated. Key local versions: NumPy 2.5.2,
Pandas 3.0.5, netCDF4 1.7.4, SciPy 1.18.1. OCSSW was mocked in synthetic worker tests.

Not executed: a fresh authenticated CDSE/getanc/Linux OCSSW scene, global reference
download, a mapped-raster comparison against the supplied Colab raster using the
original HydroLAKES geometries, or a multi-scene HPC performance benchmark. No
claim of bitwise equality, historical runtime identity, or remote deployment is made.

To run the offline tests, `PYTHONPATH=src` points Python at this checkout's source;
`python -m pytest` invokes the test runner in the selected environment; `-q` requests
concise output. Install the declared test extra in a test environment if needed.

```bash
PYTHONPATH=src python -m pytest -q
```
