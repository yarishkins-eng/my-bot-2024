"""Join the frozen writer inventory with pytest-cov executed-line evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


SECTIONS = (
    'calls',
    'definitions',
    'raw_endpoints',
    'reachable_writer_functions',
    'reachable_writer_entrypoints',
    'startup_registrations',
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--coverage', type=Path, required=True)
    parser.add_argument('--test-manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()

    inventory = json.loads(args.inventory.read_text(encoding='utf-8'))
    coverage = json.loads(args.coverage.read_text(encoding='utf-8'))
    manifest_bytes = args.test_manifest.read_bytes()
    executed = {path: set(details.get('executed_lines') or []) for path, details in coverage.get('files', {}).items()}
    sections: dict[str, object] = {}
    for section in SECTIONS:
        rows = []
        for item in inventory.get(section, []):
            path = str(item['path'])
            line = int(item['line'])
            rows.append({'path': path, 'line': line, 'covered': line in executed.get(path, set())})
        covered = sum(1 for item in rows if item['covered'])
        sections[section] = {
            'total': len(rows),
            'covered': covered,
            'uncovered': len(rows) - covered,
            'locations': rows,
        }
    payload = {
        'format': 1,
        'inventory_sha256': inventory['sha256'],
        'coverage_tool': coverage.get('meta', {}).get('version'),
        'test_manifest': args.test_manifest.as_posix(),
        'test_manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
        'overall_app_lines': {
            key: coverage.get('totals', {}).get(key) for key in ('covered_lines', 'num_statements', 'percent_covered')
        },
        'sections': sections,
        'interpretation': (
            'Executed lines prove exercised paths only. Uncovered locations remain a cutover NO-GO; '
            'coverage never proves absence of Python dynamic dispatch.'
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
