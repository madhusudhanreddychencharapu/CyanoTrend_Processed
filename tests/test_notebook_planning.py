"""Reference regional admission and adaptive fallback regression cases."""
import json
import uuid
from unittest.mock import patch
import geopandas as gpd
from shapely.geometry import box, mapping
from cyanotrend.common import Workspace
from cyanotrend.planning import create_plan
from cyanotrend.core.windows import plan_scene_windows


def test_remote_part_of_cross_region_lake_cannot_admit_scene(tmp_path):
    # A long lake touches the requested northern region AND the southern scene.
    # Lake intersection alone used to admit this scene despite disjoint regions.
    lakes = gpd.GeoDataFrame({"Hylak_id": [1, 2], "country_iso3": ["USA", "USA"],
        "state_id": ["north", "elsewhere"], "state_name": ["North", "Elsewhere"]},
        geometry=[box(0, 0, 1, 10), box(2, 8, 3, 9)], crs=4326)
    admin = gpd.GeoDataFrame({"country_iso3": ["USA"], "shapeID": ["north"], "shapeName": ["North"]},
        geometry=[box(0, 9, 1, 10)], crs=4326)
    south = {"id": str(uuid.uuid4()), "satellite": "S3A", "geofootprint": mapping(box(0, 0, 1, 1))}
    north = {"id": str(uuid.uuid4()), "satellite": "S3B", "start": "2024-08-15T12:00:00Z", "geofootprint": mapping(box(0, 8, 3, 10))}
    ws = Workspace.create(tmp_path/'out', tmp_path/'refs', tmp_path/'scratch', tmp_path/'ocssw')
    with patch('cyanotrend.references.manifest', return_value={"coverage":"global", "reference_id":"ref"}), patch('cyanotrend.references.load_lakes', return_value=lakes), patch('cyanotrend.references.boundaries', return_value=admin), patch('cyanotrend.core.catalogue.search_olci_l1_catalog', return_value=[south,north]) as search:
        result = create_plan(ws, '2024-08-15', '2024-08-15', 'USA', 'north', 5)
    plan = json.loads(open(result['plan']).read())
    assert [s['id'] for s in plan['scenes']] == [north['id']]
    assert plan['scenes'][0]['archive_region'] == {'country': 'USA', 'state_id': 'north', 'state_name': 'North'}
    assert plan['scenes'][0]['candidate_lakes'] == 1  # not both whole-scene lakes
    assert search.call_args.kwargs['max_products'] == 5


def test_adaptive_windows_and_notebook_full_scene_fallbacks():
    scene = {"geofootprint": mapping(box(0, 0, 10, 10))}
    lakes = gpd.GeoDataFrame(geometry=[box(1, 1, 1.1, 1.1), box(8, 8, 8.1, 8.1)], crs=4326)
    windows = plan_scene_windows(scene, lakes)
    assert len(windows) == 2 and all(w is not None for w in windows)
    assert windows == sorted(windows, key=lambda b: (b[1], b[0]))
    assert plan_scene_windows(scene, lakes, max_windows=1) == [None]
    assert plan_scene_windows(scene, lakes, full_scene_fraction=0.00001) == [None]
    assert plan_scene_windows(scene, lakes, strategy='full_scene') == [None]
    assert plan_scene_windows({"geofootprint": mapping(box(-179,0,179,10))}, lakes) == [None]


def test_zero_catalogue_cap_matches_notebook_all():
    from cyanotrend.cli import parser
    args = parser().parse_args(['plan','--start','2024-08-15','--end','2024-08-15','--max-scenes','0','--country','USA','--state-id','north'])
    assert args.max_scenes == 0


def test_planning_requires_country_and_state_before_reference_loading(tmp_path):
    import pytest
    ws = Workspace.create(tmp_path/'out', tmp_path/'refs', tmp_path/'scratch', tmp_path/'ocssw')
    for country, state in [(None, None), ('USA', None), (None, 'mi')]:
        with patch('cyanotrend.references.manifest') as manifest:
            with pytest.raises(ValueError, match='both --country and --state-id'):
                create_plan(ws, '2024-08-14', '2024-08-14', country, state)
            manifest.assert_not_called()
