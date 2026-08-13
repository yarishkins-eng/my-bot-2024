"""Bind every Phase-0/current writer-inventory entry to a reviewed primitive.

The classifier is intentionally deterministic and fails closed.  A new raw
endpoint, startup site, dynamic request, syntax error, or unmatched section is
an architecture-test failure until its rule and evidence are reviewed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ACCESS_PRIMITIVES = {'source_mutation', 'overlay', 'project', 'cleanup', 'observation'}
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


RAW_RULES: dict[str, tuple[str, str, str]] = {
    'create_disabled': (
        'project',
        'strict_identity_create',
        'reviewed one-shot Gate-1 gateway; CREATE is forced DISABLED',
    ),
    'patch_exact': (
        'project',
        'strict_identity_patch',
        'reviewed one-shot Gate-1 gateway; no retry or field degradation',
    ),
    'delete_once': (
        'cleanup',
        'strict_identity_delete',
        'reviewed one-shot cleanup gateway; unknown outcome quarantines',
    ),
    'create_user': ('project', 'identity_create', 'strict CREATE must replace this legacy raw writer before cutover'),
    'update_user': ('project', 'identity_patch', 'strict exact projection must replace this legacy raw writer'),
    'delete_user': ('cleanup', 'identity_delete', 'terminal cleanup command owns identity deletion'),
    'disable_user': ('overlay', 'identity_deny', 'deny overlay owns disable intent'),
    'enable_user': ('project', 'identity_enable', 'verified current generation owns enable intent'),
    'reset_user_traffic': ('overlay', 'traffic_reset', 'proven traffic reset command owns LIMITED clearance'),
    'revoke_user_subscription': ('cleanup', 'credential_revoke', 'explicit owner generation owns revoke'),
    'remove_device': ('project', 'device_projection', 'authorized device command owns deletion'),
    'reset_user_devices': ('project', 'device_projection', 'authorized device reset command owns deletion'),
    'delete_device': ('project', 'device_projection', 'authorized device command owns deletion'),
    'delete_all_devices': ('project', 'device_projection', 'authorized device command owns deletion'),
    'reduce_devices': ('project', 'device_projection', 'paid device reduction owns deletion'),
    'execute_change_devices': ('project', 'device_projection', 'paid device change owns deletion'),
    'handle_all_devices_reset_from_management': ('project', 'device_projection', 'explicit user reset owns deletion'),
    'handle_single_device_reset': ('project', 'device_projection', 'explicit user reset owns deletion'),
    'add_users_to_external_squad': ('project', 'membership_projection', 'exact squad snapshot owns membership'),
    'remove_users_from_external_squad': ('project', 'membership_projection', 'exact squad snapshot owns membership'),
    'add_users_to_internal_squad': ('project', 'membership_projection', 'exact squad snapshot owns membership'),
    'remove_users_from_internal_squad': ('project', 'membership_projection', 'exact squad snapshot owns membership'),
    'add_all_users_to_squad': ('project', 'bulk_membership_projection', 'bulk membership is an access writer'),
    'remove_all_users_from_squad': ('project', 'bulk_membership_projection', 'bulk membership is an access writer'),
    'update_squad_inbounds': ('source_mutation', 'catalog_topology', 'squad topology changes access for all members'),
    'rename_squad': ('metadata_only', 'squad_label', 'name-only patch does not change user entitlement fields'),
    'create_internal_squad': (
        'source_mutation',
        'catalog_topology',
        'new entitlement topology requires an authorized catalog source',
    ),
    'update_internal_squad': ('source_mutation', 'catalog_topology', 'inbound topology may change member access'),
    'delete_internal_squad': ('source_mutation', 'catalog_topology', 'deletion may remove member access'),
    'reorder_internal_squads': ('metadata_only', 'catalog_order', 'presentation order only'),
    'create_external_squad': ('source_mutation', 'catalog_topology', 'template/settings topology may affect access'),
    'update_external_squad': ('source_mutation', 'catalog_topology', 'template/settings topology may affect access'),
    'delete_external_squad': ('source_mutation', 'catalog_topology', 'deletion may remove assigned policy'),
    'reorder_external_squads': ('metadata_only', 'catalog_order', 'presentation order only'),
    'restart_node': ('metadata_only', 'node_operation', 'node lifecycle is explicitly outside identity authority'),
    'restart_all_nodes': ('metadata_only', 'node_operation', 'node lifecycle is explicitly outside identity authority'),
    'enable_node': ('metadata_only', 'node_operation', 'node lifecycle is explicitly outside identity authority'),
    'disable_node': ('metadata_only', 'node_operation', 'node lifecycle is explicitly outside identity authority'),
    'create_subscription_page_config': ('metadata_only', 'subscription_page', 'output-template configuration only'),
    'update_subscription_page_config': ('metadata_only', 'subscription_page', 'output-template configuration only'),
    'delete_subscription_page_config': ('metadata_only', 'subscription_page', 'output-template configuration only'),
    'clone_subscription_page_config': ('metadata_only', 'subscription_page', 'output-template configuration only'),
    'reorder_subscription_page_configs': ('metadata_only', 'subscription_page', 'presentation order only'),
    '_encrypt_via_panel': ('metadata_only', 'happ_encryption', 'link transformation does not grant access'),
    'create_invoice': ('source_mutation', 'payment_provider', 'financial provider call is not a Panel access writer'),
}


def _entry_id(section: str, item: dict, occurrence: int) -> str:
    # The AST inventory deliberately retains repeated literals.  Two identical
    # nodes can share a source line, so bind the stable occurrence ordinal too.
    raw = json.dumps([section, item, occurrence], ensure_ascii=True, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()


def _startup_binding(item: dict) -> tuple[str, str, str]:
    value = f'{item["path"]} {item["function"]} {item["call"]}'.lower()
    path = item['path']
    function = item['function']
    call = item['call']

    # Every startup/task registration in the frozen inventory is reviewed
    # semantically.  There is deliberately no metadata fallback: a new task or
    # a renamed call fails generation until it receives an explicit rule.
    if path == 'app/middlewares/auth.py' and '_refresh_remnawave_description' in call:
        return ('metadata_only', 'description_refresh', 'description-only Panel patch; no entitlement field')
    if path == 'app/middlewares/button_stats.py' and '_log_button_click_async' in call:
        return ('metadata_only', 'button_analytics', 'append-only interaction analytics')
    if path == 'app/services/broadcast_service.py' and '_run_broadcast' in call:
        return ('metadata_only', 'message_delivery', 'Telegram/email broadcast delivery only')
    if path in {'app/handlers/polls.py', 'app/handlers/tickets.py'} and '_delete_message_later' in call:
        return ('metadata_only', 'message_cleanup', 'delayed Telegram message deletion only')
    if path == 'app/services/contest_rotation_service.py' and function == '_broadcast_to_users':
        return ('metadata_only', 'message_delivery', 'contest announcement delivery only')
    if path == 'app/services/system_settings_service.py' and 'auto_backup' in call:
        return ('metadata_only', 'backup_control', 'backup scheduler lifecycle only')
    if path == 'app/cabinet/routes/admin_promo_offers.py' and function == '_schedule_promo_notifications':
        return ('metadata_only', 'message_delivery', 'promo message scheduling does not grant the offer')
    if path == 'app/services/yandex_offline_conv_service.py' and function == 'spawn_bg':
        return ('metadata_only', 'conversion_analytics', 'offline conversion reporting only')
    if path == 'app/services/backup_service.py' and function == 'start_auto_backup':
        return ('metadata_only', 'backup_control', 'database backup scheduling only')
    if path == 'app/services/reporting_service.py' and function == 'start':
        return ('metadata_only', 'report_delivery', 'daily reporting loop only')
    if path == 'app/services/remnawave_webhook_service.py' and function == '_enqueue_node_event':
        return ('metadata_only', 'node_notification', 'coalesced node-status notification only')
    if path == 'app/services/maintenance_service.py':
        return ('metadata_only', 'bot_maintenance', 'bot availability mode does not mutate VPN entitlement')
    if path == 'app/services/disposable_email_service.py':
        return ('metadata_only', 'email_catalog_refresh', 'disposable-domain catalog refresh only')
    if path == 'app/services/nalogo_queue_service.py':
        return ('metadata_only', 'fiscal_receipt_queue', 'receipt transmission does not grant access')
    if path == 'app/services/log_rotation_service.py':
        return ('metadata_only', 'log_rotation', 'log retention task only')
    if path == 'app/services/referral_contest_service.py':
        return ('metadata_only', 'contest_reporting', 'leaderboard summary delivery only')
    if path == 'app/webapi/background/backup_tasks.py':
        return ('metadata_only', 'backup_control', 'web-admin backup task runner only')
    if path == 'app/services/monitoring_service.py' and function == 'start_monitoring':
        return ('observation', 'sla_monitor', 'read/notification SLA loop; access subworkers have separate entries')
    if path == 'app/services/traffic_monitoring_service.py' and function == 'stop_monitoring':
        return ('overlay', 'traffic_scheduler_control', 'stops an access-affecting traffic scheduler')
    if path == 'app/services/contest_rotation_service.py' and function == 'start':
        return ('source_mutation', 'contest_reward_source', 'contest loop can award subscription-day prizes')
    if path == 'app/services/payment_verification_service.py' and function == 'start':
        return (
            'source_mutation',
            'payment_verification_source',
            'provider finalization can enter post-topup purchase/extension chains',
        )
    if path == 'app/webserver/telegram.py' and function == 'start':
        return ('source_mutation', 'telegram_update_dispatch', 'worker dispatches user actions into commercial writers')
    if path == 'app/webapi/server.py' and function == 'start':
        return ('source_mutation', 'webapi_dispatch', 'server dispatches admin/commercial routes')
    if path == 'main.py' and 'dedupe_expired_tariff_subscriptions' in call:
        return (
            'cleanup',
            'legacy_subscription_dedupe',
            'startup deletes redundant Subscription rows and leaves Panel identities intentionally',
        )
    if path == 'main.py' and 'dp.start_polling' in call:
        return (
            'source_mutation',
            'telegram_update_dispatch',
            'polling dispatches user actions into commercial writers',
        )
    if path == 'main.py' and 'version_service.start_periodic_check' in call:
        return ('metadata_only', 'version_check', 'release metadata polling only')
    if path == 'main.py' and 'maintenance_service.start_monitoring' in call:
        return ('metadata_only', 'bot_maintenance', 'bot availability monitor does not mutate VPN entitlement')
    if path == 'main.py' and 'entitlement_shadow_service.run' in call:
        return (
            'observation',
            'entitlement_readonly_shadow',
            'double-interlocked task uses read-only SQL and redacted rate-limited Panel GET only',
        )
    if 'device_first_recovery' in value or 'access_point_term_projection' in value:
        return ('project', 'legacy_projection_worker', 'durable projection worker can mutate access')
    if 'remnawave_retry_queue' in value:
        return ('project', 'legacy_retry_consumer', 'process-local mutating retry consumer')
    if 'remnawave_sync_service' in value or 'run_sync_now' in value:
        return ('observation', 'legacy_sync_worker', 'sync may observe and then invoke legacy writers')
    if 'traffic' in value or 'daily_subscription' in value:
        return ('overlay', 'traffic_or_expiry_worker', 'traffic/expiry jobs may change effective access')
    if 'monitoring_service.start_monitoring' in value:
        return ('observation', 'monitoring_orchestrator', 'monitor starts access-affecting subworkers')
    if path == 'app/services/traffic_monitoring_service.py' and function == 'start':
        return ('overlay', 'traffic_or_expiry_worker', 'traffic loop can apply/clear runtime access overlays')
    if path == 'app/services/contest_rotation_service.py' and function == '_broadcast_to_users':
        return ('metadata_only', 'message_delivery', 'contest announcement delivery only')
    if path == 'app/services/referral_contest_service.py' and function == 'start':
        return ('metadata_only', 'contest_reporting', 'leaderboard summary delivery only')
    if path == 'app/bot.py' and 'remnawave_retry_queue.start' in call:
        return ('project', 'legacy_retry_consumer', 'process-local mutating retry consumer')
    raise ValueError(f'unreviewed startup registration: {path}:{item["line"]} {function}: {call}')


def _general_binding(section: str, item: dict) -> tuple[str, str, str]:
    value = ' '.join(str(item.get(key, '')) for key in ('path', 'name', 'method', 'function', 'module')).lower()
    if item['path'] == 'app/services/entitlement_authority/shadow_runtime.py' and section in {
        'imports',
        'reachable_writer_functions',
        'reachable_writer_entrypoints',
    }:
        return (
            'observation',
            'entitlement_readonly_shadow',
            'over-approximated call name is confined to read-only SQL and redacted GET adapter',
        )
    if section == 'field_literals':
        if '/entitlement_authority/' in f'/{item["path"]}':
            return (
                'metadata_only',
                'desired_schema_literal',
                'exact desired/observed schema declaration, not a writer',
            )
        return (
            'project',
            'legacy_mutation_field',
            'mutation field literal lies on the reviewed legacy/current surface',
        )
    if 'webhook' in value:
        return (
            'observation',
            'webhook_observation',
            'webhook is observation and must not own commercial desired state',
        )
    if any(token in value for token in ('erasure', 'merge', 'delete_user', 'reset_subscription')):
        return ('cleanup', 'lifecycle_cleanup', 'lifecycle path must emit a cleanup generation')
    if any(token in value for token in ('channel', 'block', 'limited', 'traffic', 'grace', 'expire')):
        return ('overlay', 'deny_or_runtime_overlay', 'runtime deny/expiry path must emit an overlay revision')
    if section == 'imports' and any(token in value for token in ('inventory', 'stats', 'read')):
        return ('observation', 'read_adapter', 'read-only inventory/statistics import')
    if any(
        token in value for token in ('payment', 'checkout', 'purchase', 'renew', 'trial', 'promo', 'gift', 'tariff')
    ):
        return (
            'source_mutation',
            'commercial_source',
            'commercial path must atomically emit immutable source evidence',
        )
    if section in {'calls', 'definitions', 'reachable_writer_functions', 'reachable_writer_entrypoints', 'imports'}:
        return ('project', 'legacy_writer_reachable', 'conservative call graph reaches a legacy Panel writer')
    raise ValueError(f'unclassified inventory entry: {section}: {item!r}')


def build(inventory: dict) -> dict:
    if inventory.get('syntax_errors') or inventory.get('unknown_raw_requests'):
        raise ValueError('inventory has AST blind spots or dynamic raw requests')
    entries: list[dict] = []
    for section in SECTIONS:
        occurrences: dict[str, int] = {}
        for item in inventory[section]:
            item_key = json.dumps(item, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
            occurrence = occurrences.get(item_key, 0)
            occurrences[item_key] = occurrence + 1
            if section == 'raw_endpoints':
                try:
                    primitive, surface, evidence = RAW_RULES[item['function']]
                except KeyError as exc:
                    raise ValueError(f'unreviewed raw endpoint: {item!r}') from exc
            elif section == 'startup_registrations':
                primitive, surface, evidence = _startup_binding(item)
            else:
                primitive, surface, evidence = _general_binding(section, item)
            entries.append(
                {
                    'entry_id': _entry_id(section, item, occurrence),
                    'section': section,
                    'path': item['path'],
                    'line': item['line'],
                    'symbol': item.get('function') or item.get('name') or item.get('module') or item.get('method'),
                    'primitive': primitive,
                    'surface': surface,
                    'evidence': evidence,
                    'access_relevant': primitive in ACCESS_PRIMITIVES,
                }
            )
    entries.sort(key=lambda value: (value['section'], value['path'], value['line'], value['entry_id']))
    payload = {
        'format': 1,
        'inventory_sha256': inventory['sha256'],
        'baseline_inventory_sha256': '724d620dd11f0b878d5d802bf8a8330c8d73cd8a75b09abf31bfd374190634c9',
        'baseline_relation': 'classified_current_inventory; union evidence is stored separately',
        'allowed_primitives': sorted(ACCESS_PRIMITIVES | {'metadata_only'}),
        'entries': entries,
        'counts': {section: sum(1 for entry in entries if entry['section'] == section) for section in SECTIONS},
    }
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
    payload['sha256'] = hashlib.sha256(canonical.encode()).hexdigest()
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    payload = build(json.loads(args.inventory.read_text(encoding='utf-8')))
    rendered = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + '\n'
    if args.check:
        if not args.output.exists() or args.output.read_text(encoding='utf-8') != rendered:
            raise SystemExit('writer closure is stale; regenerate and review it')
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding='utf-8')


if __name__ == '__main__':
    main()
