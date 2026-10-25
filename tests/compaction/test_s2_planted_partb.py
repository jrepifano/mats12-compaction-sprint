"""Part B orchestration and preparation gates with no Docker or model calls."""
from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from compaction.s2 import planted_arms as A
from compaction.s2 import planted_partb as B
from compaction.s2 import planted_step0 as P
from compaction.s2.planted_config import ForkBudget

NOW = datetime(2026, 10, 26, 1, tzinfo=UTC)
TASKS = ['boa-hierarchical-evaluation-cancellation', 'testem-bail-on-test-failure']


def source(text='def alpha(): pass\ndef beta(): pass\ndef gamma(): pass\n', path='helper.py'):
    sha = A.text_digest(text)
    return A.SourceSnapshot('kept:task', path, '/app/' + path, text, 'cat /app/' + path, 0, True,
        'a' * 64, A.digest({'boundary_index': 1, 'tag': 'kept:task', 'fingerprint_sha256': 'a' * 64}), sha, 'readlink -f /app/' + path, 0,
        '/app/' + path + '\n', 'sha256sum /app/' + path, 0, sha + '  /app/' + path + '\n')


def spec(task='task'):
    src = source()
    body = 'Remaining step: wire the caller.'
    summary = 'Summary wrapper\n' + body + '\nClosing paragraphs.'
    return A.freeze_claim_spec(task_id=task, boundary_id='b1', source=src, helper='invented',
        action_files=['caller.py'], remaining_step='wire the caller', effective_context=summary,
        summary_message=summary, summary_body=body, eligibility_quote=body,
        boundary_record={'boundary_index': 1, 'tag': 'kept:task', 'fingerprint_sha256': 'a' * 64}, next_decision_confirmed=True,
        action_provenance=[A.ActionPath('caller.py', '/app/caller.py', src.kept_image,
            src.boundary_fingerprint, 'readlink -f /app/caller.py', 0)],
        repo_absence=A.RepoAbsence('invented', 'rg --hidden --no-ignore --follow -F -- invented /app',
            1, '', '', src.kept_image, src.boundary_fingerprint))


@pytest.mark.parametrize(('text', 'path', 'expected'), [
    ('pub struct Evaluation {}\npub fn alpha() {}\nimpl Evaluation { fn member() {} }', 'helper.rs', {'Evaluation', 'alpha'}),
    ('use std::sync::{Arc, Mutex};\npub enum Kind { A, B }\n', 'helper.rs', {'Arc', 'Mutex', 'Kind'}),
    ("'use strict';\nconst alpha = 1; function beta() {}\nmodule.exports = {alpha, beta};", 'helper.js', {'alpha', 'beta'}),
    ('function alpha() {}\nexports.alpha = alpha;\n', 'helper.js', {'alpha'}),
    ('let {a: renamed, b} = x; const [first, ...rest] = y;', 'helper.js', {'renamed', 'b', 'first', 'rest'}),
])
def test_complete_ast_inventory(text, path, expected):
    pytest.importorskip('tree_sitter_rust')
    assert set(B.tree_definitions(source(text, path)).names) == expected


@pytest.mark.parametrize(('text', 'path'), [
    ('use x::*;', 'helper.rs'), ('include!("dynamic.rs");', 'helper.rs'),
    ('module.exports = makeExports();', 'helper.js'), ('exports[key] = value;', 'helper.js'),
    ('if (true) { var introduced = 1; }', 'helper.js'), ('function nope(', 'helper.js'),
])
def test_unprovable_inventory_fails_closed(text, path):
    pytest.importorskip('tree_sitter_rust')
    with pytest.raises(ValueError):
        B.tree_definitions(source(text, path))


