# Whole-lake daily NASA CyAN comparison

The 2026-10-14 approved extension adds `NASA_CI_Cyano_detectable_cells` between
`ci_cyano_detection_pixels` and `CI_valid_pixels` in each published scene CSV,
master.csv and master_backup.csv. These files have 51 columns. The backup remains
the previous successful master, including a header-only initial backup.

The separate `NASA_CYAN_COMPARISON.csv` is no longer exported. The existing
NASA calculation and `NASA_CI_Cyano_detectable_cells` column are unchanged.
The internal comparison table, in schema.py order, includes the scene identity, acquisition time, Hylak_id, name, HydroLAKES
area, native/retained/CLDICE/rescued/candidate/detection counts and these NASA fields:

| Field | Definition |
| --- | --- |
| NASA_CYAN_Lake_area_km2 | Unique selected NASA cells times their actual equal-area affine pixel area, converted to km² |
| NASA_CYAN_Lake_pixels | All selected original NASA raster cells, including all four DN categories |
| NASA_CYAN_Non_detectable_pixels | DN = 0 |
| NASA_CI_Cyano_detectable_cells | DN = 1 through 253, inclusive |
| NASA_CYAN_Land_pixels | DN = 254 |
| NASA_CYAN_No_valid_data_pixels | DN = 255, counted even when declared TIFF nodata |
| NASA_CYAN_date | Requested daily product date, from scene acquisition start in UTC |
| NASA_CYAN_source_files | Semicolon-separated source filenames |
| NASA_CYAN_tile_ids | Semicolon-separated region:tile identifiers; ALL denotes regional GeoTIFF |
| NASA_CYAN_coverage_status | complete, unavailable, incomplete_coverage or explicit error status |
| NASA_CYAN_status_reason | Explanation when measurements are unavailable |

NASA cells are mapped product cells, not native OLCI observations. NASA area includes
all four DN classes whose centres are strictly inside the full HydroLAKES polygon.
It is not water-only area or a fractional intersection-area calculation.
No intensity decoding, threshold on decoded values, resampling, local-to-NASA
pairing, or clipping to the local scene footprint is performed.
All local scenes acquired on the same UTC day use the corresponding whole-lake
daily NASA product. Local CI detection is strictly CI > 0.0, retaining the existing
SS681/SS665 spectral and QA rules. The threshold also feeds existing bloom-rescue
evidence and processing identity; it is not merely a CSV relabelling.

## Tile selection and completeness

NASA CMR collection C3416412382-OB_CLOUD identifies version-6 merged Sentinel-3
CI products. The date and filename filter explicitly excludes weekly products,
true colour and other dates. Geographic corner metadata selects candidate CONUS
and Alaska files in the regional Albers CRS. A 100 m pad compensates only for
rounded catalogue corners; it never expands the lake count mask.

A covering individual tile is sufficient. Lakes crossing tile edges use all
intersecting tiles. The daily regional GeoTIFF is a fallback if individual tiles
are missing or their actual combined extents do not cover the lake. Actual raster
CRS, transform and extents are checked before producing numbers. No local scene
extent enters this test. If full coverage remains unavailable, all six NASA
measurement fields are blank and processing continues with a logged warning.
DN255 within an existing raster is counted as no valid data, not missing coverage.

Counts use unmasked uint8 data. Overlap is resolved by original integer pixel-grid
addresses. Identical overlapping cells count once; disagreeing overlaps and
misaligned grids are rejected rather than averaged or resampled. Pixel dimensions
and units come from the raster's Albers Equal Area CRS and affine transform.

The shared cache is `<reference directory>/obpg_cyan/` (normally
`reference_data/obpg_cyan/`). A per-file lock and atomic rename prevent concurrent
workers from using partially downloaded tiles. Existing cached files are reused;
no automatic reprocessing-version replacement is performed. Downloading uses the
existing Earthdata `.netrc` entry. Credentials are never written into outputs.
Authentication, network, invalid-file and coverage failures are logged, with blank
NASA measurements. Zero is reserved for an actual measured zero.

The ZIP metadata records source URLs, CMR identifiers/update times, SHA-256 hashes,
raster CRS, dimensions, transform, product tags, DN rules, area method and overlap
policy. NASA traceability fields are not added to master.csv or master_backup.csv.

## Fresh processing and validation boundary

Use a fresh output workspace including the operations ledger and plans. This
change does not rewrite existing results or delete data. Existing 50-column
masters and old processing fingerprints are deliberately rejected.

Source: [NASA CyAN V6 release notes](https://oceancolor.gsfc.nasa.gov/images/cyan/version/6/CyAN_NASA_MERISOLCI_CI_release_notes_V6_Aug_2025.pdf),
[NASA CyAN file search](https://oceandata.sci.gsfc.nasa.gov/api/cyan_file_search),
and [SeaDAS regional projection definitions](https://seadas.gsfc.nasa.gov/help-9.1.0/processors/ProcessL3mapgen.html).
Offline tests do not certify an authenticated NASA download or a fresh Linux OCSSW run.
