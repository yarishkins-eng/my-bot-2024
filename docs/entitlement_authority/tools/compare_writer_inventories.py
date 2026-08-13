"""Produce reproducible Phase-0/current writer-union evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


SECTIONS = (
    'calls',
    'definitions',
    'field_literals',
    'imports',
    'raw_endpoints',
    'reachable_writer_functions',
    'reachable_writer_entrypoints',
    'startup_registrations',
)


def stable(item: dict) -> str:
    value = {key: content for key, content in item.items() if key not in {'line', 'transport'}}
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'))


def expand(counter: Counter[str]) -> list[dict]:
    rows: list[dict] = []
    for signature, count in sorted(counter.items()):
        rows.append({'signature': json.loads(signature), 'count': count})
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--current', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text(encoding='utf-8'))
    current = json.loads(args.current.read_text(encoding='utf-8'))
    sections: dict[str, dict] = {}
    for section in SECTIONS:
        old = Counter(stable(item) for item in baseline[section])
        new = Counter(stable(item) for item in current[section])
        sections[section] = {
            'baseline_count': len(baseline[section]),
            'current_count': len(current[section]),
            'removed_or_renamed': expand(old - new),
            'new': expand(new - old),
            'unchanged_count': sum((old & new).values()),
        }
    payload = {
        'format': 1,
        'baseline_sha256': baseline['sha256'],
        'current_sha256': current['sha256'],
        'sections': sections,
        'interpretation': (
            'Every current entry is classified in writer_closure.json. '
            'Removed/renamed entries require an explicit mapping; this candidate has none '
            'for raw endpoints or startup registrations.'
        ),
    }
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
    payload['sha256'] = hashlib.sha256(canonical.encode()).hexdigest()
    rendered = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + '\n'
    if args.check:
        if not args.output.exists() or args.output.read_text(encoding='utf-8') != rendered:
            raise SystemExit('writer union evidence is stale')
    else:
        args.output.write_text(rendered, encoding='utf-8')


if __name__ == '__main__':
    main()
