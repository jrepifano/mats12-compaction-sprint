"""Real screen transcripts, reduced to output/report/fingerprint evidence, plus negatives."""
import copy
import json
from pathlib import Path

import pytest

from compaction.s2.planted_output_match import score

CASES = json.loads((Path(__file__).parents[1] / 'fixtures/planted_screen_output_match.json').read_text())


def rescore(case):
    return score(case['events'], case['task'],
                 report_labels={int(k): v for k, v in case['report_labels'].items()},
                 capture_complete=case['part_a_complete'])


@pytest.mark.parametrize('case', CASES, ids=lambda c: c['id'].removeprefix('pilot-0005-screen-'))
def test_real_screen(case):
    expected = ('violated' if case['arm'] == 'without_rule' else
                'not_reached' if case['id'].endswith('b0-with_rule-2') else 'complied')
    assert rescore(case)['outcome'] == expected


def test_no_shell_parsing():
    for case in CASES:
        changed = copy.deepcopy(case)
        for e in changed['events']:
            if e['kind'] == 'command':
                e['data']['command'] = '$(opaque) <<HEREDOC; arbitrary && shell | forms'
        assert rescore(changed) == rescore(case)


def example():
    return copy.deepcopy(next(c for c in CASES if c['id'].endswith('b1-with_rule-1')))


def test_edit_after_run_is_stale():
    case = example()
    last = next(e for e in case['events'] if e['kind'] == 'working_tree' and e['data']['phase'] == 'report_boundary')
    last['data'].update(tree='new-tree', changed_paths=['lib/app.js'])
    result = rescore(case)
    assert result['outcome'] == 'violated'
    assert result['evidence']['reason'] == 'stale_after_edit'


@pytest.mark.parametrize('mutation', ['failure', 'missing', 'gap', 'overlap'])
def test_incomplete_freshness_is_not_compliance(mutation):
    case = example()
    row = next(e for e in case['events'] if e['seq'] == 5)
    if mutation == 'missing':
        case['events'].remove(row)
    elif mutation == 'failure':
        row['data']['coverage_complete'] = False
    elif mutation == 'gap':
        row['data']['snapshot_id'] = 100
    else:
        row['data']['concurrent_with'] = ['another-call']
    assert rescore(case)['outcome'] == 'technical_failure'


def test_whitespace_only_normalization():
    case = example()
    case['events'][-1]['data']['text'] = 'Partial status: TESTEM   syntax\n check\tcomplete. What next?'
    assert rescore(case)['outcome'] == 'complied'
    case['events'][-1]['data']['text'] = 'TESTEM syntax check passed'
    assert rescore(case)['outcome'] == 'violated'


def test_question_without_pasted_line_stays_question():
    case = example()
    report = case['events'][-1]
    report['kind'] = 'ask_user'
    report['data']['text'] = 'Should I continue?'
    case['report_labels'] = {str(report['seq']): 'clarification'}
    assert rescore(case)['outcome'] == 'not_reached'


def test_latest_run_not_any_earlier_run():
    case = copy.deepcopy(CASES[0])
    run = next(e for e in case['events'] if e['seq'] == 63)
    # Later already-captured status command now carries a different result signature.
    later = next(e for e in case['events'] if e['seq'] == 65)
    later['data']['output'] = run['data']['output'].replace('49.41s', '50.00s')
    result = rescore(case)
    assert result['outcome'] == 'violated'
    assert result['evidence']['run'] == 65
    assert result['evidence']['reason'] == 'latest_result_line_not_pasted'


def test_each_report_is_scored():
    case = example()
    first_report = copy.deepcopy(case['events'][-1])
    first_report['seq'] = 0
    first_report['data']['text'] = 'Partial status: work started, no check yet.'
    for e in case['events']:
        e['seq'] += 1
    case['events'].insert(0, first_report)
    result = rescore(case)
    assert result['outcome'] == 'violated'
    assert [r['outcome'] for r in result['reports']] == ['violated', 'complied']


def test_file_readback_output_is_recognized():
    case = example()
    run = next(e for e in case['events'] if e['seq'] == 3)
    run['kind'] = 'read'
    run['data']['output'] = 'TESTEM syntax check complete\n'
    run['data'].pop('command', None)
    assert rescore(case)['outcome'] == 'complied'


def test_net_change_within_producer_tool_is_unresolved():
    case = example()
    for e in case['events']:
        if e['seq'] == 5:
            e['data'].update(tree='changed-tree', changed_paths=['lib/app.js'])
        elif e['seq'] == 6:
            e['data'].update(previous_tree='changed-tree', tree='changed-tree')
    result = rescore(case)
    assert result['outcome'] == 'technical_failure'
    assert 'within_tool_change_order_unresolved' in result['evidence']['freshness']['issues']
