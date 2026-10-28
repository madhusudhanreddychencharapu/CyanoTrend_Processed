"""Synthetic native-pixel QA/statistics checks, never a substitute for real OCSSW parity."""

import importlib.util
from pathlib import Path
import tempfile
import unittest
import uuid

AVAILABLE = all(
    importlib.util.find_spec(x) is not None
    for x in ("geopandas", "netCDF4", "scipy", "numpy", "pandas", "shapely")
)


@unittest.skipUnless(
    AVAILABLE, "Scientific dependencies are not installed in this interpreter"
)
class Science(unittest.TestCase):
    def setUp(self):
        import geopandas as gpd
        import netCDF4
        import numpy as np
        from shapely.geometry import box

        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.lakes = gpd.GeoDataFrame(
            {
                "Hylak_id": [1, 2],
                "Lake_name": ["A", "B"],
                "Lake_area": [20.0, 12.0],
                "Country": ["United States", "United States"],
                "Continent": ["North America", "North America"],
            },
            geometry=[
                box(-85.012, 39.999, -85.0, 40.01),
                box(-85.0, 39.999, -84.990, 40.01),
            ],
            crs=4326,
        )
        self.scene = {
            "id": str(uuid.uuid4()),
            "name": "S3A_SYNTHETIC",
            "start": "2024-01-01T12:00:00Z",
        }
        self.l2 = self.root / "l2.nc"
        with netCDF4.Dataset(self.l2, "w") as ds:
            ds.createDimension("rows", 2)
            ds.createDimension("cols", 6)
            dims = ("rows", "cols")
            nav = ds.createGroup("navigation_data")
            geo = ds.createGroup("geophysical_data")
            nav.createVariable("latitude", "f4", dims)[:] = np.tile(
                [[40.001], [40.004]], (1, 6)
            )
            nav.createVariable("longitude", "f4", dims)[:] = np.tile(
                [-85.009, -85.007, -85.005, -85.003, -84.997, -84.995], (2, 1)
            )
            f = geo.createVariable("l2_flags", "u4", dims)
            f.flag_meanings = "LAND CLDICE HISATZEN NAVFAIL"
            f.flag_masks = np.array([1, 2, 4, 8], dtype=np.uint32)
            f[:] = np.array([[0, 2, 2, 4, 1, 8], [0, 0, 0, 0, 0, 0]], dtype=np.uint32)
            values = {
                490: 0.01,
                560: 0.02,
                620: 0.01,
                665: 0.03,
                681: 0.02,
                709: 0.04,
                754: 0.05,
                865: 0.02,
                884: 0.01,
            }
            for w, v in values.items():
                a = np.full((2, 6), v, np.float32)
                if w == 754:
                    a[0, 2] = 0.001
                if w == 681:
                    a[1, 0] = 0.05
                if w == 620:
                    a[1, 1] = -0.01
                geo.createVariable(f"rhos_{w}", "f4", dims, fill_value=-32767)[:] = a

    def tearDown(self):
        self.temp.cleanup()

    def test_native_masks_statistics_and_schema(self):
        import netCDF4
        import numpy as np
        from cyanotrend.core import compact, statistics
        from cyanotrend.schema import COLUMNS

        p, counts = compact.extract_lake_only_netcdf(
            self.l2, self.scene, self.lakes, self.root / "native.nc", config_hash="ref"
        )
        self.assertEqual(counts["nobs"], 12)
        self.assertTrue(
            compact.validate_compact_product(
                p, scene_id=self.scene["id"], config_hash="ref"
            )
        )
        with netCDF4.Dataset(p) as ds:
            self.assertEqual(list(ds.dimensions), ["obs"])
            self.assertEqual(ds["valid_water_mask"][:6].tolist(), [1, 1, 0, 0, 0, 0])
            self.assertEqual(ds["bloom_rescue_mask"][:6].tolist(), [0, 1, 0, 0, 0, 0])
            self.assertEqual(
                ds["cyan_strict_valid_mask"][:6].tolist(), [1, 0, 0, 0, 0, 0]
            )
            self.assertTrue(np.ma.getmaskarray(ds["CI_cyano"][:])[2:7].all())
            self.assertLess(ds["rhos_620"][7], 0)
            self.assertEqual(ds["ci_valid_mask"][7], 1)
            self.assertEqual(ds.window_id, "FULL")
        table = statistics.compact_stats(p, self.lakes)
        self.assertEqual(len(table.columns), 42)  # Original per-window compact_stats remains intact.
        self.assertEqual(set(table.Hylak_id), {1, 2})
        self.assertEqual(table.native_polygon_pixels.sum(), 12)
        self.assertEqual(table.bloom_rescued_pixels.sum(), 1)

    def test_state_attributes_preserve_hydrolakes_coordinates(self):
        import geopandas as gpd
        from shapely.geometry import box
        from cyanotrend.references import assign_states

        lakes = self.lakes.copy()
        lakes["latitude"] = [40.001, 40.001]
        lakes["longitude"] = [-85.005, -84.995]
        admin = gpd.GeoDataFrame(
            {
                "country_iso3": ["USA", "USA"],
                "shapeID": ["s1", "s2"],
                "shapeName": ["West", "East"],
            },
            geometry=[box(-86, 39, -85, 41), box(-85, 39, -84, 41)],
            crs=4326,
        )
        result = assign_states(lakes, admin)
        self.assertEqual(result.latitude.tolist(), lakes.latitude.tolist())
        self.assertEqual(result.longitude.tolist(), lakes.longitude.tolist())
        self.assertEqual(result.state_name.tolist(), ["West", "East"])
        self.assertEqual(result.state_id.tolist(), ["s1", "s2"])
        lakes.loc[0, "longitude"] = -85
        result = assign_states(lakes, admin)
        self.assertEqual(result.loc[0, "admin_match_status"], "ambiguous")
        self.assertEqual(result.loc[0, "state_name"], "")

    def test_window_identity_is_preserved(self):
        import netCDF4
        from cyanotrend.core.compact import extract_lake_only_netcdf
        from cyanotrend.core.windows import window_id
        bounds = (-86, 39, -84, 41)
        path, _ = extract_lake_only_netcdf(self.l2, self.scene, self.lakes, self.root / "window.nc", window=bounds)
        with netCDF4.Dataset(path) as ds:
            self.assertEqual(ds.window_id, window_id(bounds))
            self.assertEqual(ds.window_bbox, "-86,39,-84,41")

    def test_reference_share_export_and_master_columns(self):
        import json
        import zipfile
        from types import SimpleNamespace
        from cyanotrend.core import compact, exports
        from cyanotrend.common import Workspace
        from cyanotrend.worker import build_bundle
        from cyanotrend.publication import validate_archive
        from cyanotrend.schema import COLUMNS, NOTEBOOK_COLUMNS
        p, _ = compact.extract_lake_only_netcdf(self.l2, self.scene, self.lakes, self.root / "native.nc", config_hash="ref")
        # Overlapping windows must not double count the same rounded native coordinates.
        exports.configure(self.lakes, [p, p], self.root / "exports")
        _, table = exports._share_all_index_stats(self.scene["id"], "ref", SimpleNamespace(scene_name=self.scene["name"], acquisition_start=self.scene["start"]))
        self.assertEqual(list(table.columns), NOTEBOOK_COLUMNS)
        self.assertEqual(table.native_lake_pixels.sum(), 12)
        raster, _ = exports.export_complete_scene_science_netcdf(self.scene["id"])
        table["state_name"] = ["West", "East"]
        table["state_id"] = ["s1", "s2"]
        table["latitude"] = [40.001, 40.001]
        table["longitude"] = [-85.005, -84.995]
        ws = Workspace.create(self.root / "out", self.root / "refs", self.root / "scratch", self.root / "ocssw")
        ws.ensure()
        from cyanotrend.schema import NASA_DETECTION_COLUMN
        table[NASA_DETECTION_COLUMN] = [7, 0]
        table = table[COLUMNS]
        archive = build_bundle(ws, self.scene, "ref", Path(raster), table, {})
        meta, rows = validate_archive(archive, self.scene["id"], "ref")
        self.assertEqual(list(rows[0]), COLUMNS)
        self.assertEqual([r[NASA_DETECTION_COLUMN] for r in rows], ["7", "0"])
        self.assertEqual(len(COLUMNS), 51)
        with zipfile.ZipFile(archive) as z:
            self.assertEqual(len(z.namelist()), 3)
            self.assertTrue(any(n.endswith("_ALL_INDEX_STATS.csv") for n in z.namelist()))


if __name__ == "__main__":
    unittest.main()
