"""Exercise the complete adaptive worker with synthetic L2, without satellite services."""
import json
import shutil
from unittest.mock import patch
import test_science as science_fixture
from cyanotrend.common import Workspace
from cyanotrend.ledger import Ledger
from cyanotrend.worker import process, candidate_path
from cyanotrend.publication import Publisher


def test_two_windows_use_one_download_and_one_scene_publication(tmp_path):
    fixture = science_fixture.Science('test_window_identity_is_preserved')
    fixture.setUp()
    try:
        scene = dict(fixture.scene)
        scene['archive_region'] = {'country': 'USA', 'state_id': 'mi', 'state_name': 'Michigan'}
        from shapely.geometry import box, mapping
        scene['geofootprint'] = mapping(box(-86,39,-84,41))
        scene['name'] = 'S3B_OL_1_EFR____20240815T151101_20240815T151401_20240816T001237_0179_096_239_2160_PS2_O_NT_004'
        scene['lake_region'] = 'WORLD'
        lakes = fixture.lakes.copy()
        lakes['latitude'] = [40.001,40.001]
        lakes['longitude'] = [-85.005,-84.995]
        lakes['state_name'] = ['West','East']
        lakes['state_id'] = ['s1','s2']
        ws = Workspace.create(tmp_path/'out',tmp_path/'refs',tmp_path/'scratch',tmp_path/'ocssw')
        ws.ensure()
        ledger = Ledger(ws.db)
        ledger.enqueue({'id':'plan','created':'now','reference_id':'ref','selection':{},'scenes':[scene]},ws.output/'plans/plan.json')
        record = ledger.claim()
        outputs = []
        def l2gen(*args, **kwargs):
            p = tmp_path / f'l2_{len(outputs)}.nc'
            shutil.copyfile(fixture.l2,p)
            outputs.append(p)
            return p, {'synthetic': True}
        bounds = [(-86,39,-85,41),(-85,39,-84,41)]
        with patch.dict('os.environ',{'CDSE_USERNAME':'test','CDSE_PASSWORD':'test'}), patch('cyanotrend.nasa_cyan.discover_tiles', return_value=[]), patch('cyanotrend.references.manifest',return_value={'reference_id':'ref'}), patch('cyanotrend.references.load_lakes',return_value=lakes), patch('cyanotrend.execution.configure'), patch('cyanotrend.core.staging.stage_scene_efficient',return_value=(None,tmp_path/'test.SEN3','synthetic')) as staging, patch('cyanotrend.core.staging.release_temporary_s3_credentials'), patch('cyanotrend.core.windows.plan_scene_windows',return_value=bounds), patch('cyanotrend.core.l2gen.run_l2gen',side_effect=l2gen) as run:
            process(ws,scene['id'],record['attempts'])
        assert staging.call_count == 1
        assert run.call_count == 2
        assert [c.kwargs['bbox'] for c in run.call_args_list] == bounds
        assert all(c.kwargs['process_full_scene'] is False for c in run.call_args_list)
        assert not any(p.exists() for p in outputs)
        publisher = Publisher(ws,ledger)
        publisher.publish(scene,'ref',candidate_path(ws,scene['id']))
        assert ledger.scene(scene['id'])['status'] == 'done'
        import pandas as pd
        master = pd.read_csv(ws.output/'master.csv')
        assert len(master) == 2 and master.native_lake_pixels.sum() == 12
        assert list(master.state_name) == ['West','East']
        assert not list(ws.output.rglob('*.nc'))
        assert 'NASA_CI_Cyano_detectable_cells' in master.columns
        import zipfile
        with zipfile.ZipFile(ws.archive(scene)) as bundle:
            assert len(bundle.namelist()) == 3
            metadata = json.loads(bundle.read(f"S3_OLCI_{scene['id']}_METADATA.json"))
            assert metadata['native_compact_files_retained'] is False
            assert 'native_compact_files' not in metadata
    finally:
        fixture.tearDown()
