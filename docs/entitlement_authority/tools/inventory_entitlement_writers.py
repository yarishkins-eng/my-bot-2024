"""Reproducible AST/static closure inventory for entitlement writers.

Run from the repository root. The JSON contains source locations only; no
runtime data or secrets are read.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path


PANEL_MUTATORS = {
    'add_users_to_external_squad',
    'add_users_to_internal_squad',
    'create_external_squad',
    'create_internal_squad',
    'create_user',
    'delete_external_squad',
    'delete_internal_squad',
    'delete_user',
    'disable_user',
    'enable_user',
    'remove_device',
    'remove_users_from_external_squad',
    'remove_users_from_internal_squad',
    'reset_user_devices',
    'reset_user_traffic',
    'reorder_external_squads',
    'reorder_internal_squads',
    'revoke_user_subscription',
    'update_external_squad',
    'update_internal_squad',
    'update_user',
}

WRAPPER_MUTATORS = {
    'create_remnawave_user',
    'delete_remnawave_user',
    'disable_remnawave_user',
    'enable_remnawave_user',
    'ensure_subscription_synced',
    'propagate_squads_to_tariff_subscriptions',
    'reset_subscription_with_panel',
    'revoke_remnawave_subscription',
    'run_guarded_panel_write',
    'sync_subscription_to_panel',
    'sync_subscription_with_remnawave',
    'sync_users_from_remnawave',
    'sync_users_to_remnawave',
    'update_remnawave_user',
}

WEBHOOK_WRITERS = {
    '_handle_user_created',
    '_handle_user_deleted',
    '_handle_user_disabled',
    '_handle_user_enabled',
    '_handle_user_expired',
    '_handle_user_limited',
    '_handle_user_modified',
    '_handle_user_revoked',
    '_handle_user_traffic_reset',
}

MUTATION_FIELDS = {
    'activeInternalSquads',
    'externalSquadUuid',
    'expireAt',
    'hwidDeviceLimit',
    'status',
    'trafficLimitBytes',
    'trafficLimitStrategy',
}

HTTP_MUTATION_METHODS = {'POST', 'PATCH', 'PUT', 'DELETE'}


class Visitor(ast.NodeVisitor):
    def __init__(self, path: Path, source: str) -> None:
        self.path = path
        self.source = source
        self.function_stack: list[str] = []
        self.calls: list[dict] = []
        self.definitions: list[dict] = []
        self.raw_endpoints: list[dict] = []
        self.imports: list[dict] = []
        self.startups: list[dict] = []
        self.field_literals: list[dict] = []
        self.all_definitions: list[dict] = []
        self.call_edges: list[dict] = []
        self.unknown_raw_requests: list[dict] = []

    def _function(self) -> str:
        return self.function_stack[-1] if self.function_stack else '<module>'

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ''
        if 'remnawave' in module:
            self.imports.append(
                {
                    'path': self.path.as_posix(),
                    'line': node.lineno,
                    'module': module,
                    'names': sorted(alias.name for alias in node.names),
                }
            )
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        names = sorted(alias.name for alias in node.names if 'remnawave' in alias.name)
        if names:
            self.imports.append(
                {
                    'path': self.path.as_posix(),
                    'line': node.lineno,
                    'module': '<import>',
                    'names': names,
                }
            )
        self.generic_visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.all_definitions.append(
            {
                'path': self.path.as_posix(),
                'line': node.lineno,
                'name': node.name,
                'decorators': sorted(ast.unparse(item) for item in node.decorator_list),
            }
        )
        if node.name in PANEL_MUTATORS | WRAPPER_MUTATORS | WEBHOOK_WRITERS:
            self.definitions.append(
                {
                    'path': self.path.as_posix(),
                    'line': node.lineno,
                    'name': node.name,
                    'async': isinstance(node, ast.AsyncFunctionDef),
                }
            )
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Call(self, node: ast.Call) -> None:
        called_name = None
        if isinstance(node.func, ast.Attribute):
            name = node.func.attr
            called_name = name
            if name in PANEL_MUTATORS | WRAPPER_MUTATORS:
                self.calls.append(
                    {
                        'path': self.path.as_posix(),
                        'line': node.lineno,
                        'function': self._function(),
                        'receiver': ast.unparse(node.func.value),
                        'method': name,
                        'kind': 'panel' if name in PANEL_MUTATORS else 'wrapper',
                    }
                )
            if name in {'create_task', 'add_job', 'start'}:
                rendered = ast.get_source_segment(self.source, node) or ast.unparse(node)
                lowered = rendered.lower()
                if any(
                    token in lowered
                    for token in ('worker', 'monitor', 'sync', 'remnawave', 'recovery', 'outbox', 'shadow')
                ):
                    self.startups.append(
                        {
                            'path': self.path.as_posix(),
                            'line': node.lineno,
                            'function': self._function(),
                            'call': rendered.replace('\n', ' ')[:300],
                        }
                    )
            if name in {'_make_request', 'request_once'}:
                method = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else None
                if method in HTTP_MUTATION_METHODS:
                    endpoint = ast.unparse(node.args[1]) if len(node.args) > 1 else '<missing>'
                    self.raw_endpoints.append(
                        {
                            'path': self.path.as_posix(),
                            'line': node.lineno,
                            'function': self._function(),
                            'method': method,
                            'endpoint': endpoint,
                            'transport': name,
                        }
                    )
                elif method is None or method not in {'GET'}:
                    self.unknown_raw_requests.append(
                        {
                            'path': self.path.as_posix(),
                            'line': node.lineno,
                            'function': self._function(),
                            'method_expression': ast.unparse(node.args[0]) if node.args else '<missing>',
                            'transport': name,
                        }
                    )
        elif isinstance(node.func, ast.Name):
            called_name = node.func.id
        if called_name is not None and self.function_stack:
            self.call_edges.append(
                {
                    'path': self.path.as_posix(),
                    'caller': self._function(),
                    'callee': called_name,
                    'line': node.lineno,
                }
            )
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and node.value in MUTATION_FIELDS:
            self.field_literals.append(
                {
                    'path': self.path.as_posix(),
                    'line': node.lineno,
                    'function': self._function(),
                    'field': node.value,
                }
            )


def inventory(root: Path) -> dict:
    sections: dict[str, list[dict]] = {
        'calls': [],
        'definitions': [],
        'field_literals': [],
        'imports': [],
        'raw_endpoints': [],
        'startup_registrations': [],
        'unknown_raw_requests': [],
    }
    syntax_errors: list[dict] = []
    all_definitions: list[dict] = []
    call_edges: list[dict] = []
    paths = sorted((root / 'app').rglob('*.py'))
    if (root / 'main.py').exists():
        paths.append(root / 'main.py')
    for path in paths:
        relative = path.relative_to(root)
        source = path.read_text(encoding='utf-8')
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            syntax_errors.append({'path': relative.as_posix(), 'line': exc.lineno})
            continue
        visitor = Visitor(relative, source)
        visitor.visit(tree)
        sections['calls'].extend(visitor.calls)
        sections['definitions'].extend(visitor.definitions)
        sections['field_literals'].extend(visitor.field_literals)
        sections['imports'].extend(visitor.imports)
        sections['raw_endpoints'].extend(visitor.raw_endpoints)
        sections['startup_registrations'].extend(visitor.startups)
        sections['unknown_raw_requests'].extend(visitor.unknown_raw_requests)
        all_definitions.extend(visitor.all_definitions)
        call_edges.extend(visitor.call_edges)

    # Conservative name-based reverse call graph. It intentionally
    # over-approximates dynamic Python dispatch: a false positive is reviewed,
    # while a new function that calls a known writer becomes visible.
    reachable_names = set(PANEL_MUTATORS | WRAPPER_MUTATORS | WEBHOOK_WRITERS)
    changed = True
    while changed:
        changed = False
        for edge in call_edges:
            if edge['callee'] in reachable_names and edge['caller'] not in reachable_names:
                reachable_names.add(edge['caller'])
                changed = True
    reachable_functions = [item for item in all_definitions if item['name'] in reachable_names]
    reachable_functions.sort(key=lambda item: json.dumps(item, sort_keys=True))
    entrypoint_markers = ('/routes/', '/handlers/', 'worker', 'monitor', 'webhook', 'retry', 'main.py')
    reachable_entrypoints = [
        item
        for item in reachable_functions
        if any(marker in f'/{item["path"]}'.lower() or marker in item['name'].lower() for marker in entrypoint_markers)
        or any('router.' in decorator for decorator in item['decorators'])
    ]
    for values in sections.values():
        values.sort(key=lambda item: json.dumps(item, sort_keys=True))
    payload = {
        'format': 1,
        'scope': 'app/**/*.py',
        'scope_files': ['app/**/*.py', 'main.py'],
        'panel_mutators': sorted(PANEL_MUTATORS),
        'wrapper_mutators': sorted(WRAPPER_MUTATORS),
        'webhook_writers': sorted(WEBHOOK_WRITERS),
        'syntax_errors': syntax_errors,
        'reachable_writer_functions': reachable_functions,
        'reachable_writer_entrypoints': reachable_entrypoints,
        **sections,
    }
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
    payload['sha256'] = hashlib.sha256(canonical.encode()).hexdigest()
    payload['counts'] = {
        **{key: len(value) for key, value in sections.items()},
        'reachable_writer_functions': len(reachable_functions),
        'reachable_writer_entrypoints': len(reachable_entrypoints),
    }
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    payload = inventory(args.root.resolve())
    rendered = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding='utf-8')
    else:
        print(rendered, end='')


if __name__ == '__main__':
    main()
