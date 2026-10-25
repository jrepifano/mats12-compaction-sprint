"""Part B preparation and durable, local-only runtime. Importing starts nothing.

Preparation dependencies (not model/runtime dependencies): tree-sitter==0.25.2,
tree-sitter-rust==0.24.0, tree-sitter-javascript==0.25.0, from their official PyPI
projects. Parse errors and unsupported/dynamic binding forms fail closed.
Static strace is built from the official release in a disposable existing image;
no sudo, image mutation, credential mount, or model invocation is involved.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import io
import json
import os
import re
import shlex
import shutil
import struct
import tarfile
import time
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from compaction.deepswe import sandbox as SB
from compaction.s2 import config as C
from compaction.s2 import planted_arms as A
from compaction.s2 import planted_score as S
from compaction.s2 import planted_step0 as P
from compaction.s2.native import claude_code as CC
from compaction.s2.native import common as N
from compaction.s2.planted_config import (
    B_CUTOFF,
    B_DROP_IF_STEP0_AFTER,
    RESET,
    ForkBudget,
    PartBAllowance,
    part_b_guard,
    part_b_tasks,
)
from compaction.s2.planted_retention import SEED, order_key

LOCAL_DOCKER = 'unix:///var/run/docker.sock'


def local_docker(args, **kwargs):
    with SB.using_host(LOCAL_DOCKER):
        return SB._docker(args, **kwargs)


PARSER_VERSIONS = {'tree-sitter': '0.25.2', 'tree-sitter-rust': '0.24.0',
                   'tree-sitter-javascript': '0.25.0'}
STRACE_URL = 'https://strace.io/files/6.11/strace-6.11.tar.xz'
STRACE_SHA256 = '83262583a3529f02c3501aa8b8ac772b4cbc03dc934e98bab6e4883626e283a5'
STRACE_HASH_SOURCE = 'https://patchwork.yoctoproject.org/project/oe-core/patch/20240930222913.3981407-1-raj.khem%40gmail.com/'
STRACE_BUILD_DOC = 'https://github.com/strace/strace/blob/v6.11/dist/INSTALL'


def durable(path: Path, value: dict, *, new=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if new and path.exists():
        raise FileExistsError(path)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w') as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def checked(sb, command: str, *, timeout=60, workdir='/app'):
    result = sb.exec(command, timeout_s=timeout, workdir=workdir)
    if result.rc:
        raise RuntimeError(f'command failed rc={result.rc}: {command}: {result.stderr[-1500:]}')
    return result


def tree_definitions(source: A.SourceSnapshot) -> A.DefinitionInventory:
    """Exhaustive syntactic module bindings; reject dynamic exports, wildcards and item macros.

    Rust impl members are not module-level definitions. JavaScript property assignments
    are not lexical declarations; named CommonJS exports are included explicitly.
    No regular-expression fallback is used when parsing is unavailable or incomplete.
    """
    import tree_sitter_javascript
    import tree_sitter_rust
    from tree_sitter import Language, Parser
    for package, wanted in PARSER_VERSIONS.items():
        if importlib.metadata.version(package) != wanted:
            raise ValueError(f'preparation requires {package}=={wanted}')
    rust = source.path.endswith('.rs')
    if not rust and not source.path.endswith('.js'):
        raise ValueError('explicit extractor supports .rs/.js only')
    language = tree_sitter_rust if rust else tree_sitter_javascript
    root = Parser(Language(language.language())).parse(source.text.encode()).root_node
    if root.has_error:
        raise ValueError('source AST contains parse errors')
    names = set()

    def add(node):
        if node is None or node.type not in ('identifier', 'type_identifier', 'property_identifier', 'shorthand_property_identifier_pattern'):
            raise ValueError('unsupported binding pattern')
        names.add(node.text.decode())

    def pattern(node):
        if node.type in ('identifier', 'shorthand_property_identifier_pattern'):
            add(node)
        elif node.type in ('array_pattern', 'object_pattern', 'rest_pattern'):
            for child in node.named_children:
                pattern(child)
        elif node.type in ('pair_pattern', 'assignment_pattern', 'object_assignment_pattern'):
            pattern(node.child_by_field_name('value') or node.child_by_field_name('left'))
        else:
            raise ValueError(f'unsupported declaration binding {node.type}')

    def rust_use(node):
        if node.type in ('identifier', 'type_identifier'):
            add(node)
        elif node.type == 'scoped_identifier':
            add(node.child_by_field_name('name'))
        elif node.type == 'use_as_clause':
            add(node.child_by_field_name('alias'))
        elif node.type in ('use_list', 'scoped_use_list'):
            for child in node.named_children:
                if node.type == 'scoped_use_list' and child != node.child_by_field_name('list'):
                    continue
                rust_use(child)
        else:
            raise ValueError(f'unsupported Rust import {node.type}')

    def visit(node):
        kind = node.type
        if kind in ('line_comment', 'block_comment', 'comment', 'empty_statement', 'shebang'):
            return
        if rust:
            if kind in ('function_item', 'struct_item', 'enum_item', 'union_item', 'type_item',
                        'const_item', 'static_item', 'trait_item', 'mod_item', 'macro_definition'):
                add(node.child_by_field_name('name'))
            elif kind == 'use_declaration':
                rust_use(node.child_by_field_name('argument'))
            elif kind in ('impl_item', 'attribute_item', 'inner_attribute_item'):
                if kind.endswith('attribute_item') and any(x in node.text.decode() for x in ('include', 'macro_use')):
                    raise ValueError('attribute may introduce module bindings')
            else:
                raise ValueError(f'unsupported Rust top-level item {kind}')
        elif kind in ('function_declaration', 'generator_function_declaration', 'class_declaration'):
            add(node.child_by_field_name('name'))
        elif kind in ('lexical_declaration', 'variable_declaration'):
            for child in node.named_children:
                if child.type != 'variable_declarator':
                    raise ValueError('unsupported JS declaration')
                pattern(child.child_by_field_name('name'))
        elif kind == 'export_statement':
            declaration = node.child_by_field_name('declaration')
            if declaration is None:
                raise ValueError('re-exports need authoritative resolution')
            visit(declaration)
        elif kind == 'expression_statement':
            expression = node.named_children[0]
            if expression.type == 'string':  # strict-mode directive
                return
            if expression.type != 'assignment_expression':
                raise ValueError('top-level executable expression may mutate exports')
            left = expression.child_by_field_name('left')
            spelling = left.text.decode()
            if spelling == 'module.exports':
                right = expression.child_by_field_name('right')
                if right.type not in ('identifier', 'function_expression', 'arrow_function', 'object'):
                    raise ValueError('dynamic CommonJS export')
                if right.type == 'object':
                    for prop in right.named_children:
                        if prop.type == 'shorthand_property_identifier':
                            names.add(prop.text.decode())
                        elif prop.type in ('pair', 'method_definition'):
                            add(prop.child_by_field_name('key') or prop.child_by_field_name('name'))
                        else:
                            raise ValueError('dynamic CommonJS object export')
            elif spelling.startswith('exports.') and left.type == 'member_expression':
                add(left.child_by_field_name('property'))
            elif left.type == 'member_expression' and '.prototype.' in spelling:
                return  # prototype members are not module bindings
            else:
                raise ValueError('unsupported top-level assignment')
        else:
            raise ValueError(f'unsupported JS top-level item {kind}')

    for node in root.named_children:
        visit(node)
    if any(node.type == 'call_expression' and node.child_by_field_name('function') is not None
           and node.child_by_field_name('function').text in (b'eval', b'Function')
           for node in walk(root)):
        raise ValueError('dynamic namespace construction')
    extractor = f'tree-sitter-{ "rust-0.24.0" if rust else "javascript-0.25.0"}-module-bindings-v1'
    return A.DefinitionInventory(tuple(sorted(names)), source.sha256, extractor, True)


def walk(node):
    yield node
    for child in node.named_children:
        yield from walk(child)


def capture_source(sb, *, bdir: Path, path: str) -> A.SourceSnapshot:
    metadata = P.read(Path(bdir) / 'boundary/boundary.json')
    path = A.repo_path(path)
    absolute = '/app/' + path
    read_cmd, resolve_cmd, hash_cmd = (shlex.join([tool, *args, absolute]) for tool, args in
                                      [('cat', []), ('readlink', ['-f']), ('sha256sum', [])])
    read = checked(sb, read_cmd)
    resolve = checked(sb, resolve_cmd)
    hashed = checked(sb, hash_cmd)
    return A.SourceSnapshot(metadata['tag'], path, resolve.stdout.strip(), read.stdout,
        read_cmd, read.rc, True, metadata['fingerprint_sha256'], A.digest(metadata),
        hashed.stdout.split()[0], resolve_cmd, resolve.rc, resolve.stdout,
        hash_cmd, hashed.rc, hashed.stdout)


def freeze_source(sb, *, bdir: Path, task_id: str, selection: dict) -> A.ClaimSpec:
    """Only a predeclared exact next-step quotation and complete kept-source inventory qualify."""
    source = capture_source(sb, bdir=bdir, path=selection['source_file'])
    actions = []
    for path in selection['action_files']:
        path = A.repo_path(path)
        command = shlex.join(['readlink', '-f', '/app/' + path])
        result = checked(sb, command)
        actions.append(A.ActionPath(path, result.stdout.strip(), source.kept_image,
                                   source.boundary_fingerprint, command, result.rc))
    helper = selection['helper']
    command = shlex.join(['rg', '--hidden', '--no-ignore', '--follow', '-F', '--', helper, '/app'])
    result = sb.exec(command, timeout_s=180)
    if result.rc != 1 or result.stdout or result.stderr:
        raise ValueError(f'repo helper absence unproven: rc={result.rc}, stderr={result.stderr[-1500:]!r}, stdout={result.stdout[-1500:]!r}')
    absence = A.RepoAbsence(helper, command, result.rc, result.stdout, result.stderr,
                           source.kept_image, source.boundary_fingerprint)
    spec = A.freeze_claim_from_boundary(bdir, task_id=task_id, source=source, helper=helper,
        action_files=selection['action_files'], remaining_step=selection['remaining_step'],
        eligibility_quote=selection['eligibility_quote'], next_decision_confirmed=True,
        action_provenance=actions, repo_absence=absence, extractor=tree_definitions)
    # Scope also pins every callsite's starting bytes, before any fork or manipulation.
    return spec


def static_elf(binary: bytes) -> dict:
    """Verify no runtime interpreter or shared dependencies directly from ELF64 headers."""
    if binary[:6] != b'\x7fELF\x02\x01' or len(binary) < 64:
        raise ValueError('expected little-endian ELF64 strace')
    offset = struct.unpack_from('<Q', binary, 32)[0]
    size, count = struct.unpack_from('<HH', binary, 54)
    if size != 56 or not count or offset + size * count > len(binary):
        raise ValueError('invalid ELF program-header table')
    dynamic = []
    for index in range(count):
        kind, _, start, _, _, length, _, _ = struct.unpack_from('<IIQQQQQQ', binary, offset + index * size)
        if kind == 3:
            raise ValueError('strace has a dynamic ELF interpreter')
        if kind == 2:
            if start + length > len(binary) or length % 16:
                raise ValueError('invalid ELF dynamic section')
            dynamic.extend(struct.unpack_from('<qQ', binary, pos)[0] for pos in range(start, start + length, 16))
    if 1 in dynamic:
        raise ValueError('strace requires a shared library (DT_NEEDED)')
    return {'format': 'ELF64 little endian', 'interpreter': None, 'needed_libraries': [],
            'method': 'ELF program headers and dynamic entries'}


RIPGREP_URL = 'https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/ripgrep-14.1.1-x86_64-unknown-linux-musl.tar.gz'
RIPGREP_SHA256 = '4cf9f2741e6c465ffdb7c26f38056a59e2a2544b51f7cc128ef28337eeae4d8e'


def preparation_ripgrep(sb) -> dict:
    """Pinned static search tool, only in this disposable preparation container."""
    with urllib.request.urlopen(RIPGREP_URL, timeout=60) as response:
        archive = response.read(8 * 1024 * 1024)
    if hashlib.sha256(archive).hexdigest() != RIPGREP_SHA256:
        raise ValueError('official ripgrep archive hash mismatch')
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as package:
        member = package.getmember('ripgrep-14.1.1-x86_64-unknown-linux-musl/rg')
        if not member.isfile() or member.size > 16 * 1024 * 1024:
            raise ValueError('invalid static ripgrep archive member')
        binary = package.extractfile(member).read()
    linkage = static_elf(binary)
    sb.put_file(binary, '/usr/local/bin/rg')
    checked(sb, 'chmod 755 /usr/local/bin/rg')
    resolved = checked(sb, 'command -v rg').stdout.strip()
    if resolved != '/usr/local/bin/rg':
        raise ValueError('preparation search PATH differs from pinned tool')
    return {'url': RIPGREP_URL, 'archive_sha256': RIPGREP_SHA256,
            'hash_source': RIPGREP_URL + '.sha256', 'binary_sha256': hashlib.sha256(binary).hexdigest(),
            'linkage': linkage, 'version': checked(sb, 'rg --version').stdout,
            'scope': 'disposable preparation container only; kept image unchanged'}


def provision_strace(image: str, output: Path, *, go=False) -> dict:
    """Build a pinned static executable in a disposable image; publish only after verification.

    The existing CLI volume is the sole destination. Its claude executable is never changed.
    Existing strace with a different hash is not overwritten. No sudo or package install.
    """
    if not go:
        raise PermissionError('free preparation requires explicit --go')
    output = Path(output)
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    report = {'ok': False, 'started_at': N.utc(), 'source_url': STRACE_URL,
              'source_sha256': STRACE_SHA256, 'hash_source': STRACE_HASH_SOURCE,
              'build_instructions': STRACE_BUILD_DOC, 'model_calls': 0}
    inv = SB.Invocation(purpose='planted-strace-build', host=LOCAL_DOCKER)
    try:
        image_id = json.loads(local_docker(['image', 'inspect', image], timeout=30).stdout)[0]['Id']
        local_docker(['volume', 'inspect', CC.CLI_VOLUME], timeout=30)
        from compaction.s2.planted_eligibility import resource_guard
        resource_guard(LOCAL_DOCKER)
        with urllib.request.urlopen(STRACE_URL, timeout=60) as response:
            archive = response.read(8 * 1024 * 1024)
        if hashlib.sha256(archive).hexdigest() != STRACE_SHA256:
            raise ValueError('official strace archive hash mismatch')
        (output / 'strace-6.11.tar.xz').write_bytes(archive)
        sb = N.start_task_container(inv, image_id, network='none', cpus=2, memory_mb=2048,
            lifetime_s=1200, extra_args=('--pull', 'never', '--entrypoint', '',
                                       '-v', f'{CC.CLI_VOLUME}:/strace-destination'))
        sb.put_file(archive, '/tmp/strace.tar.xz')
        configure = "./configure --enable-mpers=no --without-libunwind --without-libdw --without-libiberty LDFLAGS='-static -pthread'"
        command = f'mkdir /tmp/strace-build && tar -xJf /tmp/strace.tar.xz -C /tmp/strace-build --strip-components=1 && cd /tmp/strace-build && {configure} && make -j2'
        build = sb.exec(command, timeout_s=1050, workdir='/tmp')
        (output / 'build.stdout').write_text(build.stdout)
        (output / 'build.stderr').write_text(build.stderr)
        report.update(image_id=image_id, build_command=command, build_rc=build.rc)
        if build.rc:
            raise RuntimeError('strace static build failed; see build.stdout/build.stderr')
        binary = sb.read_file('/tmp/strace-build/src/strace')
        binary_sha = hashlib.sha256(binary).hexdigest()
        linkage = static_elf(binary)
        preflight = checked(sb, CC.trace_command('/tmp/strace-build/src/strace', '/dev/null', 'true'))
        install = ('if [ -e /strace-destination/strace ]; then '
                   f'test "$(sha256sum /strace-destination/strace | cut -d " " -f1)" = {binary_sha}; '
                   'else cp /tmp/strace-build/src/strace /strace-destination/strace && chmod 755 /strace-destination/strace; fi')
        checked(sb, install)
        (output / 'strace').write_bytes(binary)
        report.update(ok=True, binary_sha256=binary_sha, bytes=len(binary), linkage=linkage,
                      version=checked(sb, '/strace-destination/strace --version').stdout,
                      preflight_rc=preflight.rc, volume=CC.CLI_VOLUME)
    except Exception as exc:  # noqa: BLE001 -- persist preparation failures, never spend a fork
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        try:
            report['cleanup_unconfirmed'] = inv.cleanup()
        except Exception as exc:  # noqa: BLE001 -- persist cleanup uncertainty
            report['cleanup_unconfirmed'] = [str(exc)]
        report['ok'] = report['ok'] and not report['cleanup_unconfirmed']
        report['ended_at'] = N.utc()
        durable(output / 'provision.json', report)
    return report


def inspect_sources(bdir: Path, output: Path, *, prefixes: list[str], go=False) -> dict:
    """Read candidate production modules from one existing image, without changing /app."""
    if not go:
        raise PermissionError('free source inspection requires explicit --go')
    if Path(output).exists():
        raise FileExistsError(output)
    b = P.read(Path(bdir) / 'boundary/boundary.json')
    report = {'ok': False, 'boundary': str(Path(bdir).resolve()), 'model_calls': 0, 'sources': {}}
    inv = SB.Invocation(purpose='planted-source-inspect', host=LOCAL_DOCKER)
    try:
        from compaction.s2.planted_eligibility import resource_guard
        resource_guard(LOCAL_DOCKER)
        image = json.loads(local_docker(['image', 'inspect', b['tag']], timeout=30).stdout)[0]['Id']
        report['image_id'] = image
        sb = N.start_task_container(inv, image, network='none', cpus=1, memory_mb=1024,
            lifetime_s=300, extra_args=('--pull', 'never', '--entrypoint', ''))
        files = checked(sb, 'git ls-files').stdout.splitlines()
        report['tracked_files'] = files
        for path in files:
            if not any(path.startswith(prefix) for prefix in prefixes) or not path.endswith(('.rs', '.js', '.json')):
                continue
            read = checked(sb, 'cat ' + shlex.quote('/app/' + path))
            if len(read.stdout.encode()) > 32000:
                continue
            row = {'text': read.stdout, 'sha256': A.text_digest(read.stdout)}
            if path.endswith(('.rs', '.js')):
                try:
                    captured = capture_source(sb, bdir=bdir, path=path)
                    row['definitions'] = list(tree_definitions(captured).names)
                except ValueError as exc:
                    row['inventory_error'] = str(exc)
            report['sources'][path] = row
        report['ok'] = True
    except Exception as exc:  # noqa: BLE001 -- source inspection is evidence, never a fork
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        try:
            report['cleanup_unconfirmed'] = inv.cleanup()
        except Exception as exc:  # noqa: BLE001 -- fail closed cleanup
            report['cleanup_unconfirmed'] = [str(exc)]
        report['ok'] = report['ok'] and not report['cleanup_unconfirmed']
        durable(output, report, new=True)
    return report


def mutation_scope(spec: A.ClaimSpec) -> list[str]:
    """Observe source definitions as well as calls; scientific action targets stay distinct."""
    return sorted({spec.source.resolved_path, *(item.resolved_path for item in spec.action_provenance)})


def valid_scope_guard(guard, spec):
    return (isinstance(guard, dict) and guard.get('version') == CC.SOURCE_SCOPE_GUARD_VERSION
            and guard.get('markers') == [spec.helper] and isinstance(guard.get('paths'), list)
            and guard.get('sha256') == hashlib.sha256(json.dumps(guard['paths']).encode()).hexdigest())


def prepare_boundary(bdir: Path, output: Path, *, task_id: str, selection: dict,
                     eligibility: dict, go=False, smoke_runner=None) -> dict:
    """Freeze one measured kept source before experiments. All work is model-free.

    The selection's exact next-decision quotation is supplied by the pre-run source audit,
    not inferred from future outcomes. The live eligibility artifact must certify this source.
    """
    if not go:
        raise PermissionError('free preparation requires explicit --go')
    if not eligibility.get('eligible') or eligibility.get('task_id') != task_id:
        raise ValueError('live image/fork-check eligibility is required')
    if Path(eligibility.get('boundary', '')).resolve() != Path(bdir).resolve():
        raise ValueError('eligibility belongs to a different source boundary')
    output = Path(output)
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    report = {'task_id': task_id, 'ok': False, 'selection': selection, 'model_calls': 0,
              'eligibility_sha256': A.digest(eligibility), 'started_at': N.utc()}
    inv = SB.Invocation(purpose='planted-partb-prepare', host=LOCAL_DOCKER)
    try:
        census = P.inspect_boundary(Path(bdir))
        if not census['offline_eligible']:
            raise ValueError(f'offline source failed: {census["problems"]}')
        copied = P.snapshot_boundary(Path(bdir), output / 'source', expected_hashes=census['hashes'])
        shutil.copy2(Path(bdir) / 'run.json', copied / 'run.json')
        b = P.read(copied / 'boundary/boundary.json')
        image = json.loads(local_docker(['image', 'inspect', b['tag']], timeout=30).stdout)[0]['Id']
        if image != eligibility['image_before']['image_id']:
            raise ValueError('image changed since eligibility')
        from compaction.s2 import interact as I
        from compaction.s2.planted_eligibility import resource_guard
        resource_guard(LOCAL_DOCKER)
        sb = N.start_task_container(inv, image, network='none', cpus=2, memory_mb=4096,
            lifetime_s=900, extra_args=(*CC.run_args(), '--pull', 'never', '--entrypoint', ''))
        fingerprint = I.sha256(I.fingerprint(I.guarded_runner(sb), int(b.get('fingerprint_version') or 1)))
        if fingerprint != b['fingerprint_sha256']:
            raise ValueError('preparation container workspace differs from boundary')
        report['search_tool'] = preparation_ripgrep(sb)
        spec = freeze_source(sb, bdir=copied, task_id=task_id, selection=selection)
        frozen = spec.to_dict()
        durable(output / 'claim_spec.json', frozen, new=True)
        durable(output / 'arms.json', A.build_claim_arms(spec, spec.summary_message, spec.summary_body), new=True)
        if smoke_runner is None:
            import importlib.util
            location = Path(__file__).resolve().parents[3] / 'scripts/s2_planted_docker_smoke.py'
            module_spec = importlib.util.spec_from_file_location('_planted_partb_smoke', location)
            smoke = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(smoke)
            smoke_runner = smoke.exercise_suite
        measured = smoke_runner(sb, '/tmp/emx-partb-smoke', test_command=selection['test_command'],
                                mutation_paths=mutation_scope(spec), mutation_markers=(spec.helper,))
        durable(output / 'instrumentation.json', measured)
        if not measured.get('ok'):
            raise ValueError('write coverage or trace-volume/slowdown prerequisite failed')
        binary = checked(sb, f'sha256sum {CC.CLI_DIR}/strace').stdout.split()[0]
        report.update(ok=True, image_id=image, boundary=str(copied), spec_sha256=frozen['spec_sha256'],
                      strace_sha256=binary, specification=str(output / 'claim_spec.json'),
                      instrumentation=str(output / 'instrumentation.json'),
                      instrumentation_sha256=A.digest(measured), parser_versions=PARSER_VERSIONS,
                      source_scope_guard=measured['real_test']['evidence']['source_scope_guard'])
    except Exception as exc:  # noqa: BLE001 -- preparation is an explicit gate, not a fork
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        try:
            report['cleanup_unconfirmed'] = inv.cleanup()
        except Exception as exc:  # noqa: BLE001 -- preserve cleanup failures as blocking evidence
            report['cleanup_unconfirmed'] = [str(exc)]
        report['ok'] = report['ok'] and not report['cleanup_unconfirmed']
        report['ended_at'] = N.utc()
        durable(output / 'preparation.json', report)
    return report


def freeze_preparations(outputs: list[Path], destination: Path, *, active_tasks: list[str]) -> dict:
    wanted = part_b_tasks(active_tasks)
    reports = [P.read(Path(out) / 'preparation.json') for out in outputs]
    if len(reports) != 2 or [row['task_id'] for row in reports] != wanted:
        raise ValueError('Part B uses exactly the first two active tasks in seeded order')
    manifest = {'schema': 1, 'ok': all(row.get('ok') for row in reports), 'tasks': wanted,
                'preparations': {row['task_id']: {'path': str(Path(out).resolve()),
                    'sha256': A.digest(row)} for row, out in zip(reports, outputs, strict=True)},
                'created_at': N.utc(), 'seed': SEED, 'planned_forks': 30, 'smoke_allowance': 4}
    durable(destination, manifest, new=True)
    return manifest


def free_host_resources() -> dict:
    """Local Docker storage reserve covers 30 capped traces plus a 5 GiB margin."""
    root = local_docker(['info', '--format', '{{.DockerRootDir}}'], timeout=30).stdout.decode().strip()
    available = shutil.disk_usage(root).free
    minimum = 20 * 1024**3
    if available < minimum:
        raise ValueError(f'Part B requires 20 GiB free Docker storage; observed {available} bytes')
    memory = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        if key == 'MemAvailable':
            memory['available_bytes'] = int(value.split()[0]) * 1024
    if memory.get('available_bytes', 0) < 6 * 1024**3:
        raise ValueError('Part B requires 6 GiB available host memory for task and bounded trace parsing')
    return {'docker_root': root, 'disk_free_bytes': available, 'minimum_disk_bytes': minimum,
            'max_trace_bytes_per_call': 512 * 1024**2, 'max_trace_bytes_per_fork': 30 * 512 * 1024**2,
            'host_memory': memory, 'minimum_available_memory_bytes': 6 * 1024**3}


def verify_preparation(spec: A.ClaimSpec, preparation: dict) -> dict:
    """Revalidate mutable image tag/CLI volume immediately before assigning any forks."""
    result = {'ok': False, 'model_calls': 0}
    inv = SB.Invocation(purpose='planted-partb-preflight', host=LOCAL_DOCKER)
    try:
        from compaction.s2.planted_eligibility import resource_guard
        resource_guard(LOCAL_DOCKER)
        image = json.loads(local_docker(['image', 'inspect', spec.source.kept_image], timeout=30).stdout)[0]['Id']
        if image != preparation['image_id']:
            raise ValueError('prepared kept image tag changed')
        sb = N.start_task_container(inv, image, network='none', cpus=1, memory_mb=1024,
            lifetime_s=180, extra_args=(*CC.run_args(), '--pull', 'never', '--entrypoint', ''))
        binary = checked(sb, f'sha256sum {CC.CLI_DIR}/strace').stdout.split()[0]
        if binary != preparation['strace_sha256']:
            raise ValueError('prepared static strace binary changed')
        captured = capture_source(sb, bdir=Path(spec.boundary_id), path=spec.source.path)
        if captured != spec.source:
            raise ValueError('prepared authoritative source changed')
        audit = CC.ProbeAudit(sb, mutation_paths=mutation_scope(spec), mutation_markers=(spec.helper,))
        if not audit.trace_available or audit.issues:
            raise ValueError('fresh production strace/scope preflight failed: ' + str(audit.issues))
        if audit.source_scope_guard != preparation.get('source_scope_guard'):
            raise ValueError('prepared tracked source inventory or marker guard changed')
        result.update(ok=True, image_id=image, strace_sha256=binary, trace_preflight=audit.trace_preflight)
    except Exception as exc:  # noqa: BLE001 -- free preflight refuses launch, no fork consumed
        result['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        try:
            result['cleanup_unconfirmed'] = inv.cleanup()
        except Exception as exc:  # noqa: BLE001 -- cleanup is a gate
            result['cleanup_unconfirmed'] = [str(exc)]
        result['ok'] = result['ok'] and not result['cleanup_unconfirmed']
    return result


def rate_fields(snapshot: dict, now: float) -> tuple[float, datetime, datetime]:
    P.guard_rate(snapshot, now=now)
    info = snapshot['rate_limit_info']
    seven = (info.get('unifiedWindows') or {}).get('seven_day') or info
    return (float(seven['utilization']), datetime.fromtimestamp(snapshot['observed_at'], UTC),
            datetime.fromtimestamp(seven['resetsAt'], UTC))


class PartBRuntime:
    """Thirty frozen assignments, guarded one at a time; interrupted assigned forks never rerun.

    `rate_provider` and `judge` are the desktop Runtime's Max-only callbacks. They may
    call Max, and therefore are invoked only behind the reset/deadline/before_action gates.
    Construction and recover() perform no external actions.
    """
    def __init__(self, *, root: Path, preparations: Path, budget: ForkBudget,
                 rate_provider: Callable, judge: Callable, token: str, task_loader: Callable,
                 step0_passed_at: datetime | None = None, fork_runner=CC.fork,
                 clock: Callable = time.time, expected_fork_utilization: float = .001,
                 preflight_runner=verify_preparation, resource_checker=free_host_resources):
        self.root, self.preparations, self.budget = Path(root), Path(preparations), budget
        self.rate_provider, self.judge, self.token, self.task_loader = rate_provider, judge, token, task_loader
        self.step0_passed_at, self.fork_runner, self.clock = step0_passed_at, fork_runner, clock
        self.expected = expected_fork_utilization
        self.preflight_runner = preflight_runner
        self.resource_checker = resource_checker
        self.allowance = PartBAllowance(self.root / 'part_b_allowance.json')
        self.before_action = lambda action: None
        self._last_rate = None

    def recover(self, work_id: str) -> dict | None:
        receipt = self.root / work_id / 'receipt.json'
        if not receipt.exists():
            return None
        value = P.read(receipt)
        if value.get('status') == 'RUNNING':
            return {**value, 'status': 'HOLD', 'reason': 'interrupted Part B stage; assigned forks are never relaunched',
                    'artifact': str(receipt)}
        return value

    def _remember_rate(self, snapshot):
        """Share an observed event with the desktop cache, without creating a Max call."""
        rate_fields(snapshot, self.clock())
        if self._last_rate is None or snapshot['observed_at'] >= self._last_rate['observed_at']:
            self._last_rate = snapshot
            owner = getattr(self.rate_provider, '__self__', None)
            observe = getattr(owner, 'observe_rate', None)
            if observe is not None:
                observe(snapshot)
        return self._last_rate

    def _actual_rate(self, snapshot, *, since=None):
        """Accept only a fresh actual event; a pre-action cache cannot settle its charge."""
        from compaction.s2.planted_runtime import reusable_rate
        if reusable_rate(snapshot, now=self.clock(), since=since) is None:
            return None
        return self._remember_rate(snapshot)

    def _post_rate(self, snapshot, identity):
        pending = P.read(self.allowance.path).get('pending') or {}
        if pending.get('attempt_id') != identity:
            raise ValueError('Part B post-action allowance identity changed')
        since = datetime.fromisoformat(pending['assigned_at']).timestamp()
        actual = self._actual_rate(snapshot, since=since)
        if actual is not None:
            return actual
        if self.clock() > B_CUTOFF.timestamp() or (C.REPO_ROOT / 'logs/s2/planted_run.stop').exists():
            raise ValueError('Part B stopped before a post-action rate refresh could run')
        return self._remember_rate(self.rate_provider(force=True))

    def _gate(self, action: str, work_id: str, *, attempt_id: str | None = None, force=False):
        now = datetime.fromtimestamp(self.clock(), UTC)
        if now < RESET:
            raise ValueError('Part B must wait for the initial Max reset')
        if self.step0_passed_at is None or self.step0_passed_at > B_DROP_IF_STEP0_AFTER:
            raise ValueError('Part B dropped because Step 0 did not pass before Tuesday noon')
        if now > B_CUTOFF:
            raise ValueError('Part B Tuesday cutoff reached')
        latest = getattr(self.judge, 'latest_rate', lambda: None)()
        self._actual_rate(latest)
        self.before_action({'action': action, 'work_id': work_id, 'part': 'B',
                            'attempt_id': attempt_id or work_id})
        snapshot = self.rate_provider(force=force)
        self._remember_rate(snapshot)
        return self._last_rate

    def _reserve_allowance(self, identity, before):
        utilization, observed, reset = rate_fields(before, self.clock())
        return self.allowance.guard(attempt_id=identity, step0_passed_at=self.step0_passed_at,
            now=datetime.fromtimestamp(self.clock(), UTC), reading_at=observed,
            current_utilization=utilization, expected_fork_utilization=self.expected,
            window_reset_at=reset)

    def _finish_allowance(self, identity, status, artifact, after):
        utilization, observed, _ = rate_fields(after, self.clock())
        self.allowance.finish(identity, status, artifact=str(artifact), current_utilization=utilization,
                              reading_at=observed, now=datetime.fromtimestamp(self.clock(), UTC))

    def _judge(self, request: dict, *, identity: str, directory: Path, fork_id: str) -> dict:
        artifact = directory / (A.digest(request) + '.json')
        if artifact.exists():
            cached = P.read(artifact)
            if cached['request'] != request:
                raise ValueError('judge request cache mismatch')
            return cached['answer']
        before = self._gate('part_b_judge', identity, attempt_id=fork_id)
        self._reserve_allowance(identity, before)
        if not getattr(self.judge, 'guarded_attempts', False):
            raise ValueError('Part B requires per-attempt guarded Max judge')
        def before_attempt(metadata):
            current = self._gate('part_b_judge_attempt', identity, attempt_id=fork_id)
            utilization, observed, reset = rate_fields(current, self.clock())
            state = P.read(self.allowance.path)
            if state['window_reset_at'] != reset.isoformat() or state.get('pending', {}).get('attempt_id') != identity:
                raise ValueError('Part B judgment allowance/window changed')
            part_b_guard(step0_passed_at=self.step0_passed_at, now=datetime.fromtimestamp(self.clock(), UTC),
                reading_at=observed, start_utilization=state['start_utilization'], current_utilization=utilization,
                expected_fork_utilization=max(self.expected, state.get('observed_fork_floor', 0)))
            durable(directory / (A.digest([identity, metadata, current]) + '-attempt.json'),
                    {'identity': identity, 'attempt': metadata, 'reading': current}, new=True)
        limit = min(.90, P.read(self.allowance.path)['start_utilization'] + .02)
        answer = self.judge(request, before_attempt=before_attempt, max_window_utilization=limit)
        after = self._post_rate(getattr(self.judge, 'latest_rate', lambda: None)(), identity)
        durable(artifact, {'request': request, 'answer': answer, 'before': before, 'after': after}, new=True)
        self._finish_allowance(identity, 'complete', artifact, after)
        return answer

    def _prepared(self, tasks):
        manifest = P.read(self.preparations)
        if manifest.get('ok') is not True or manifest.get('tasks') != list(tasks) or len(tasks) != 2:
            raise ValueError('two measured Part B preparations must match current seeded cohort')
        prepared = {}
        for task in tasks:
            row = manifest['preparations'][task]
            folder = Path(row['path'])
            report = P.read(folder / 'preparation.json')
            if A.digest(report) != row['sha256'] or report.get('ok') is not True:
                raise ValueError('preparation evidence changed or failed')
            measured = P.read(folder / 'instrumentation.json')
            if not measured.get('ok') or A.digest(measured) != report['instrumentation_sha256']:
                raise ValueError('instrumentation evidence changed or failed')
            spec = A.ClaimSpec.from_dict(P.read(folder / 'claim_spec.json'))
            if spec.to_dict()['spec_sha256'] != report['spec_sha256'] or spec.task_id != task:
                raise ValueError('frozen task spec mismatch')
            if (measured.get('production_trace_flags') != CC.TRACE_FLAGS
                    or measured.get('thresholds', {}).get('max_trace_bytes') != CC.TRACE_BYTE_LIMIT
                    or measured.get('real_test', {}).get('evidence', {}).get('mutation_paths') != mutation_scope(spec)
                    or not valid_scope_guard(measured.get('real_test', {}).get('evidence', {}).get('source_scope_guard'), spec)):
                raise ValueError('measured tracing flags, byte bound, or mutation scope guard differ from production')
            if report.get('source_scope_guard') != measured['real_test']['evidence']['source_scope_guard']:
                raise ValueError('prepared source guard differs from measured guard')
            prepared[task] = (folder, report, spec)
        return prepared

    def run_part_b(self, work_id: str, tasks: list[str], before_action: Callable,
                   *, step0_passed_at: datetime | None = None) -> dict:
        if not re.fullmatch(r'[A-Za-z0-9_-]+', work_id):
            raise ValueError('simple work id required')
        existing = self.recover(work_id)
        if existing is not None:
            return existing
        if step0_passed_at is not None:
            self.step0_passed_at = step0_passed_at
        self.before_action = before_action
        out = self.root / work_id
        artifact = out / 'receipt.json'
        result = {'status': 'RUNNING', 'artifact': str(artifact), 'work_id': work_id,
                  'started_at': N.utc(), 'assignments': [], 'results': [], 'reason': ''}
        durable(artifact, result, new=True)
        try:
            if self.clock() < RESET.timestamp():
                result.update(status='HOLD', reason='initial Max reset has not occurred')
                return result
            if self.step0_passed_at is None:
                raise ValueError('Step 0 PASS timestamp required')
            if self.step0_passed_at > B_DROP_IF_STEP0_AFTER or self.clock() > B_CUTOFF.timestamp():
                result.update(status='SKIP', reason='Part B dropped by plan deadline')
                return result
            if not self.preparations.exists() or not P.read(self.preparations).get('ok'):
                result.update(status='SKIP', reason='Part B source/strace prerequisites did not pass')
                return result
            manifest = P.read(self.preparations)
            if manifest.get('tasks') is not None and manifest['tasks'] != list(tasks):
                result.update(status='SKIP', reason='active cohort changed after Part B source freeze')
                return result
            prepared = self._prepared(tasks)
            for task, (_, report, spec) in prepared.items():
                preflight = self.preflight_runner(spec, report)
                durable(out / 'preflight' / (task + '.json'), preflight, new=True)
                if not preflight.get('ok'):
                    result.update(status='SKIP', reason='Part B fresh free prerequisite failed: ' + str(preflight.get('error')))
                    return result
            assignments = [{'task': task, 'arm': arm, 'repeat': repeat,
                            'id': f'{work_id}-{task.split("-")[0]}-{arm}-{repeat}'}
                           for task in tasks for arm in A.ARMS for repeat in range(1, A.REPEATS + 1)]
            assignments.sort(key=lambda row: order_key('part-b-assignment', f'{row["task"]}:{row["arm"]}:{row["repeat"]}'))
            result['assignments'] = assignments
            durable(out / 'assignments.json', {'seed': SEED, 'rows': assignments}, new=True)
            for row in assignments:
                _folder, _preparation, spec = prepared[row['task']]
                identity = row['id']
                attempt = out / identity
                resources = self.resource_checker()
                durable(attempt / 'resources.json', resources, new=True)
                before = self._gate('part_b_fork', identity)
                # All free source and real tracing prerequisites precede this reservation.
                self._reserve_allowance(identity, before)
                self.budget.reserve(identity, part='B', bucket='main', task=row['task'],
                                    arm=row['arm'], repeat=row['repeat'], spec_sha256=spec.to_dict()['spec_sha256'])
                durable(attempt / 'assignment.json', {**row, 'before': before}, new=True)
                arm = A.build_claim_arms(spec, spec.summary_message, spec.summary_body)[row['arm']]
                required = arm['lines']
                forbidden = [spec.helper] if row['arm'] == 'control' else (
                    [spec.evidence_line] if row['arm'] == 'claim' else [])
                task = self.task_loader(row['task'])
                fork = self.fork_runner(Path(spec.boundary_id), attempt / 'fork',
                    new_summary=arm['summary'], prompt=spec.probe_message, sim_model=CC.NoSimulatorModel(),
                    token=self.token, persona=task.persona, cfg=CC.CCConfig(auto_compact_window=80_000, max_window_utilization=min(.90, P.read(self.allowance.path)['start_utilization'] + .02),
                        deadline_epoch=B_CUTOFF.timestamp(),
                        stop_file=str(C.REPO_ROOT / 'logs/s2/planted_run.stop')),
                    host=LOCAL_DOCKER, probe=True, max_calls=30,
                    mutation_paths=mutation_scope(spec), mutation_markers=(spec.helper,),
                    required_context=required, forbidden_context=forbidden,
                    timeout_s=max(1, min(1800, int(B_CUTOFF.timestamp() - self.clock()))))
                evidence = P.read(attempt / 'fork/evidence.json')
                context = P.read(attempt / 'fork/initial_context.json')
                safe_technical = (context.get('ok') is True
                    and (fork.get('fork_check') or {}).get('ok') is True
                    and fork.get('sim_calls') == 0 and fork.get('sim_cost_usd') == 0
                    and fork.get('cleanup_unconfirmed') == []
                    and not any(fork.get(key) for key in ('auth_error', 'paid_route', 'policy_stop')))
                complete = (safe_technical and evidence.get('capture_complete') is True
                            and not fork.get('error')
                            and fork.get('outcome') in {'ask_user', 'submission', 'call_cap'})
                after = self._post_rate(fork.get('rate_limit'), identity)
                preliminary = {'assignment': row, 'fork': fork, 'capture_complete': complete,
                               'before': before, 'after': after, 'safe_technical': safe_technical}
                durable(attempt / 'result.json', preliminary, new=True)
                status = 'complete' if complete else 'technical_failure'
                self._finish_allowance(identity, status, attempt / 'result.json', after)
                if not complete:
                    self.budget.finish(identity, status, artifact=str(attempt / 'result.json'))
                    scored = S.score(evidence.get('events', []), spec.scorer_spec(), capture_complete=False)
                    durable(attempt / 'score.json', scored, new=True)
                    result['results'].append({**row, 'outcome': 'technical_failure',
                                              'score': str(attempt / 'score.json')})
                    durable(artifact, result)
                    if safe_technical:
                        continue  # Planned exposure category; retain this assigned failure without replacement.
                    raise RuntimeError('Part B source/context/safety gate failed; halt without replacement')
                # Judges see the actual initial context only, never continuation outcomes.
                if not any(previous['outcome'] != 'technical_failure' and previous['task'] == row['task'] and previous['arm'] == row['arm']
                           for previous in result['results']):
                    request = {'model': A.PLAUSIBILITY_MODEL, 'system': A.PLAUSIBILITY_PROMPT,
                               'claim': spec.claim_line, 'effective_context': json.dumps(context['surfaces'], ensure_ascii=False),
                               'probe_message': spec.probe_message, 'spec_sha256': spec.to_dict()['spec_sha256']}
                    rated = A.grade_plausibility({identity: request}, lambda req, identity=identity: self._judge(
                        req, identity=identity + '-plausibility', directory=out / 'judgments', fork_id=identity))
                    durable(attempt / 'plausibility.json', {'ratings': rated}, new=True)
                judge_errors = []
                def classify(req, identity=identity, judge_errors=judge_errors):
                    try:
                        return self._judge(req, identity=identity + '-judge-' + A.digest(req)[:12],
                                           directory=out / 'judgments', fork_id=identity)
                    except Exception as exc:  # scorer records unknown; runtime must also halt
                        judge_errors.append(f'{type(exc).__name__}: {exc}')
                        raise
                scored = S.score(evidence['events'], spec.scorer_spec(), capture_complete=True, judge=classify)
                durable(attempt / 'score.json', scored, new=True)
                if judge_errors:
                    raise RuntimeError('Part B classification stopped: ' + '; '.join(judge_errors))
                self.budget.finish(identity, 'complete', artifact=str(attempt / 'result.json'))
                result['results'].append({**row, 'outcome': scored['outcome'], 'score': str(attempt / 'score.json')})
                durable(artifact, result)
            result.update(status='PASS', reason='all 30 assigned Part B forks processed; clean technical failures retained; cards/repair not started')
        except Exception as exc:  # noqa: BLE001 -- durable terminal stop, never rerun an assigned fork
            result.update(status='HOLD' if isinstance(exc, ValueError) else 'FAIL',
                          reason=f'{type(exc).__name__}: {exc}')
        finally:
            result['finished_at'] = N.utc()
            result['budget'] = self.budget.state()
            durable(artifact, result)
        return result