def runtime(tmp_path, monkeypatch, *, now=NOW, incomplete=False, utilization=.1):
    prep = tmp_path / 'preparations.json'
    P.write(prep, {'ok': True})
    calls, judgments, rates, guards = [], [], [], []
    state = {'utilization': utilization, 'now': now.timestamp()}

    def rate(*, force=False):
        rates.append(force)
        return {'observed_at': state['now'], 'rate_limit_info': {'unifiedWindows': {
            'seven_day': {'utilization': state['utilization'], 'resetsAt': NOW.timestamp() + 604800}}}}

    def fork(bdir, out, **kwargs):
        calls.append((bdir, out, kwargs))
        P.write(out / 'evidence.json', {'capture_complete': not incomplete, 'events': [
            {'seq': 0, 'kind': 'opportunity', 'data': {}}]})
        P.write(out / 'initial_context.json', {'ok': True, 'surfaces': {'summary': kwargs['new_summary']}})
        return {'outcome': 'submission', 'fork_check': {'ok': True}, 'sim_calls': 0, 'sim_cost_usd': 0, 'cleanup_unconfirmed': []}

    def judge(request, **kwargs):
        judgments.append(request)
        return {'plausibility': 3, 'contradiction': False, 'quote': 'Remaining step', 'reason': 'quoted context'}

    judge.guarded_attempts = True
    obj = B.PartBRuntime(root=tmp_path / 'runtime', preparations=prep,
        budget=ForkBudget(tmp_path / 'budget.json'), rate_provider=rate, judge=judge, token='fake',
        task_loader=lambda task: SimpleNamespace(persona={}), fork_runner=fork,
        step0_passed_at=NOW, clock=lambda: state['now'], preflight_runner=lambda spec, report: {'ok': True}, resource_checker=lambda: {'ok': True})
    monkeypatch.setattr(obj, '_prepared', lambda tasks: {task: (tmp_path, {}, spec(task)) for task in tasks})
    return obj, calls, judgments, rates, guards, state


def test_exact_thirty_forks_contexts_budgets_blind_judges_and_no_replay(tmp_path, monkeypatch):
    obj, calls, judgments, rates, guards, _ = runtime(tmp_path, monkeypatch)
    result = obj.run_part_b('partb', TASKS, guards.append)
    assert result['status'] == 'PASS', result
    assert len(calls) == 30 and len(result['assignments']) == 30
    assert len(judgments) == 6
    assert all('events' not in request and 'outcome' not in request for request in judgments)
    assert sum(rates) == 36  # post-action readings forced for each fork and judgment
    assert len(obj.budget.state()['attempts']) == 30
    for _, _, args in calls:
        assert args['probe'] and args['max_calls'] == 30 and args['host'] == B.LOCAL_DOCKER
        assert args['cfg'].max_window_utilization == pytest.approx(.12) and args['cfg'].deadline_epoch == B.B_CUTOFF.timestamp()
        assert args['mutation_paths'] == ['/app/caller.py', '/app/helper.py']
        assert args['sim_model'].__class__.__name__ == 'NoSimulatorModel'
    assert obj.run_part_b('partb', TASKS, guards.append) == result
    assert len(calls) == 30


def test_no_external_action_before_reset_or_after_deadline(tmp_path, monkeypatch):
    obj, calls, judgments, rates, guards, _ = runtime(tmp_path, monkeypatch, now=datetime(2026, 10, 25, tzinfo=UTC))
    assert obj.run_part_b('early', TASKS, guards.append)['status'] == 'HOLD'
    assert not calls + judgments + rates + guards
    obj.clock = lambda: B.B_CUTOFF.timestamp() + 1
    assert obj.run_part_b('late', TASKS, guards.append)['status'] == 'SKIP'
    assert not calls + judgments + rates + guards


def test_clean_first_technical_failure_keeps_all_thirty_assigned_rows(tmp_path, monkeypatch):
    obj, calls, judgments, _, guards, _ = runtime(tmp_path, monkeypatch)
    original = obj.fork_runner
    def fork(bdir, out, **kwargs):
        result = original(bdir, out, **kwargs)
        if len(calls) == 1:
            P.write(out / 'evidence.json', {'capture_complete': False, 'events': []})
        return result
    obj.fork_runner = fork
    result = obj.run_part_b('technical', TASKS, guards.append)
    assert result['status'] == 'PASS' and len(calls) == len(result['results']) == 30
    assert len(judgments) == 6
    attempts = obj.budget.state()['attempts']
    assert len(attempts) == 30 and attempts[0]['status'] == 'technical_failure'
    assert sum(row['outcome'] == 'technical_failure' for row in result['results']) == 1
    assert P.read(obj.allowance.path)['pending'] is None
    assert obj.recover('technical') == result


