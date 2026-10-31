"""The sole allowed config migration preserves original failure and never arms."""
import importlib.util
import json
from pathlib import Path

import pytest

path = Path(__file__).resolve().parents[2] / 'logs/s2/planted_step0_window_migrate.py'
spec = importlib.util.spec_from_file_location('window_migrate', path)
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


def fixture(tmp_path):
    config = json.loads((path.parent / 'planted_run_config.json').read_text())
    state = {'schema': 1, 'status': 'HALTED', 'phase': 'step0', 'pending': None,
             'sequence': 1, 'reason': 'step0 FAIL: gate failure',
             'completed': [{'work_id': 'pilot-0001-step0', 'result': {'status': 'FAIL'}}],
             'configuration_sha256': M.digest({k:v for k,v in config.items() if k != 'provenance'})}
    (tmp_path / 'planted_run_config.json').write_text(json.dumps(config))
    (tmp_path / 'planted_run_state.json').write_text(json.dumps(state))
    return config, state


def test_dry_run_and_applied_provenance_preserve_state_and_failure(tmp_path):
    config, state = fixture(tmp_path)
    p = tmp_path / 'planted_run_state.json'
    original = p.read_bytes()
    assert not M.migrate(tmp_path)['applied']
    assert p.read_bytes() == original
    assert M.migrate(tmp_path, True)['applied']
    after = json.loads(p.read_text())
    assert after['configuration_sha256'] == M.digest(config)
    assert after['status'] == 'HALTED' and after['completed'] == state['completed']
    assert (tmp_path / 'planted_run_state.before_window_decision.json').read_bytes() == original
    assert M.migrate(tmp_path, True)['already_applied']


@pytest.mark.parametrize('mutation', ['path', 'window', 'pending', 'retry', 'armed'])
def test_reject_non_provenance_config_changes_or_active_state(tmp_path, mutation):
    config, state = fixture(tmp_path)
    if mutation == 'path':
        config['root'] += '/other'
    elif mutation == 'window':
        config['provenance']['window_decision']['probe_auto_compact_window'] = 80000
    elif mutation == 'pending':
        state['pending'] = {'work_id':'active'}
    elif mutation == 'retry':
        state['authorized_step0_retries'] = [{'from_work_id':'pilot-0001-step0'}]
    else:
        state['status'] = 'ARMED'
    (tmp_path / 'planted_run_config.json').write_text(json.dumps(config))
    p = tmp_path / 'planted_run_state.json'
    p.write_text(json.dumps(state))
    before = p.read_bytes()
    with pytest.raises(ValueError):
        M.migrate(tmp_path, True)
    assert p.read_bytes() == before
