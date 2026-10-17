"""Regression tests for HAPP crypt-link template resolution (Remnawave 2.8.0).

The stored crypto link is a FULL deep link (``happ://crypt4/...`` from the old
panel endpoint or ``happ://crypt5/...`` from the official Happ API). Subpage
templates may hardcode the prefix (``happ://crypt4/{{HAPP_CRYPT4_LINK}}``) —
naive substitution would produce ``happ://crypt4/happ://crypt5/...``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes.subscription_modules import status as subscription_status
from app.cabinet.routes.subscription_modules.status import _create_deep_link, _resolve_button_url
from app.config import settings
from app.handlers.subscription.common import create_deep_link as bot_create_deep_link, resolve_button_url


CRYPT5 = 'happ://crypt5/encrypted-payload'
CRYPT4 = 'happ://crypt4/encrypted-payload'
SUB_URL = 'https://panel.example/sub/abc123'


class TestCabinetResolveButtonUrl:
    def test_bare_variable_resolves_to_full_link(self):
        assert _resolve_button_url('{{HAPP_CRYPT4_LINK}}', SUB_URL, CRYPT5) == CRYPT5

    def test_prefixed_template_does_not_double_prefix(self):
        assert _resolve_button_url('happ://crypt4/{{HAPP_CRYPT4_LINK}}', SUB_URL, CRYPT5) == CRYPT5
        assert _resolve_button_url('happ://crypt4/{{HAPP_CRYPT4_LINK}}', SUB_URL, CRYPT4) == CRYPT4
        assert _resolve_button_url('happ://crypt3/{{HAPP_CRYPT3_LINK}}', SUB_URL, CRYPT5) == CRYPT5

    def test_subscription_link_variable(self):
        assert _resolve_button_url('happ://add/{{SUBSCRIPTION_LINK}}', SUB_URL, CRYPT5) == f'happ://add/{SUB_URL}'

    def test_without_crypto_link_template_stays_unresolved(self):
        # The caller checks for leftover {{ }} and skips resolvedUrl in that case.
        assert _resolve_button_url('{{HAPP_CRYPT4_LINK}}', SUB_URL, None) == '{{HAPP_CRYPT4_LINK}}'


class TestBotResolveButtonUrl:
    def test_prefixed_template_does_not_double_prefix(self):
        assert resolve_button_url('happ://crypt4/{{HAPP_CRYPT4_LINK}}', SUB_URL, CRYPT5) == CRYPT5

    def test_bare_variable_resolves_to_full_link(self):
        assert resolve_button_url('{{HAPP_CRYPT4_LINK}}', SUB_URL, CRYPT5) == CRYPT5


class TestCreateDeepLink:
    @pytest.mark.parametrize('crypto', [CRYPT4, CRYPT5, None, 'https://untrusted.example/'])
    @pytest.mark.parametrize('direct', [False, True])
    def test_real_panel_bare_happ_template(self, crypto, direct):
        buttons = [{'type': 'subscriptionLink', 'link': '{{HAPP_CRYPT4_LINK}}'}]
        app = {'name': 'Happ', **({'buttons': buttons} if direct else {'blocks': [{'buttons': buttons}]})}
        expected = crypto if crypto in (CRYPT4, CRYPT5) else None
        assert _create_deep_link(app, SUB_URL, crypto) == expected

    def test_crypto_app_returns_stored_full_link_as_is(self):
        app = {'urlScheme': 'happ://crypt4/', 'usesCryptoLink': True}
        assert _create_deep_link(app, SUB_URL, CRYPT5) == CRYPT5
        assert _create_deep_link(app, SUB_URL, CRYPT4) == CRYPT4

    def test_crypto_app_without_crypto_link_returns_none(self):
        app = {'urlScheme': 'happ://crypt4/', 'usesCryptoLink': True}
        assert _create_deep_link(app, SUB_URL, None) is None

    def test_plain_app_prepends_scheme(self):
        app = {'urlScheme': 'happ://add/', 'usesCryptoLink': False}
        assert _create_deep_link(app, SUB_URL, None) == f'happ://add/{SUB_URL}'

    def test_crypto_app_with_https_redirect_wrapper_keeps_wrapper(self):
        # Redirect wrappers exist because Telegram/browsers can't open happ:// directly;
        # only a happ://-scheme prefix must be dropped, not an https wrapper.
        app = {'urlScheme': 'https://o.example/redirect?url=', 'usesCryptoLink': True}
        assert _create_deep_link(app, SUB_URL, CRYPT5) == f'https://o.example/redirect?url={CRYPT5}'


class TestBotCreateDeepLink:
    def test_crypt_scheme_with_crypt_payload_does_not_double_prefix(self):
        # In happ_cryptolink mode the bot passes the stored crypt link as the
        # subscription URL (get_display_subscription_link) — gluing the template
        # scheme on top used to produce happ://crypt4/happ://crypt5/...
        app = {'urlScheme': 'happ://crypt4/'}
        assert bot_create_deep_link(app, CRYPT5) == CRYPT5

    def test_plain_scheme_with_https_payload_still_prepends(self):
        app = {'urlScheme': 'happ://add/'}
        assert bot_create_deep_link(app, SUB_URL) == f'happ://add/{SUB_URL}'


@pytest.mark.parametrize('direct', [False, True])
def test_external_only_app_never_inherits_a_happ_crypto_link(direct):
    buttons = [{'type': 'external', 'link': 'https://example.invalid/download'}]
    app = {'name': 'ExternalOnly', **({'buttons': buttons} if direct else {'blocks': [{'buttons': buttons}]})}
    assert _create_deep_link(app, SUB_URL, CRYPT4) is None


@pytest.mark.parametrize('platform', ['ios', 'android'])
@pytest.mark.parametrize('app_name', ['INCY', 'Happ'])
@pytest.mark.anyio('asyncio')
async def test_get_app_config_with_real_panel_blocks(monkeypatch, platform, app_name):
    panel = json.loads((Path(__file__).parent / 'fixtures/panel-20261017.json').read_text(encoding='utf-8'))
    subscription = SimpleNamespace(
        actual_status='active',
        subscription_url=SUB_URL,
        subscription_crypto_link=CRYPT4,
    )
    monkeypatch.setattr(type(settings), 'should_hide_subscription_link', lambda self: False)
    monkeypatch.setattr(subscription_status, 'resolve_subscription', AsyncMock(return_value=subscription))
    monkeypatch.setattr(subscription_status, '_load_app_config_async', AsyncMock(return_value=copy.deepcopy(panel)))
    response = await subscription_status.get_app_config(
        user=SimpleNamespace(), db=SimpleNamespace(), subscription_id=None
    )
    app = next(a for a in response['platforms'][platform]['apps'] if a['name'] == app_name)
    original = next(a for a in panel['platforms'][platform]['apps'] if a['name'] == app_name)
    assert app['blocks'][0]['buttons'] == original['blocks'][0]['buttons']
    assert app['blocks'][0]['buttons'][0]['type'] == 'external'
    assert app['deepLink'] == (f'incy://import/{SUB_URL}' if app_name == 'INCY' else CRYPT4)
    assert app['blocks'][1]['buttons'][0]['resolvedUrl'] == app['deepLink']