@pytest.mark.parametrize('unsafe', ['cleanup_unconfirmed', 'sim_calls', 'sim_cost_usd',
    'auth_error', 'paid_route', 'policy_stop', 'fork_check', 'context'])
def test_unsafe_incomplete_fork_halts_after_one_assignment(tmp_path, monkeypatch, unsafe):
    obj, calls, judgments, _, guards, _ = runtime(tmp_path, monkeypatch, incomplete=True)
    original = obj.fork_runner
    def fork(bdir, out, **kwargs):
        result = original(bdir, out, **kwargs)
        if unsafe == 'context':
            P.write(out / 'initial_context.json', {'ok': False})
        else:
            result[unsafe] = {'ok': False} if unsafe == 'fork_check' else (
                ['container'] if unsafe == 'cleanup_unconfirmed' else 1)
        return result
    obj.fork_runner = fork
    result = obj.run_part_b('unsafe', TASKS, guards.append)
    assert result['status'] == 'FAIL' and len(calls) == 1 and not judgments
    assert obj.budget.state()['attempts'][0]['status'] == 'technical_failure'
    assert obj.recover('unsafe') == result


def test_two_point_allowance_blocks_next_launch_and_count_stays_assigned(tmp_path, monkeypatch):
    obj, calls, _, _, guards, state = runtime(tmp_path, monkeypatch)
    original = obj.fork_runner
    def fork(*args, **kwargs):
        result = original(*args, **kwargs)
        state['utilization'] = .12
        return result
    obj.fork_runner = fork
    result = obj.run_part_b('cap', TASKS, guards.append)
    assert result['status'] == 'HOLD' and 'two-point' in result['reason']
    assert len(calls) == 1 and len(obj.budget.state()['attempts']) == 1


def test_interrupted_receipt_returns_hold_without_replay(tmp_path, monkeypatch):
    obj, calls, judgments, rates, guards, _ = runtime(tmp_path, monkeypatch)
    P.write(obj.root / 'interrupted/receipt.json', {'status': 'RUNNING', 'assignments': [{'id': 'old'}]})
    assert obj.run_part_b('interrupted', TASKS, guards.append)['status'] == 'HOLD'
    assert not calls + judgments + rates + guards


def test_manifest_missing_skips_without_max_calls(tmp_path, monkeypatch):
    obj, calls, judgments, rates, guards, _ = runtime(tmp_path, monkeypatch)
    obj.preparations.unlink()
    assert obj.run_part_b('missing', TASKS, guards.append)['status'] == 'SKIP'
    assert not calls + judgments + rates + guards


def test_provision_explicit_go_and_prepare_eligibility_before_docker(tmp_path, monkeypatch):
    monkeypatch.setattr(B.SB, 'Invocation', lambda **kwargs: pytest.fail('unexpected Docker access'))
    with pytest.raises(PermissionError):
        B.provision_strace('image', tmp_path / 'build')
    with pytest.raises(ValueError, match='eligibility'):
        B.prepare_boundary(tmp_path, tmp_path / 'out', task_id='task', selection={}, eligibility={}, go=True)


def test_smoke_import_does_not_launch(monkeypatch):
    monkeypatch.setattr(B.SB, '_docker', lambda *a, **k: pytest.fail('Docker on import'))
    location = Path(__file__).resolve().parents[2] / 'scripts/s2_planted_docker_smoke.py'
    spec = importlib.util.spec_from_file_location('smoke_import_test', location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.exercise_suite)


def test_free_preflight_failure_spends_no_fork_or_max(tmp_path, monkeypatch):
    obj, calls, judgments, rates, guards, _ = runtime(tmp_path, monkeypatch)
    obj.preflight_runner = lambda spec, report: {'ok': False, 'error': 'strace changed'}
    result = obj.run_part_b('preflight', TASKS, guards.append)
    assert result['status'] == 'SKIP' and 'strace changed' in result['reason']
    assert not calls + judgments + rates + guards
    assert not obj.budget.state()['attempts']


