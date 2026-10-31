"""Explicit model-free Part B provision/source preparation; local Docker only.

Use uv run --no-sync --with tree-sitter==0.25.2 --with tree-sitter-rust==0.24.0
--with tree-sitter-javascript==0.25.0 python scripts/s2_planted_partb_prepare.py ...
No imports, help, or commands without --go create containers. No model calls exist here.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from compaction.s2 import planted_partb as B
from compaction.s2 import planted_step0 as P


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    provision = commands.add_parser('provision')
    provision.add_argument('--image', required=True)
    provision.add_argument('--out', required=True, type=Path)
    provision.add_argument('--go', action='store_true')
    inspect = commands.add_parser('inspect')
    inspect.add_argument('--boundary', required=True, type=Path)
    inspect.add_argument('--prefix', required=True, action='append')
    inspect.add_argument('--out', required=True, type=Path)
    inspect.add_argument('--go', action='store_true')
    prepare = commands.add_parser('prepare')
    prepare.add_argument('--boundary', required=True, type=Path)
    prepare.add_argument('--task', required=True)
    prepare.add_argument('--selection', required=True, type=Path)
    prepare.add_argument('--eligibility', required=True, type=Path)
    prepare.add_argument('--out', required=True, type=Path)
    prepare.add_argument('--go', action='store_true')
    manifest = commands.add_parser('manifest')
    manifest.add_argument('--preparation', required=True, action='append', type=Path)
    manifest.add_argument('--active-tasks', required=True, nargs='+')
    manifest.add_argument('--out', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == 'provision':
        report = B.provision_strace(args.image, args.out, go=args.go)
    elif args.command == 'inspect':
        report = B.inspect_sources(args.boundary, args.out, prefixes=args.prefix, go=args.go)
    elif args.command == 'prepare':
        all_eligibility = P.read(args.eligibility)
        if 'tasks' not in all_eligibility:
            all_eligibility = {'tasks': [{'task_id': args.task, 'boundaries': [all_eligibility]}]}
        matches = [entry for task in all_eligibility['tasks'] if task['task_id'] == args.task
                   for entry in task['boundaries'] if Path(entry['boundary']).resolve() == args.boundary.resolve()]
        if len(matches) != 1:
            raise ValueError('exactly one live eligibility record required for selected boundary')
        report = B.prepare_boundary(args.boundary, args.out, task_id=args.task,
            selection=P.read(args.selection), eligibility=matches[0], go=args.go)
    else:
        report = B.freeze_preparations(args.preparation, args.out, active_tasks=args.active_tasks)
    print(json.dumps({'ok': report['ok'], 'artifact': str(args.out), 'error': report.get('error')}))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
