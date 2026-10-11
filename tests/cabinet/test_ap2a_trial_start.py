"""АП-2а: реальное первое письмо пробного и его единственная карточка настроек."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes import admin_auto_messages as cards
from app.localization.texts import get_texts
from app.services.notification_settings_service import NotificationSettingsService as Settings
from app.utils import funnel_notify


@pytest.fixture
def storage(tmp_path, monkeypatch):
    from app.localization.loader import clear_locale_cache

    monkeypatch.setattr(Settings, '_storage_path', tmp_path / 'notification_settings.json')
    monkeypatch.setattr(Settings, '_data', {})
    monkeypatch.setattr(Settings, '_loaded', False)
    monkeypatch.setattr(Settings, '_readonly', False)
    clear_locale_cache()
    yield Settings
    clear_locale_cache()


def _user(language='ru', *, is_trial=True, tariff_id=5, limit=7):
    return SimpleNamespace(
        id=42,
        telegram_id=123,
        language=language,
        subscription=SimpleNamespace(
            id=91,
            is_trial=is_trial,
            tariff_id=tariff_id,
            end_date=datetime(2030, 11, 30, 21, 47, tzinfo=UTC),
            traffic_limit_gb=limit,
        ),
    )


def _tariff(mode='DAY'):
    return SimpleNamespace(id=5, traffic_reset_mode=mode)


def _sender(monkeypatch):
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=17)),
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=17)),
        session=SimpleNamespace(close=AsyncMock()),
    )
    monkeypatch.setattr(funnel_notify, '_funnel_enabled', lambda: True)
    monkeypatch.setattr(funnel_notify.settings, 'ENABLE_LOGO_MODE', True)
    monkeypatch.setattr(funnel_notify.settings, 'MAIN_MENU_MODE', 'cabinet')
    monkeypatch.setattr(funnel_notify.settings, 'MINIAPP_CUSTOM_URL', 'https://cabinet.example.test')
    monkeypatch.setattr('app.bot_factory.create_bot', lambda: bot)
    monkeypatch.setattr(funnel_notify, '_delete_remembered_menu', AsyncMock())
    monkeypatch.setattr(funnel_notify, '_remember_menu_message_id', AsyncMock())
    monkeypatch.setattr(
        'app.utils.subscription_link_access.get_user_subscription_with_available_link', lambda user: None
    )
    return bot


@pytest.mark.asyncio
async def test_first_trial_letter_is_text_with_connection_and_real_subscription_facts(storage, monkeypatch):
    bot = _sender(monkeypatch)
    await funnel_notify.send_funnel_trial_menu(_user(), trial_tariff=_tariff())

    bot.send_message.assert_awaited_once()
    bot.send_photo.assert_not_awaited()
    sent = bot.send_message.call_args.kwargs
    assert '01.12' in sent['text'] and '00:47' in sent['text']
    assert '7' in sent['text'] and ('день' in sent['text'].lower() or 'сутки' in sent['text'].lower())
    assert '{end_date}' not in sent['text'] and '{traffic_text}' not in sent['text']
    button = sent['reply_markup'].inline_keyboard[0][0]
    assert button.text == get_texts('ru').t('FUNNEL_CONNECT_VPN')
    assert button.web_app.url == 'https://cabinet.example.test/connection'


def test_trial_started_card_uses_the_sender_source_and_defaults_enabled(storage):
    assert 'trial-started' in cards.CATALOG_BY_ID
    entry = cards.CATALOG_BY_ID['trial-started']
    assert entry['settings_key'] == 'trial_started'
    assert entry['control'] == 'toggle'
    assert storage.is_enabled('trial_started') is True
    assert cards._LOCALE_TEXT_KEYS['trial-started'] == 'FUNNEL_TRIAL_ACTIVATED'
    facts = cards._text_facts('trial-started')
    assert facts['text'] == get_texts('ru').t('FUNNEL_TRIAL_ACTIVATED')
    assert facts['text_only'] is True
    assert facts['text_with_logo'] is False
    explained = {marker.name for marker in facts['text_markers']} | {insert.name for insert in facts['text_inserts']}
    assert {'end_date', 'traffic_text', 'limit_gb'} <= explained


@pytest.mark.parametrize(
    'language,settings_word,claim', [('ru', 'настройки', 'вы подключились'), ('en', 'settings', 'you connected')]
)
def test_first_connection_text_describes_receiving_settings(storage, language, settings_word, claim):
    text = get_texts(language).t('WEBHOOK_SUB_FIRST_CONNECTED').lower()
    assert settings_word in text
    assert claim not in text


@pytest.mark.asyncio
@pytest.mark.parametrize('language,day_words', [('ru', ('день', 'сутки')), ('en', ('day',))])
async def test_daily_limit_is_subscription_data_in_both_languages(storage, monkeypatch, language, day_words):
    bot = _sender(monkeypatch)
    await funnel_notify.send_funnel_trial_menu(_user(language, limit=9), trial_tariff=_tariff())
    text = bot.send_message.call_args.kwargs['text']
    assert '9' in text and any(word in text.lower() for word in day_words)
    assert '5 ГБ' not in text and '5 GB' not in text
    bot.send_photo.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['NO_RESET', 'MONTH', 'WEEK'])
async def test_other_reset_modes_do_not_promise_daily_traffic(storage, monkeypatch, mode):
    bot = _sender(monkeypatch)
    await funnel_notify.send_funnel_trial_menu(_user(limit=13), trial_tariff=_tariff(mode))
    text = bot.send_message.call_args.kwargs['text'].lower()
    assert '13' in text
    assert 'в день' not in text and 'в сутки' not in text and 'per day' not in text


@pytest.mark.asyncio
@pytest.mark.parametrize('language,unlimited', [('ru', ('безлимит', 'без ограничения')), ('en', ('unlimited',))])
async def test_unlimited_subscription_does_not_display_zero_gb(storage, monkeypatch, language, unlimited):
    bot = _sender(monkeypatch)
    await funnel_notify.send_funnel_trial_menu(_user(language, limit=0), trial_tariff=_tariff())
    text = bot.send_message.call_args.kwargs['text'].lower()
    assert any(word in text for word in unlimited)
    assert '0 гб' not in text and '0 gb' not in text


@pytest.mark.asyncio
async def test_activation_only_prepends_connect_to_existing_trial_menu(storage, monkeypatch):
    from app.keyboards.inline import build_funnel_menu_keyboard
    from app.utils.funnel_state import FunnelState

    bot = _sender(monkeypatch)
    ordinary = build_funnel_menu_keyboard(FunnelState.TRIAL_ACTIVE, 'ru', get_texts('ru'))
    await funnel_notify.send_funnel_trial_menu(_user(), trial_tariff=_tariff())
    sent = bot.send_message.call_args.kwargs['reply_markup']
    assert len(sent.inline_keyboard[0]) == 1
    assert sent.inline_keyboard[1:] == ordinary.inline_keyboard
    assert ordinary.inline_keyboard[0][0].text == get_texts('ru').t('FUNNEL_SUBSCRIBE_CTA')


@pytest.mark.asyncio
async def test_connection_falls_back_to_existing_callback_without_cabinet_url(storage, monkeypatch):
    bot = _sender(monkeypatch)
    monkeypatch.setattr(funnel_notify.settings, 'MINIAPP_CUSTOM_URL', '')
    await funnel_notify.send_funnel_trial_menu(_user(), trial_tariff=_tariff())
    button = bot.send_message.call_args.kwargs['reply_markup'].inline_keyboard[0][0]
    assert button.callback_data == 'subscription_connect'
    assert button.web_app is None


def _trial_lookup(monkeypatch, tariff):
    lookup = AsyncMock(return_value=tariff)
    monkeypatch.setattr(funnel_notify, 'get_trial_tariff', lookup)
    return lookup


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['no_subscription', 'paid', 'other_tariff', 'no_canonical_tariff'])
async def test_notification_never_labels_an_unverified_subscription_as_trial(storage, monkeypatch, case):
    user = _user(is_trial=case != 'paid', tariff_id=99 if case == 'other_tariff' else 5)
    if case == 'no_subscription':
        user.subscription = None
    _trial_lookup(monkeypatch, None if case == 'no_canonical_tariff' else _tariff())
    send = AsyncMock()
    monkeypatch.setattr(funnel_notify, 'send_funnel_trial_menu', send)
    db = SimpleNamespace(refresh=AsyncMock())

    await funnel_notify.notify_trial_menu(db, user)

    send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['refresh', 'tariff'])
async def test_lookup_failure_skips_trial_letter_without_breaking_activation(storage, monkeypatch, failure):
    lookup = _trial_lookup(monkeypatch, _tariff())
    db = SimpleNamespace(refresh=AsyncMock())
    (db.refresh if failure == 'refresh' else lookup).side_effect = RuntimeError('lookup unavailable')
    send = AsyncMock()
    monkeypatch.setattr(funnel_notify, 'send_funnel_trial_menu', send)

    await funnel_notify.notify_trial_menu(db, _user())

    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_trial_started_clears_stale_menu_without_sending(storage, monkeypatch):
    bot = _sender(monkeypatch)
    assert storage.set_enabled('trial_started', False)
    _trial_lookup(monkeypatch, _tariff())
    clear = AsyncMock()
    monkeypatch.setattr(funnel_notify, 'clear_funnel_menu', clear)
    user = _user()
    await funnel_notify.notify_trial_menu(SimpleNamespace(refresh=AsyncMock()), user)
    bot.send_message.assert_not_awaited()
    bot.send_photo.assert_not_awaited()
    clear.assert_awaited_once_with(user)


@pytest.mark.asyncio
async def test_direct_sender_requires_subscription_and_verified_tariff(storage, monkeypatch):
    bot = _sender(monkeypatch)
    await funnel_notify.send_funnel_trial_menu(_user())
    user = _user()
    user.subscription = None
    await funnel_notify.send_funnel_trial_menu(user, trial_tariff=_tariff())
    bot.send_message.assert_not_awaited()
    bot.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_delivery_keeps_old_menu_and_closes_bot_session(storage, monkeypatch):
    bot = _sender(monkeypatch)
    bot.send_message.side_effect = RuntimeError('telegram unavailable')
    await funnel_notify.send_funnel_trial_menu(_user(), trial_tariff=_tariff())
    funnel_notify._delete_remembered_menu.assert_not_awaited()
    funnel_notify._remember_menu_message_id.assert_not_awaited()
    bot.session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_card_save_and_reset_reach_actual_ru_sender_and_leave_en_unchanged(storage, monkeypatch):
    bot = _sender(monkeypatch)
    monkeypatch.setattr(cards, '_quiet_facts', AsyncMock(return_value=({}, {})))
    monkeypatch.setattr(cards, '_sent_counts', AsyncMock(return_value={}))
    monkeypatch.setattr(cards, '_claimed_counts', AsyncMock(return_value={}))
    english = get_texts('en').t('FUNNEL_TRIAL_ACTIVATED')
    standard = get_texts('ru').t('FUNNEL_TRIAL_ACTIVATED')
    custom = 'Мой пробный до {end_date}. Мой лимит: {traffic_text}.'

    await cards.patch_auto_message('trial-started', cards.AutoMessagePatch(text=custom), object(), object())
    assert cards._text_facts('trial-started')['text_source'] == 'custom'
    assert cards._text_facts('trial-started')['text'] == custom
    assert get_texts('en').t('FUNNEL_TRIAL_ACTIVATED') == english
    await funnel_notify.send_funnel_trial_menu(_user(), trial_tariff=_tariff())
    sent = bot.send_message.call_args.kwargs['text']
    assert sent.startswith('Мой пробный до ') and 'Мой лимит:' in sent
    assert '{end_date}' not in sent and '{traffic_text}' not in sent

    await cards.patch_auto_message('trial-started', cards.AutoMessagePatch(reset_text=True), object(), object())
    facts = cards._text_facts('trial-started')
    assert facts['text_source'] == 'code' and facts['text'] == standard
    bot.send_message.reset_mock()
    await funnel_notify.send_funnel_trial_menu(_user(), trial_tariff=_tariff())
    assert 'Мой пробный' not in bot.send_message.call_args.kwargs['text']


@pytest.mark.asyncio
async def test_card_rejects_edit_that_loses_subscription_markers(storage, monkeypatch):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await cards.patch_auto_message(
            'trial-started', cards.AutoMessagePatch(text='Просто привет.'), object(), object()
        )
    assert exc.value.status_code == 422
    assert cards._text_facts('trial-started')['text_source'] == 'code'


@pytest.mark.asyncio
async def test_existing_card_api_exposes_text_only_and_switches_new_letter(storage, monkeypatch):
    monkeypatch.setattr(cards, '_quiet_facts', AsyncMock(return_value=({}, {})))
    monkeypatch.setattr(cards, '_sent_counts', AsyncMock(return_value={}))
    monkeypatch.setattr(cards, '_claimed_counts', AsyncMock(return_value={}))
    detail = await cards.get_auto_message('trial-started', object(), object(), object())
    assert detail.model_dump()['text_only'] is True
    assert detail.text == get_texts('ru').t('FUNNEL_TRIAL_ACTIVATED')
    assert detail.history == []
    assert detail.sent_count is None
    await cards.patch_auto_message('trial-started', cards.AutoMessagePatch(enabled=False), object(), object())
    assert storage.is_enabled('trial_started') is False
    await cards.patch_auto_message('trial-started', cards.AutoMessagePatch(enabled=True), object(), object())
    assert storage.is_enabled('trial_started') is True


@pytest.mark.asyncio
@pytest.mark.parametrize('marked', [True, False])
async def test_start_letter_keeps_trial_activation_tariff_fallback(storage, monkeypatch, marked):
    tariff = _tariff()
    _trial_lookup(monkeypatch, tariff if marked else None)
    fallback = AsyncMock(return_value=tariff)
    monkeypatch.setattr('app.database.crud.tariff.get_tariff_by_id', fallback)
    monkeypatch.setattr(funnel_notify.settings, 'TRIAL_TARIFF_ID', 5)
    send = AsyncMock()
    monkeypatch.setattr(funnel_notify, 'send_funnel_trial_menu', send)
    db = SimpleNamespace(refresh=AsyncMock())
    user = _user()
    await funnel_notify.notify_trial_menu(db, user)
    send.assert_awaited_once_with(user, trial_tariff=tariff)
    if marked:
        fallback.assert_not_awaited()
    else:
        fallback.assert_awaited_once_with(db, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize('fallback_exists', [True, False])
async def test_start_card_state_matches_configured_fallback_tariff(storage, monkeypatch, fallback_exists):
    from sqlalchemy import create_engine, text

    from app.services.daily_subscription_service import daily_subscription_service

    monkeypatch.setattr(funnel_notify.settings, 'TRIAL_TARIFF_ID', 5)
    monkeypatch.setattr(funnel_notify.settings, 'FUNNEL_MENU_ENABLED', True)
    monkeypatch.setattr(funnel_notify.settings, 'MAIN_MENU_MODE', 'cabinet')
    monkeypatch.setattr(daily_subscription_service, 'is_enabled', lambda: False)
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE tariffs (id INTEGER PRIMARY KEY, is_trial_available BOOLEAN)'))
        connection.execute(
            text(
                'CREATE TABLE subscriptions (id INTEGER PRIMARY KEY, status TEXT, '
                'traffic_limit_gb INTEGER, autopay_enabled BOOLEAN, is_trial BOOLEAN, tariff_id INTEGER)'
            )
        )
        if fallback_exists:
            connection.execute(text('INSERT INTO tariffs VALUES (5, 0)'))

        async def scalar(statement):
            return connection.scalar(statement)

        reasons, notes = await cards._quiet_facts(SimpleNamespace(scalar=scalar))
        assert ('trial_start_available' not in reasons) is fallback_exists
        assert 'trial_tariff_marked' in reasons, 'other trial letters still require a marked tariff'
    engine.dispose()