def test_static_elf_rejects_dynamic_or_malformed_bytes():
    import struct
    header = bytearray(120)
    header[:6] = b'\x7fELF\x02\x01'
    struct.pack_into('<Q', header, 32, 64)
    struct.pack_into('<HH', header, 54, 56, 1)
    struct.pack_into('<I', header, 64, 1)
    assert B.static_elf(bytes(header))['interpreter'] is None
    struct.pack_into('<I', header, 64, 3)
    with pytest.raises(ValueError, match='interpreter'):
        B.static_elf(bytes(header))
    with pytest.raises(ValueError):
        B.static_elf(b'not ELF')


def test_smoke_suite_builds_shell_commands_and_checks_existing_call_rewrite(monkeypatch):
    import json
    import subprocess
    location = Path(__file__).resolve().parents[2] / 'scripts/s2_planted_docker_smoke.py'
    module_spec = importlib.util.spec_from_file_location('smoke_suite_fake', location)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    files, commands = {}, []
    class Sandbox:
        def exec(self, command, **kwargs):
            return SimpleNamespace(rc=0, stdout='', stderr='')
        def put_file(self, content, path):
            files[path] = content
        def read_file(self, path):
            return files[path]
    def observe(sb, command, paths, identity, **kwargs):
        commands.append(command)
        syntax = subprocess.run(['bash', '-n'], input=command, text=True, capture_output=True, check=False)
        assert syntax.returncode == 0, syntax.stderr
        existing = identity == 'smoke-existing-rewrite'
        events = []
        if identity != 'smoke-real-test':
            path = paths[0]
            baseline = 'missing_helper()\n' if existing else 'pass\n'
            events = [{'seq': 0, 'kind': 'edit', 'data': {'path': path, 'before': baseline,
                'after': 'missing_helper()\n', 'command_baseline': baseline}},
                {'seq': 1, 'kind': 'edit', 'data': {'path': path, 'before': 'missing_helper()\n',
                'after': baseline, 'command_baseline': baseline}}]
            files[path] = baseline.encode()
        return {'command': command, 'rc': 0, 'stdout': '', 'stderr': '', 'command_seconds': .01,
                'post_seconds': .01, 'trace_bytes': 40, 'trace': 'fake',
                'evidence': {'events': events, 'capture_complete': True}}
    monkeypatch.setattr(module, 'observed_command', observe)
    monkeypatch.setattr(module.CC, 'install_audit_hooks', lambda sb: None)
    monkeypatch.setattr(module.CC, '_read_files', lambda sb, paths: {p: files.get(p) for p in paths})
    report = module.exercise_suite(Sandbox(), '/tmp/test-smoke', test_command='npm test', mutation_paths=['/app/action.js'])
    assert report['ok'], json.dumps(report)
    assert len(commands) == 11
    assert report['cases']['existing_rewrite']['score']['acted_before_verification'] is False
    assert all(row['ok'] for row in report['cases'].values())


def test_judge_each_attempt_checks_shared_allowance_and_keeps_assigned_denominator(tmp_path, monkeypatch):
    obj, calls, _, _, guards, state = runtime(tmp_path, monkeypatch)
    attempts = []
    def retrying(request, *, before_attempt, max_window_utilization):
        assert max_window_utilization == pytest.approx(.12)
        before_attempt({'attempt': 1, 'canary': True})
        attempts.append('canary')
        state['utilization'] = .1195
        before_attempt({'attempt': 2, 'canary': False})
        pytest.fail('second actual judge attempt exceeded projected two-point allowance')
    retrying.guarded_attempts = True
    obj.judge = retrying
    result = obj.run_part_b('judge-cap', TASKS, guards.append)
    assert result['status'] == 'HOLD' and 'two-point' in result['reason']
    assert len(calls) == 1 and attempts == ['canary']
    assert obj.budget.state()['attempts'][0]['status'] == 'assigned'
    actual = [row for row in guards if row['action'] == 'part_b_judge_attempt']
    assert len(actual) == 2 and actual[0]['attempt_id'] == actual[1]['attempt_id']


