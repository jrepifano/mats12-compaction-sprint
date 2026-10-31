"""Offline coordinator report-window migration: exact decision, unchanged old receipts."""
import importlib.util
import json
import shutil
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('report_window_migrate', ROOT / 'logs/s2/planted_report_window_migrate.py')
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


def setup(tmp_path):
    directory = tmp_path / 'logs/s2'
    directory.mkdir(parents=True)
    shutil.copy(ROOT / 'logs/s2/planted_verification_v2_freeze.json', directory)
    shutil.copytree(ROOT / 'configs/s2/planted', tmp_path / 'specs')
    config = json.loads((ROOT / 'logs/s2/planted_run_config.json').read_text())
    config['specs_dir'] = str(tmp_path / 'specs')
    old = deepcopy(config)
    old['step0_retry_authorization'] = {'decision_id':'planted-verification-v2',
        'from_work_id':'pilot-0002-step0','max_operations':1,'after_completed_retry_count':1}
    old['provenance'].pop('report_window_decision')
    state = {'schema':1, 'status':'HALTED', 'phase':'step0', 'sequence':3, 'pending':None,
             'reason':'step0 FAIL: gate failure', 'configuration_sha256':M.digest(old),
             'completed':[{'work_id':'pilot-0001-step0','result':{'status':'FAIL'}},
                          {'work_id':'pilot-0002-step0','result':{'status':'FAIL'}},
                          {'work_id':'pilot-0003-step0','result':{'status':'FAIL'}}],
             'authorized_step0_retries':[{'from_work_id':'pilot-0001-step0',
                                         'consumed_by_work_id':'pilot-0002-step0'},
                                         {'from_work_id':'pilot-0002-step0','consumed_by_work_id':'pilot-0003-step0'}]}
    (directory / 'planted_run_config.json').write_text(json.dumps(config))
    (directory / 'planted_run_state.json').write_text(json.dumps(state))
    return directory, config, state


def test_migration_preserves_both_failures_and_prior_retry_and_never_arms(tmp_path):
    directory, config, original = setup(tmp_path)
    p = directory / 'planted_run_state.json'
    before = p.read_bytes()
    assert not M.migrate(tmp_path)['applied']
    assert p.read_bytes() == before
    assert M.migrate(tmp_path, True)['applied']
    state = json.loads(p.read_text())
    assert state['status'] == 'HALTED' and state['sequence'] == 3
    assert state['completed'] == original['completed']
    assert state['authorized_step0_retries'] == original['authorized_step0_retries']
    assert state['configuration_sha256'] == M.digest(config)
    assert (directory / 'planted_run_state.before_report_window.json').read_bytes() == before
    assert M.migrate(tmp_path, True)['already_applied']


@pytest.mark.parametrize('change', ['spec','manifest','path','sequence','pending','reason','receipt','approval','hash_without_receipt'])
def test_migration_fails_closed_on_unapproved_changes(tmp_path, change):
    directory, config, state = setup(tmp_path)
    if change == 'spec':
        next((tmp_path / 'specs').glob('*.json')).write_text('{}')
    elif change == 'manifest':
        (directory / 'planted_verification_v2_freeze.json').write_text('{}')
    elif change == 'path':
        config['root'] += '/other'
    elif change == 'sequence':
        state['sequence'] = 4
    elif change == 'pending':
        state['pending'] = {'work_id':'inflight'}
    elif change == 'reason':
        state['reason'] = 'operator stop'
    elif change == 'receipt':
        state['completed'][-1]['result']['status'] = 'PASS'
    elif change == 'approval':
        config['step0_retry_authorization']['required_pairs'] = 1
    else:
        state['configuration_sha256'] = M.digest(config)
    (directory / 'planted_run_config.json').write_text(json.dumps(config))
    p = directory / 'planted_run_state.json'
    p.write_text(json.dumps(state))
    before = p.read_bytes()
    with pytest.raises(ValueError):
        M.migrate(tmp_path, True)
    assert p.read_bytes() == before
