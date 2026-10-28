# Notebook parity contract

The user-supplied v2.6.8 notebook is authoritative. SHA256 and original final-share
column order are in reference.json. Notebook cell indices are zero-based.
No notebook is imported or executed at application runtime.

Approved extension (2026-10-14): local detection now uses strictly CI > 0.0
with existing spectral and QA conditions, replacing the reference threshold.
NASA daily raw-DN whole-lake counts are added separately; see
[NASA comparison](NASA_CYAN_COMPARISON.md). This changes the processing hash and
the output contract; historical numerical parity evidence is not a new-run guarantee.

Effective source functions are copied into standalone Python modules:

- Cell 42: adaptive windows, margin 3 km, max 6, fraction threshold 0.35, and window IDs.
- Cells 20/30/48: NDCI, CI/CIcyano, MPH/FAI and production bands.
- Cells 44/57: polygon membership, native extraction and masks, float32 compact writer.
- Cell 57: scene pixel loading with first-observation deduplication by Hylak_id and
  latitude/longitude rounded to six decimals, and original configuration hash.
- Cells 58/59: nominal 300 m EPSG:4326 same-lake nearest-native mapping and the
  final ALL_INDEX_STATS statistics. These statistics use native observations,
  never mapped raster values. Nearest source is selected before QA.
- Cell 50: the scene must intersect the selected administrative polygon as well
  as its candidate lake pool. Regional candidate counts are not whole-scene counts.
- Cell 29: the USA lake universe uses full lake polygons intersecting the Census
  USA geometry. WORLD retains the global universe. Area remains >=1.3 as requested.

notebook_function_contract.json contains normalized AST hashes independently
extracted from the notebook. Tests compare imported source against these hashes;
module qualifiers/docstrings are normalized, numerical operations are not.
science_audit.json includes remaining operational/context differences.

## Final CSV

Scene CSV, master and backup preserve the final notebook names/order, with
NASA_CI_Cyano_detectable_cells inserted between ci_cyano_detection_pixels and
CI_valid_pixels, followed by state_name, state_id, latitude, longitude.
There are 51 columns. Detailed NASA measurements and traceability are confined
to the metadata JSON; the separate NASA_CYAN_COMPARISON.csv is no longer exported.
The earlier compact_stats function remains unchanged for reference, but its
per-window column names are not substituted into the final scene/master outputs.
Coordinates appended to CSV are original HydroLAKES Pour_lat/Pour_long. Pixel
coordinates remain in native NC. State matching uses those original lake points
against ADM1 geometry; unmatched/ambiguous points remain blank, not nearest-state guesses.

## Operational differences retained

Independent process per scene; windows remain sequential within each scene.
One staging download per scene, saved queue, three attempts, single master writer,
previous-successful-master backup, locks/recovery and CLI telemetry remain.
A separate process owns each export context, so no mutable notebook globals are
shared across concurrent scenes. Output filenames/folder root, timestamps,
operational checksums, and added state metadata cannot be byte-identical to Colab.
Native outputs remain in scratch only; the published ZIP contains three files. Successful scratch is
removed; failed scratch is retained. An empty native scene completes without a
fabricated NetCDF/share ZIP, as the notebook's processing/share distinction requires.

## Evidence and limits

Historically, the supplied Colab config hash 58f41b2c1318eaf1 was reproduced from USA,
minimum area 1.3 and the old threshold; WORLD yielded 5cab52e25b49b846.
The new zero threshold intentionally changes both hashes. The earlier final share writer reproduced all 41 numeric reference columns
for all 791 shared lakes from the supplied native observations at rtol=1e-12,
atol=1e-15; counts agree exactly. See supplied_scene_comparison.json.
The 4,540 additional global lakes are all Canada-labelled in the supplied older
standalone CSV; deduplicating observations does not remove these lake IDs.

No fresh Linux L2Gen run has been executed. The supplied Colab share lacks its
native window inputs/runtime package inventory. Source and downstream numerical
agreement do not certify upstream OCSSW/ancillary/library equivalence. Keep real
L1 identity, HydroLAKES geometry/order, loaded universe, windows, L2 parameters,
binary/LUT/ancillary versions and package versions aligned for end-to-end validation.
Dependencies remain version ranges because the historical Colab versions are unknown;
no fabricated pins or automatic OCSSW upgrades were introduced.