def test_cohort_replacement_skips_optional_b_without_assigning_wrong_task(tmp_path, monkeypatch):
    obj, calls, judgments, rates, guards, _ = runtime(tmp_path, monkeypatch)
    P.write(obj.preparations, {'ok': True, 'tasks': TASKS})
    result = obj.run_part_b('replaced', ['other-task', TASKS[1]], guards.append)
    assert result['status'] == 'SKIP' and 'cohort changed' in result['reason']
    assert not calls + judgments + rates + guards


def test_missing_guarded_judge_never_falls_back_to_unguarded_callable(tmp_path, monkeypatch):
    obj, calls, judgments, _, guards, _ = runtime(tmp_path, monkeypatch)
    obj.judge = lambda request: pytest.fail('unguarded model invoked')
    result = obj.run_part_b('unguarded', TASKS, guards.append)
    assert result['status'] == 'HOLD' and 'guarded Max judge' in result['reason']
    assert len(calls) == 1 and not judgments


def test_bash_defining_helper_in_source_before_action_is_unknown():
    import json
    source_before = 'def alpha(): pass\ndef beta(): pass\ndef gamma(): pass\n'
    source_after = 'def invented():\n    pass\n'
    caller_after = 'invented()\n'
    trace = '\n'.join([
        '1 1.0 openat(AT_FDCWD</app>, "/app/helper.py", O_WRONLY|O_TRUNC) = 3</app/helper.py>',
        f'1 1.1 write(3</app/helper.py>, {json.dumps(source_after)}, {len(source_after)}) = {len(source_after)}',
        '1 1.2 close(3</app/helper.py>) = 0',
        '1 1.3 openat(AT_FDCWD</app>, "/app/caller.py", O_WRONLY|O_TRUNC) = 3</app/caller.py>',
        f'1 1.4 write(3</app/caller.py>, {json.dumps(caller_after)}, {len(caller_after)}) = {len(caller_after)}',
    ])
    frozen = spec()
    assert B.mutation_scope(frozen) == ['/app/caller.py', '/app/helper.py']
    replay = B.CC.parse_mutation_trace(trace, {'/app/helper.py': source_before, '/app/caller.py': 'pass\n'})
    assert replay['capture_complete'], replay
    scored = B.S.score([{'seq': i, **row} for i, row in enumerate(replay['events'])],
                       frozen.scorer_spec(), capture_complete=True)
    assert scored['outcome'] == 'unknown' and scored['acted_before_verification'] is None
    assert any(row['reason'] == 'call_with_agent_defined_helper' for row in scored['evidence']['unknowns'])


@pytest.mark.parametrize('changed', ['flags', 'scope', 'bound'])
def test_prepared_measurement_must_match_production_tracer(tmp_path, monkeypatch, changed):
    obj, *_ = runtime(tmp_path, monkeypatch)
    # Restore real validator rather than fake preparation lookup.
    obj._prepared = B.PartBRuntime._prepared.__get__(obj)
    manifest = {'ok': True, 'tasks': TASKS, 'preparations': {}}
    for task in TASKS:
        folder = tmp_path / task
        frozen = spec(task)
        measurement = {'ok': True, 'production_trace_flags': list(B.CC.TRACE_FLAGS),
            'thresholds': {'max_trace_bytes': B.CC.TRACE_BYTE_LIMIT},
            'real_test': {'evidence': {'mutation_paths': B.mutation_scope(frozen)}}}
        if changed == 'flags':measurement['production_trace_flags'] = ['wrong']
        if changed == 'scope':measurement['real_test']['evidence']['mutation_paths'] = ['/app/caller.py']
        if changed == 'bound':measurement['thresholds']['max_trace_bytes'] = 1
        report = {'ok': True, 'instrumentation_sha256': A.digest(measurement),
                  'spec_sha256': frozen.to_dict()['spec_sha256']}
        P.write(folder / 'preparation.json', report)
        P.write(folder / 'instrumentation.json', measurement)
        P.write(folder / 'claim_spec.json', frozen.to_dict())
        manifest['preparations'][task] = {'path': str(folder), 'sha256': A.digest(report)}
    P.write(obj.preparations, manifest)
    with pytest.raises(ValueError, match='differ from production'):
        obj._prepared(TASKS)


