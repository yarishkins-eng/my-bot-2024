from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes import admin_auto_messages as api
from app.services.notification_settings_service import NotificationSettingsService as Settings
from tests.cabinet.test_ap4_pr4_prices import price_env  # noqa: F401 -- shared real-pricing fixture.


@pytest.mark.parametrize('carrier,card_id', [(0, 'trial-2h'), (24, 'trial-1d'), (48, 'trial-2d')])
def test_context_is_limited_to_exact_five_cards_and_uses_real_carrier_id(price_env, carrier, card_id):
    assert Settings.set_trial_ending_discount_carrier(carrier)
    found = set()
    for entry in api.AUTO_MESSAGE_CATALOG:
        item = api._build_item(entry, {}, {}, {}, {}).model_dump()
        if 'discount_context' in item:
            found.add(entry['id'])
            assert item['discount_context'] == {
                'carrier_id': card_id,
                'percent': 20,
                'enabled': False,
                'legacy_enabled': False,
            }
    assert found == {'trial-2h', 'trial-2d', 'trial-1d', 'trial-discount', 'trial-ending-discount'}


def test_quiet_reasons_follow_discount_then_carrier_then_price(price_env, monkeypatch):
    entry = api.CATALOG_BY_ID['trial-ending-discount']
    reasons = {'trial_warning_available': 'панель молчит', 'trial_ending_available': 'касса закрыта'}
    assert api._state_of(entry, reasons, {})[1] == 'выключено в этом разделе'
    assert Settings.set_trial_ending_discount_enabled(True)
    monkeypatch.setattr(Settings, 'are_notifications_globally_enabled', classmethod(lambda _: True))
    assert api._state_of(entry, reasons, {})[1] == 'письмо-носитель молчит: панель молчит'
    assert api._state_of(entry, {'trial_ending_available': 'касса закрыта'}, {})[1] == 'касса закрыта'
    assert api._state_of(entry, {}, {}) == ('live', None, 'выдача только тестовым стендам')


@pytest.mark.asyncio
async def test_previews_use_real_sender_formatters_and_do_not_issue_or_mutate(price_env):
    _, options = await api._discount_examples(AsyncMock())
    before = deepcopy(options)
    no_balance, balance, unused = api._discount_previews(options)
    assert '119 ₽ вместо <s>149 ₽</s>' in no_balance.text
    assert '319 ₽ вместо <s>399 ₽</s>' in no_balance.text
    assert no_balance.text.count('119 ₽') == 1
    assert 'с баланса 50 ₽, доплатить 69 ₽' in balance.text
    assert balance.text.count('119 ₽') == 1 and balance.text.count('50 ₽') == 1
    assert '149 ₽' in unused.text and '399 ₽' in unused.text
    assert '−20%' not in unused.text and '119 ₽' not in unused.text
    assert options == before and Settings.is_trial_ending_discount_enabled() is False
    assert Settings.get_trial_ending_discount_stats_since() is None
    assert Settings.set_trial_ending_discount_audience(0)
    assert '119 ₽' in api._discount_previews(options)[2].text


@pytest.mark.asyncio
async def test_catalog_summary_does_not_count_the_same_carrier_twice(price_env, monkeypatch):
    monkeypatch.setattr(api, '_quiet_facts', AsyncMock(return_value=({}, {})))
    monkeypatch.setattr(api, '_sent_counts', AsyncMock(return_value={('trial_expiring_24h', None): 7}))
    monkeypatch.setattr(api, '_claimed_counts', AsyncMock(return_value={}))
    monkeypatch.setattr(api, '_trial_discount_stats', AsyncMock(return_value=(3, 'Delivered', [])))
    db = AsyncMock()
    db.scalar.return_value = None
    response = await api.list_auto_messages(db, None)
    new = next(item for item in response.items if item.id == 'trial-ending-discount')
    assert new.sent_count == 3 and new.claimed_count is None and new.claim_tracked is False
    assert response.summary.sent_total == 7
    assert new.discount_prices['20'][0]['price_kopeks'] == 11900


@pytest.mark.asyncio
async def test_new_card_has_visible_history_note_and_no_second_text_editor(price_env, monkeypatch):
    monkeypatch.setattr(api, '_quiet_facts', AsyncMock(return_value=({}, {})))
    monkeypatch.setattr(api, '_sent_counts', AsyncMock(return_value={}))
    monkeypatch.setattr(api, '_claimed_counts', AsyncMock(return_value={}))
    monkeypatch.setattr(
        api, '_trial_discount_stats', AsyncMock(return_value=(0, 'Отсчёт начнётся после включения.', []))
    )
    detail = await api.get_auto_message('trial-ending-discount', None, AsyncMock(), None)
    assert detail.sent_count == 0 and 'после включения' in detail.history_note
    assert detail.text is None and detail.text_markers == []
    assert detail.buttons[0].label == '🎁 Оформить со скидкой'
    assert detail.discount_context['carrier_id'] == 'trial-1d'


@pytest.mark.asyncio
async def test_selected_existing_carrier_keeps_owner_text_and_gets_real_examples(price_env, monkeypatch):
    monkeypatch.setattr(api, '_quiet_facts', AsyncMock(return_value=({}, {})))
    monkeypatch.setattr(api, '_sent_counts', AsyncMock(return_value={}))
    monkeypatch.setattr(api, '_claimed_counts', AsyncMock(return_value={}))
    monkeypatch.setattr(api, '_history_for', AsyncMock(return_value=[]))
    assert Settings.set_text_override('TRIAL_EXPIRING_24H', 'Текст владельца\n\n{price_lines}')
    before = api._text_facts('trial-1d')
    off = await api.get_auto_message('trial-1d', None, AsyncMock(), None)
    assert off.text_inserts == before['text_inserts']
    assert Settings.set_trial_ending_discount_enabled(True)
    detail = await api.get_auto_message('trial-1d', None, AsyncMock(), None)
    assert detail.text == 'Текст владельца\n\n{price_lines}'
    insert = next(item for item in detail.text_inserts if item.name == 'price_lines')
    assert len(insert.variants) == 3 and '119 ₽' in insert.variants[0].text
    assert detail.text_source == 'custom'
    assert detail.text_markers == []
    assert Settings.set_trial_ending_discount_carrier(0)
    assert Settings.set_text_override('TRIAL_ENDING_TEXT', 'До конца {hours_text}.')
    last_hours = await api.get_auto_message('trial-2h', None, AsyncMock(), None)
    assert [marker.name for marker in last_hours.text_markers] == ['hours_text']
    assert any(insert.name == 'price_lines' for insert in last_hours.text_inserts)