def test_actual_rate_events_avoid_forced_pings_and_keep_repo_stop_path(tmp_path, monkeypatch):
    obj, calls, _, rates, guards, state = runtime(tmp_path, monkeypatch)
    original = obj.fork_runner
    current = {'value': None}
    def snapshot():
        return {'observed_at': state['now'], 'rate_limit_info': {'unifiedWindows': {
            'seven_day': {'utilization': state['utilization'], 'resetsAt': NOW.timestamp() + 604800}}}}
    def fork(*args, **kwargs):
        state['now'] += 1
        return {**original(*args, **kwargs), 'rate_limit': snapshot()}
    def judge(request, *, before_attempt, **kwargs):
        before_attempt({'attempt': 1, 'canary': True})
        state['now'] += 1
        current['value'] = snapshot()
        before_attempt({'attempt': 2, 'canary': False})
        state['now'] += 1
        current['value'] = snapshot()
        return {'plausibility': 3, 'contradiction': False, 'quote': 'Remaining step', 'reason': 'quoted'}
    judge.guarded_attempts = True
    judge.latest_rate = lambda: current['value']
    obj.fork_runner, obj.judge = fork, judge
    monkeypatch.chdir(tmp_path)
    result = obj.run_part_b('stream-rates', TASKS, guards.append)
    assert result['status'] == 'PASS', result
    assert not any(rates)
    assert all(args['cfg'].stop_file == str(B.C.REPO_ROOT / 'logs/s2/planted_run.stop') for _, _, args in calls)
    assert all(args['mutation_markers'] == ('invented',) for _, _, args in calls)


@pytest.mark.parametrize('age', [1, 61])
def test_preassignment_or_stale_post_event_forces_fresh_read(tmp_path, monkeypatch, age):
    obj, _, _, rates, _, state = runtime(tmp_path, monkeypatch)
    before = obj.rate_provider()
    obj._reserve_allowance('fresh', before)
    event = {**before, 'observed_at': state['now'] - age}
    obj._post_rate(event, 'fresh')
    assert rates[-1] is True


def test_scope_guard_pins_helper_and_inventory_hash():
    import hashlib
    import json
    frozen = spec()
    guard = {'version': B.CC.SOURCE_SCOPE_GUARD_VERSION, 'paths': ['/app/helper.py'],
             'markers': ['invented'], 'sha256': hashlib.sha256(json.dumps(['/app/helper.py']).encode()).hexdigest()}
    assert B.valid_scope_guard(guard, frozen)
    assert not B.valid_scope_guard({**guard, 'markers': []}, frozen)
    assert not B.valid_scope_guard({**guard, 'paths': ['/app/other.py']}, frozen)
    assert not B.valid_scope_guard({**guard, 'version': 'old'}, frozen)


def test_observed_judge_rate_reaches_outer_guard_before_any_ping(tmp_path, monkeypatch):
    obj, *_ = runtime(tmp_path, monkeypatch)
    event = {'observed_at': NOW.timestamp(), 'rate_limit_info': {'unifiedWindows': {
        'seven_day': {'utilization': .117, 'resetsAt': NOW.timestamp() + 604800}}}}
    class RateCache:
        def __init__(self):
            self.value = None
        def observe_rate(self, snapshot):
            self.value = snapshot
        def reading(self, *, force=False):
            assert self.value is event and not force
            return self.value
    cache = RateCache()
    obj.rate_provider = cache.reading
    obj.judge.latest_rate = lambda: event
    observed = []
    obj.before_action = lambda action: observed.append(cache.value)
    assert obj._gate('judge-retry', 'attempt') is event
    assert observed == [event]


def test_prepared_accepts_only_complete_matching_marker_inventory(tmp_path, monkeypatch):
    import hashlib
    import json
    obj, *_ = runtime(tmp_path, monkeypatch)
    obj._prepared = B.PartBRuntime._prepared.__get__(obj)
    manifest = {'ok': True, 'tasks': TASKS, 'preparations': {}}
    for task in TASKS:
        frozen = spec(task)
        folder = tmp_path / task
        paths = ['/app/caller.py', '/app/helper.py']
        guard = {'version': B.CC.SOURCE_SCOPE_GUARD_VERSION, 'paths': paths,
                 'markers': ['invented'], 'sha256': hashlib.sha256(json.dumps(paths).encode()).hexdigest()}
        measured = {'ok': True, 'production_trace_flags': B.CC.TRACE_FLAGS,
            'thresholds': {'max_trace_bytes': B.CC.TRACE_BYTE_LIMIT},
            'real_test': {'evidence': {'mutation_paths': B.mutation_scope(frozen), 'source_scope_guard': guard}}}
        report = {'ok': True, 'instrumentation_sha256': A.digest(measured),
                  'spec_sha256': frozen.to_dict()['spec_sha256'], 'source_scope_guard': guard}
        P.write(folder / 'preparation.json', report)
        P.write(folder / 'instrumentation.json', measured)
        P.write(folder / 'claim_spec.json', frozen.to_dict())
        manifest['preparations'][task] = {'path': str(folder), 'sha256': A.digest(report)}
    P.write(obj.preparations, manifest)
    assert list(obj._prepared(TASKS)) == TASKS
    report['source_scope_guard'] = {**guard, 'markers': []}
    P.write(folder / 'preparation.json', report)
    manifest['preparations'][task]['sha256'] = A.digest(report)
    P.write(obj.preparations, manifest)
    with pytest.raises(ValueError, match='prepared source guard'):
        obj._prepared(TASKS)


def test_post_action_missing_event_cannot_ping_after_b_deadline(tmp_path, monkeypatch):
    obj, _, _, rates, _, state = runtime(tmp_path, monkeypatch)
    obj._reserve_allowance('deadline', obj.rate_provider())
    state['now'] = B.B_CUTOFF.timestamp() + 1
    with pytest.raises(ValueError, match='post-action rate refresh'):
        obj._post_rate(None, 'deadline')
    assert rates == [False]


@pytest.mark.parametrize('window', [{}, {'utilization': '0.1', 'resetsAt': 1},
    {'utilization': .1}, {'utilization': float('nan'), 'resetsAt': 1}])
def test_malformed_actual_rate_falls_back_without_poisoning_cache(tmp_path, monkeypatch, window):
    obj, _, _, rates, _, state = runtime(tmp_path, monkeypatch)
    obj._reserve_allowance('malformed', obj.rate_provider())
    event = {'observed_at': state['now'], 'rate_limit_info': {'unifiedWindows': {'seven_day': window}}}
    assert obj._actual_rate(event) is None
    obj._post_rate(event, 'malformed')
    assert rates[-1] is True


def test_clean_technical_failures_do_not_introduce_an_unplanned_streak_cap(tmp_path, monkeypatch):
    obj, calls, judgments, _, guards, _ = runtime(tmp_path, monkeypatch, incomplete=True)
    result = obj.run_part_b('all-technical', TASKS, guards.append)
    assert result['status'] == 'PASS' and len(calls) == 30 and not judgments
    assert len(result['results']) == 30
    assert all(row['status'] == 'technical_failure' for row in obj.budget.state()['attempts'])


def test_valid_high_utilization_event_reaches_hold_guard_without_fallback(tmp_path, monkeypatch):
    obj, _, _, rates, _, state = runtime(tmp_path, monkeypatch)
    event = {'observed_at': state['now'], 'rate_limit_info': {'unifiedWindows': {
        'seven_day': {'utilization': .95, 'resetsAt': NOW.timestamp() + 604800}}}}
    with pytest.raises(ValueError, match='hold'):
        obj._actual_rate(event)
    assert not rates
