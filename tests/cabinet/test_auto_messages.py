"""Сторожа раздела кабинета «Автосообщения».

Что здесь стерегут и почему именно это:

1. **Полнота каталога.** Экран обещает показать ВСЕ автоматические сообщения. Если в
   боте появится новое, а в каталоге его не будет, экран молча соврёт — поэтому
   сторож читает сам ``monitoring_service`` и требует соответствия.
2. **Забор на деньги.** Процент скидки может менять не только владелец, но и роль
   Marketer. Проверяется не только правка процента, но и ВКЛЮЧЕНИЕ сообщения:
   процент мог быть выставлен в сто из чат-админки бота, где потолка нет.
3. **Честность управления.** У пятнадцати сообщений из двадцати выключателя в коде
   нет. ``PATCH`` по ним обязан отказывать, а не делать вид, что сохранил.
4. **Маскирование клиентов.** Имена видит только тот, кому и так открыта клиентская
   база. Без этого сторожа маску можно снять, не покрасив ни одного теста.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, status

from app.cabinet.routes.admin_auto_messages import (
    AUTO_MESSAGE_CATALOG,
    CATALOG_BY_ID,
    GETTER_NAMES,
    GLOBALLY_SWITCHED_IDS,
    MAX_OFFER_DISCOUNT_PERCENT,
    SETTER_NAMES,
    TRIGGER_DAYS_MIN,
    AutoMessagePatch,
    _assert_discount_is_safe,
    _history_for,
    _max_promo_group_percent,
    patch_auto_message,
)
from app.services.notification_settings_service import NotificationSettingsService


_ROUTE = 'app.cabinet.routes.admin_auto_messages'
# От файла теста, а не от текущего каталога: относительный путь зеленел бы только
# при запуске pytest из корня репозитория.
_BOT_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 1. Полнота каталога
# ---------------------------------------------------------------------------


def test_catalog_covers_every_recorded_notification_type() -> None:
    """Каждый тип, который бот пишет в sent_notifications, есть в каталоге.

    Сторож читает ИСХОДНИК monitoring_service, а не список из этого же файла:
    иначе он сверялся бы сам с собой и пропустил бы новое сообщение.

    🔴 Имя сессии в регулярке НЕ зашито. Первая версия требовала буквального ``db,``
    и из-за этого пропустила единственный вызов, сделанный из батчевой сессии.
    """
    source = (_BOT_ROOT / 'app/services/monitoring_service.py').read_text(encoding='utf-8')
    recorded = set(re.findall(r"record_notification\(\s*\w+,[^)]*?'([a-z0-9_]+)'", source, re.DOTALL))
    assert len(recorded) >= 7, f'сторож ослеп: вычитал всего {len(recorded)} типов'

    catalogued = {entry['sent_type'] for entry in AUTO_MESSAGE_CATALOG if entry.get('sent_type')}
    missing = recorded - catalogued
    assert not missing, f'бот отправляет типы, которых нет в каталоге экрана: {sorted(missing)}'


def test_catalog_days_match_the_bot_settings() -> None:
    """Дни у «истекает через N» зашиты в каталог, а бот берёт их из настройки.

    Если настройку поменяют, надписи и счётчики станут ложью — пусть об этом
    скажет красный тест, а не менеджер, увидевший прочерк.
    """
    from app.config import settings

    catalog_days = {entry['sent_days'] for entry in AUTO_MESSAGE_CATALOG if entry.get('sent_days') is not None}
    assert catalog_days == set(settings.get_autopay_warning_days()), (
        'AUTOPAY_WARNING_DAYS разошлось с каталогом — обновите записи paid-*'
    )


def test_catalog_ids_are_unique_and_groups_known() -> None:
    ids = [entry['id'] for entry in AUTO_MESSAGE_CATALOG]
    assert len(ids) == len(set(ids)), 'дублирующиеся id в каталоге'
    assert set(CATALOG_BY_ID) == set(ids)
    assert {entry['group'] for entry in AUTO_MESSAGE_CATALOG} <= {'trial', 'paid', 'return', 'other'}
    assert {entry['control'] for entry in AUTO_MESSAGE_CATALOG} <= {'toggle', 'locked', 'server'}


def test_every_settings_key_exists_in_the_bot() -> None:
    """Ключи настроек — те же, что у сервиса. Опечатка здесь = молчаливый отказ."""
    known = set(NotificationSettingsService._DEFAULTS)
    used = {entry['settings_key'] for entry in AUTO_MESSAGE_CATALOG if entry.get('settings_key')}
    assert used <= known, f'каталог ссылается на несуществующие ключи: {sorted(used - known)}'
    # После АС-2 управляются девятнадцать из двадцати. Единственное исключение —
    # бонусные дни: там выключатель гасит не сообщение, а сам бонус.
    toggles = [entry for entry in AUTO_MESSAGE_CATALOG if entry['control'] == 'toggle']
    assert len(toggles) == 19
    assert [entry['id'] for entry in AUTO_MESSAGE_CATALOG if entry['control'] != 'toggle'] == ['grace-2d']


def test_setters_and_getters_exist_and_match_catalog_params() -> None:
    """Читаем и пишем только именованными методами — в каждом свой ограничитель."""
    for name in (*SETTER_NAMES.values(), *GETTER_NAMES.values()):
        assert hasattr(NotificationSettingsService, name), f'нет метода {name}'

    for entry in AUTO_MESSAGE_CATALOG:
        key = entry.get('settings_key')
        for field in entry.get('params') or ():
            assert (key, field) in SETTER_NAMES, f'нет сеттера для {key}.{field}'
            assert (key, field) in GETTER_NAMES, f'нет геттера для {key}.{field}'


def test_globally_switched_ids_exist_in_catalog() -> None:
    assert set(CATALOG_BY_ID) >= GLOBALLY_SWITCHED_IDS
    # Общий выключатель гасит ПЯТЬ сообщений, а не все двадцать: так в monitoring_service.
    assert len(GLOBALLY_SWITCHED_IDS) == 5
    assert len(AUTO_MESSAGE_CATALOG) > len(GLOBALLY_SWITCHED_IDS)


def test_no_global_switch_endpoint_is_exposed() -> None:
    """Общий выключатель отсюда не меняется: он пишет ключ из чужой зоны прав и
    вдобавок глушит уведомления клиентам об ответах поддержки."""
    from app.cabinet.routes.admin_auto_messages import router

    paths = {getattr(route, 'path', '') for route in router.routes}
    assert '/admin/auto-messages/global' not in paths


# ---------------------------------------------------------------------------
# 2. Забор на деньги
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discount_above_cap_is_rejected() -> None:
    with patch(f'{_ROUTE}._max_promo_group_percent', AsyncMock(return_value=0)):
        with pytest.raises(HTTPException) as exc:
            await _assert_discount_is_safe(AsyncMock(), MAX_OFFER_DISCOUNT_PERCENT + 1)
    assert exc.value.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.asyncio
async def test_discount_at_cap_is_allowed() -> None:
    with patch(f'{_ROUTE}._max_promo_group_percent', AsyncMock(return_value=50)):
        await _assert_discount_is_safe(AsyncMock(), MAX_OFFER_DISCOUNT_PERCENT)


@pytest.mark.asyncio
async def test_guard_does_not_lock_the_screen_when_a_group_already_zeroes_the_price() -> None:
    """Если цену обнулила промогруппа, правку запрещать нельзя.

    Иначе раздел запирается целиком, и сбить сбежавшую скидку станет невозможно
    ровно тогда, когда это нужнее всего.
    """
    with patch(f'{_ROUTE}._max_promo_group_percent', AsyncMock(return_value=100)):
        await _assert_discount_is_safe(AsyncMock(), 5)


@pytest.mark.asyncio
async def test_max_promo_group_percent_takes_the_worst_value() -> None:
    """Забор обязан смотреть на худшую группу, включая скидки по периодам."""
    db = AsyncMock()
    db.execute.return_value = MagicMock(all=lambda: [(10, 0, 0, None), (0, 5, 0, {'360': 70}), (0, 0, 20, {})])
    assert await _max_promo_group_percent(db) == 70


@pytest.mark.asyncio
async def test_max_promo_group_percent_survives_broken_period_data() -> None:
    db = AsyncMock()
    db.execute.return_value = MagicMock(all=lambda: [(None, None, None, {'30': 'ерунда'})])
    assert await _max_promo_group_percent(db) == 0


@pytest.mark.asyncio
async def test_patch_rejects_unsafe_discount_before_writing_anything() -> None:
    """Отказ обязан случиться ДО записи: половина применённых изменений хуже нуля."""
    payload = AutoMessagePatch(enabled=True, discount_percent=90)
    with (
        patch(f'{_ROUTE}._max_promo_group_percent', AsyncMock(return_value=0)),
        patch.object(NotificationSettingsService, 'set_enabled') as set_enabled,
        patch.object(NotificationSettingsService, 'set_second_wave_discount_percent') as set_percent,
    ):
        with pytest.raises(HTTPException) as exc:
            await patch_auto_message('return-wave2', payload, AsyncMock(), MagicMock())
    assert exc.value.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    set_enabled.assert_not_called()
    set_percent.assert_not_called()


@pytest.mark.asyncio
async def test_enabling_checks_the_percent_already_stored() -> None:
    """Тумблер тоже опасен: процент мог быть выставлен в сто из чат-админки бота."""
    with (
        patch(f'{_ROUTE}._max_promo_group_percent', AsyncMock(return_value=0)),
        patch(f'{_ROUTE}._params_for', return_value={'discount_percent': 100, 'valid_hours': 24}),
        patch.object(NotificationSettingsService, 'set_enabled') as set_enabled,
    ):
        with pytest.raises(HTTPException) as exc:
            await patch_auto_message('return-wave2', AutoMessagePatch(enabled=True), AsyncMock(), MagicMock())
    assert exc.value.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    set_enabled.assert_not_called()


@pytest.mark.asyncio
async def test_disabling_never_needs_the_money_guard() -> None:
    """Выключить можно всегда — иначе сбежавшую скидку нечем остановить."""
    with (
        patch(f'{_ROUTE}._params_for', return_value={'discount_percent': 100, 'valid_hours': 24}),
        patch.object(NotificationSettingsService, 'set_enabled', return_value=True) as set_enabled,
        patch(f'{_ROUTE}._quiet_facts', AsyncMock(return_value=({}, {}))),
        patch(f'{_ROUTE}._sent_counts', AsyncMock(return_value={})),
        patch(f'{_ROUTE}._claimed_counts', AsyncMock(return_value={})),
    ):
        await patch_auto_message('return-wave2', AutoMessagePatch(enabled=False), AsyncMock(), MagicMock())
    set_enabled.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(('field', 'value'), [('valid_hours', 169), ('trigger_days', 61)])
async def test_patch_rejects_out_of_range_values(field: str, value: int) -> None:
    payload = AutoMessagePatch.model_construct(**{field: value})
    with patch.object(NotificationSettingsService, f'set_third_wave_{field}') as setter:
        with pytest.raises(HTTPException) as exc:
            await patch_auto_message('return-wave3', payload, AsyncMock(), MagicMock())
    assert exc.value.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    setter.assert_not_called()


@pytest.mark.asyncio
async def test_trigger_days_lower_bound_is_per_message() -> None:
    """У третьей волны нижняя граница 2: сеттер поднял бы единицу молча, и ответ
    вернул бы не то, что просили."""
    assert TRIGGER_DAYS_MIN['expired_third_wave'] == 2
    with patch.object(NotificationSettingsService, 'set_third_wave_trigger_days') as setter:
        with pytest.raises(HTTPException) as exc:
            await patch_auto_message('return-wave3', AutoMessagePatch(trigger_days=1), AsyncMock(), MagicMock())
    assert exc.value.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    setter.assert_not_called()


@pytest.mark.asyncio
async def test_failed_disk_write_is_not_reported_as_success() -> None:
    """Сеттер вернул False — значит на диск не легло, и «сохранено» было бы ложью."""
    with (
        patch(f'{_ROUTE}._params_for', return_value={'discount_percent': 10, 'valid_hours': 24}),
        patch.object(NotificationSettingsService, 'set_enabled', return_value=False),
    ):
        with pytest.raises(HTTPException) as exc:
            await patch_auto_message('return-wave2', AutoMessagePatch(enabled=False), AsyncMock(), MagicMock())
    assert exc.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR


# ---------------------------------------------------------------------------
# 3. Честность управления
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize('message_id', ['grace-2d'])
async def test_patch_on_unmanaged_message_is_refused(message_id: str) -> None:
    """У бонусных дней выключателя нет: там рычаг гасит сам бонус, а не сообщение."""
    with patch.object(NotificationSettingsService, 'set_enabled') as set_enabled:
        with pytest.raises(HTTPException) as exc:
            await patch_auto_message(message_id, AutoMessagePatch(enabled=False), AsyncMock(), MagicMock())
    assert exc.value.status_code == status.HTTP_409_CONFLICT
    set_enabled.assert_not_called()


@pytest.mark.asyncio
async def test_patch_refuses_param_the_message_does_not_have() -> None:
    """У «первого дня без подписки» есть выключатель, но нет процента скидки."""
    with patch(f'{_ROUTE}._max_promo_group_percent', AsyncMock(return_value=0)):
        with pytest.raises(HTTPException) as exc:
            await patch_auto_message('return-day1', AutoMessagePatch(discount_percent=10), AsyncMock(), MagicMock())
    assert exc.value.status_code == status.HTTP_409_CONFLICT


def test_unknown_field_is_rejected_by_the_schema() -> None:
    """Опечатка в имени поля — отказ, а не тихий 200 «сохранено»."""
    with pytest.raises(ValueError):
        AutoMessagePatch(percent=90)


@pytest.mark.asyncio
async def test_patch_unknown_message_is_404() -> None:
    with pytest.raises(HTTPException) as exc:
        await patch_auto_message('нет-такого', AutoMessagePatch(enabled=True), AsyncMock(), MagicMock())
    assert exc.value.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# 4. Маскирование клиентов
# ---------------------------------------------------------------------------


def _history_db(rows: list[tuple]) -> AsyncMock:
    db = AsyncMock()
    db.execute.return_value = MagicMock(all=lambda: rows)
    return db


@pytest.mark.asyncio
async def test_history_hides_names_without_users_read() -> None:
    rows = [(None, 5521, 325525224, 'stanis_ya', 'stanis', None)]
    with patch(
        'app.services.permission_service.PermissionService.check_permission',
        AsyncMock(return_value=(False, 'denied')),
    ):
        history = await _history_for(_history_db(rows), CATALOG_BY_ID['return-wave2'], MagicMock(), MagicMock())
    assert history[0].user_ref == 'Клиент #5521'
    # Ни ника, ни имени, ни telegram_id в ответе быть не должно.
    assert 'stanis' not in history[0].user_ref
    assert '325525224' not in history[0].user_ref


@pytest.mark.asyncio
async def test_history_shows_names_with_users_read() -> None:
    rows = [(None, 5521, 325525224, 'stanis_ya', 'stanis', None)]
    with patch(
        'app.services.permission_service.PermissionService.check_permission',
        AsyncMock(return_value=(True, 'granted')),
    ):
        history = await _history_for(_history_db(rows), CATALOG_BY_ID['return-wave2'], MagicMock(), MagicMock())
    assert history[0].user_ref == '@stanis_ya'


@pytest.mark.asyncio
async def test_history_permission_check_receives_the_client_ip() -> None:
    """Без IP политика запрета по сети молча не применяется и маска снимается зря."""
    checker = AsyncMock(return_value=(True, 'granted'))
    with (
        patch('app.services.permission_service.PermissionService.check_permission', checker),
        patch('app.cabinet.ip_utils.get_client_ip', return_value='10.0.0.7'),
    ):
        await _history_for(_history_db([]), CATALOG_BY_ID['return-wave2'], MagicMock(), MagicMock())
    assert checker.await_args.kwargs.get('ip_address') == '10.0.0.7'


# ---------------------------------------------------------------------------
# 5. Причина молчания и уточнение — разные вещи
# ---------------------------------------------------------------------------


def test_note_never_masquerades_as_a_reason_to_be_silent() -> None:
    """«Низкий баланс» уходит — просто адресно. Оговорка про адресность не должна
    делать сообщение молчащим и занижать счётчик работающих."""
    from app.cabinet.routes.admin_auto_messages import _state_of

    entry = CATALOG_BY_ID['low-balance']
    notes = {'client_opt_in': 'уходит только тем, кто сам включил это в своих настройках'}
    with patch.object(NotificationSettingsService, 'are_notifications_globally_enabled', return_value=True):
        state, quiet_reason, note = _state_of(entry, {}, notes)

    assert state == 'live'
    assert quiet_reason is None, 'уточнение уехало в поле причины молчания'
    assert note == notes['client_opt_in']


def test_real_reason_still_makes_the_message_quiet() -> None:
    from app.cabinet.routes.admin_auto_messages import _state_of

    entry = CATALOG_BY_ID['grace-2d']
    with patch.object(NotificationSettingsService, 'are_notifications_globally_enabled', return_value=True):
        state, quiet_reason, note = _state_of(entry, {'grace_enabled': 'выключено на сервере'}, {})

    assert state == 'quiet'
    assert quiet_reason == 'выключено на сервере'
    assert note is None


# ---------------------------------------------------------------------------
# 6. Права привязаны к маршрутам
# ---------------------------------------------------------------------------


def test_routes_require_the_right_permissions() -> None:
    """Чтение и запись разведены НА МАРШРУТАХ, а не только в реестре прав.

    🔴 Остальные тесты зовут обработчики напрямую, минуя зависимости FastAPI, —
    подмена edit на read в декораторе прошла бы мимо всех них.
    """
    from app.cabinet.routes.admin_auto_messages import router

    required: dict[tuple[str, str], set[str]] = {}
    for route in router.routes:
        codes: set[str] = set()
        # Право зашито в замыкании зависимости — достаём из свободных переменных.
        for dep in getattr(getattr(route, 'dependant', None), 'dependencies', []):
            call = getattr(dep, 'call', None)
            closure = getattr(call, '__closure__', None) or ()
            for cell in closure:
                value = cell.cell_contents
                if isinstance(value, tuple) and value and isinstance(value[0], str) and ':' in value[0]:
                    codes.update(value)
        for method in getattr(route, 'methods', set()):
            required[(method, getattr(route, 'path', ''))] = codes

    assert required, 'не удалось вычитать права маршрутов — сторож ослеп'
    for (method, path), codes in required.items():
        assert codes, f'{method} {path} не требует ни одного права'
        if method == 'PATCH':
            assert 'auto_messages:edit' in codes, f'{method} {path} пускает на запись без edit'
        else:
            assert codes == {'auto_messages:read'}, f'{method} {path} требует неожиданное: {codes}'


def test_zero_percent_is_refused() -> None:
    """Ноль процентов — это письмо «Скидка 0%», а не выключение."""
    from app.cabinet.routes.admin_auto_messages import MIN_OFFER_DISCOUNT_PERCENT

    assert MIN_OFFER_DISCOUNT_PERCENT >= 1


@pytest.mark.asyncio
async def test_zero_percent_is_rejected_by_the_guard() -> None:
    with patch(f'{_ROUTE}._max_promo_group_percent', AsyncMock(return_value=0)):
        with pytest.raises(HTTPException) as exc:
            await _assert_discount_is_safe(AsyncMock(), 0)
    assert exc.value.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.asyncio
async def test_empty_patch_is_refused_rather_than_reported_as_saved() -> None:
    with pytest.raises(HTTPException) as exc:
        await patch_auto_message('return-wave2', AutoMessagePatch(), AsyncMock(), MagicMock())
    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST


def test_message_with_a_switch_but_no_numbers_is_still_manageable() -> None:
    """У «первого дня без подписки» есть выключатель и нет числовых полей.

    Пустой словарь, а не None: None экран читал как «управлять нельзя» и говорил
    это про управляемое сообщение.
    """
    from app.cabinet.routes.admin_auto_messages import _params_for

    assert _params_for(CATALOG_BY_ID['return-day1']) == {}
    assert _params_for(CATALOG_BY_ID['trial-2h']) == {'warn_hours': 2}
    # У бонусных дней ключа настроек нет вовсе — только там None.
    assert _params_for(CATALOG_BY_ID['grace-2d']) is None


def test_limits_are_per_message_and_reach_the_screen() -> None:
    """Пол «через сколько дней» разный у разных сообщений — экран берёт его отсюда."""
    from app.cabinet.routes.admin_auto_messages import _limits_for

    assert _limits_for(CATALOG_BY_ID['return-wave3'])['trigger_days'][0] == 2
    assert _limits_for(CATALOG_BY_ID['trial-discount'])['trigger_days'][0] == 1
    assert _limits_for(CATALOG_BY_ID['return-wave2'])['discount_percent'] == [1, 50]
    assert _limits_for(CATALOG_BY_ID['trial-2h'])['warn_hours'] == [1, 48]
    assert _limits_for(CATALOG_BY_ID['grace-2d']) is None


# ---------------------------------------------------------------------------
# 7. Выключатель обязан что-то выключать
# ---------------------------------------------------------------------------


# Пять ключей, которые бот читает не общим `is_enabled`, а именованными геттерами —
# они существовали до АС-2 и переписывать их незачем.
_NAMED_GETTER_KEYS = {
    'trial_channel_unsubscribed': 'is_trial_channel_unsubscribed_enabled',
    'expired_1d': 'is_expired_1d_enabled',
    'expired_second_wave': 'is_second_wave_enabled',
    'expired_third_wave': 'is_third_wave_enabled',
    'trial_expired_discount': 'is_trial_expired_discount_enabled',
}


def _bot_sending_sources() -> str:
    """Исходники обеих служб, которые реально отправляют сообщения клиентам."""
    return '\n'.join(
        (_BOT_ROOT / name).read_text(encoding='utf-8')
        for name in ('app/services/monitoring_service.py', 'app/services/daily_subscription_service.py')
    )


def test_every_switch_actually_guards_sending() -> None:
    """У каждого переключателя в разделе есть забор в коде бота.

    🔴 Это главный сторож АС-2. Владелец поймал ровно обратное: экран говорил
    «выключить нельзя» там, где он этого не просил. Обратная беда опаснее —
    нарисовать тумблер, который ничего не выключает. Тест читает исходники обеих
    отправляющих служб и требует, чтобы ключ там встречался.
    """
    source = _bot_sending_sources()
    missing = []
    for entry in AUTO_MESSAGE_CATALOG:
        if entry['control'] != 'toggle':
            continue
        key = entry['settings_key']
        named = _NAMED_GETTER_KEYS.get(key)
        guarded = f"is_enabled('{key}')" in source or (named and f'{named}()' in source)
        if not guarded:
            missing.append(f'{entry["id"]} → {key}')
    assert not missing, 'переключатель есть, а забора в боте нет: ' + ', '.join(missing)


def test_messages_sharing_a_switch_say_so() -> None:
    """Если один ключ гасит два сообщения — оба обязаны об этом сказать.

    Молчание здесь и есть та ложь, которую экран не имеет права допускать:
    человек выключает одно, а замолкают два.
    """
    by_key: dict[str, list[dict]] = {}
    for entry in AUTO_MESSAGE_CATALOG:
        if entry['control'] == 'toggle':
            by_key.setdefault(entry['settings_key'], []).append(entry)

    for key, entries in by_key.items():
        if len(entries) == 1:
            assert not entries[0].get('shares_switch_with'), f'{entries[0]["id"]}: ключ {key} ни с кем не делится'
            continue
        assert len(entries) == 2, f'ключ {key} гасит больше двух сообщений — подпись на экране не рассчитана'
        first, second = entries
        assert first.get('shares_switch_with') == second['title'], f'{first["id"]} молчит про пару'
        assert second.get('shares_switch_with') == first['title'], f'{second["id"]} молчит про пару'


def test_dangerous_messages_explain_the_consequence() -> None:
    """Там, где выключение оставит клиента без объяснения, это написано словами."""
    dangerous = {'trial-expired', 'paid-expired', 'traffic-80', 'daily-paused', 'autopay-final'}
    for message_id in dangerous:
        warning = CATALOG_BY_ID[message_id].get('warning')
        assert warning, f'{message_id}: последствие выключения не названо'
        assert len(warning) > 40, f'{message_id}: предупреждение слишком короткое, чтобы что-то объяснить'


def test_trigger_days_cap_matches_the_bot_lookback() -> None:
    """Потолок «через сколько дней» не может быть больше окна выборки бота.

    Моя мина из АС-1: потолок стоял 60, а бот смотрит назад на 30 — выставить
    можно было, а сообщение не ушло бы никогда.
    """
    from app.cabinet.routes.admin_auto_messages import MAX_TRIGGER_DAYS

    source = (_BOT_ROOT / 'app/services/monitoring_service.py').read_text(encoding='utf-8')
    lookbacks = {int(days) for days in re.findall(r'lookback = now - timedelta\(days=(\d+)\)', source)}
    assert lookbacks, 'не удалось вычитать окно выборки — сторож ослеп'
    assert min(lookbacks) >= MAX_TRIGGER_DAYS, (
        f'потолок {MAX_TRIGGER_DAYS} больше окна выборки {min(lookbacks)} — сообщение не уйдёт никогда'
    )


# ---------------------------------------------------------------------------
# 8. Час пробного: число живёт в одном месте, а не в трёх
# ---------------------------------------------------------------------------


def test_trial_hours_are_not_hardcoded_anymore() -> None:
    """Ни момент отправки, ни текст сообщения больше не зашивают двойку.

    🔴 Число жило в трёх местах: порог выборки, текст письма и подпись в разделе.
    Поменять одно и забыть остальные — значит слать за три часа и писать «через два».
    """
    source = (_BOT_ROOT / 'app/services/monitoring_service.py').read_text(encoding='utf-8')
    assert 'get_trial_warn_hours()' in source, 'момент отправки не читает настройку'
    assert 'истекает через 2 часа' not in source, 'в тексте письма осталась зашитая двойка'
    assert 'timedelta(hours=warn_hours)' in source, 'порог выборки не следует за настройкой'


def test_trial_hours_floor_is_the_monitoring_interval() -> None:
    """Нижняя граница не может быть меньше того, как часто бот обходит клиентов.

    Окно уже промежутка между обходами цикл просто перешагнёт, и большинство
    клиентов не получит предупреждения — молча.
    """
    from app.cabinet.routes.admin_auto_messages import MIN_WARN_HOURS
    from app.config import settings

    interval_hours = settings.MONITORING_INTERVAL / 60
    assert MIN_WARN_HOURS >= interval_hours, (
        f'граница {MIN_WARN_HOURS} ч меньше промежутка между обходами {interval_hours} ч'
    )


def test_label_follows_the_setting() -> None:
    """Подпись на экране обязана показывать то же число, что применит бот."""
    from app.cabinet.routes.admin_auto_messages import _resolve_when

    entry = CATALOG_BY_ID['trial-2h']
    assert '3 часа' in _resolve_when(entry, {'warn_hours': 3})
    assert '1 час' in _resolve_when(entry, {'warn_hours': 1})
    assert '5 часов' in _resolve_when(entry, {'warn_hours': 5})


def test_message_text_uses_the_same_number_as_the_threshold() -> None:
    """Текст письма собирается из того же числа, которым отобрали получателей."""
    from app.utils.formatters import format_hours_declension

    assert format_hours_declension(1) == '1 час'
    assert format_hours_declension(2) == '2 часа'
    assert format_hours_declension(5) == '5 часов'
    assert format_hours_declension(11) == '11 часов'
    assert format_hours_declension(21) == '21 час'


@pytest.mark.asyncio
async def test_saving_trial_hours_reaches_the_setter() -> None:
    """Новое поле реально сохраняется, а не отбрасывается перебором старых трёх."""
    with (
        patch(f'{_ROUTE}._params_for', return_value={'warn_hours': 2}),
        patch.object(NotificationSettingsService, 'set_trial_warn_hours', return_value=True) as setter,
        patch(f'{_ROUTE}._quiet_facts', AsyncMock(return_value=({}, {}))),
        patch(f'{_ROUTE}._sent_counts', AsyncMock(return_value={})),
        patch(f'{_ROUTE}._claimed_counts', AsyncMock(return_value={})),
    ):
        await patch_auto_message('trial-2h', AutoMessagePatch(warn_hours=3), AsyncMock(), MagicMock())
    setter.assert_called_once_with(3)


@pytest.mark.asyncio
async def test_trial_hours_below_the_floor_are_refused() -> None:
    """Меньше часа не пропускаем: цикл обходит клиентов раз в час."""
    with patch.object(NotificationSettingsService, 'set_trial_warn_hours') as setter:
        with pytest.raises(HTTPException) as exc:
            await patch_auto_message(
                'trial-2h', AutoMessagePatch.model_construct(warn_hours=0), AsyncMock(), MagicMock()
            )
    assert exc.value.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    setter.assert_not_called()


def test_numeric_fields_registry_covers_every_catalog_param() -> None:
    """Поле, добавленное в каталог мимо общего набора, молча не сохранилось бы."""
    from app.cabinet.routes.admin_auto_messages import _NUMERIC_FIELDS

    used = {field for entry in AUTO_MESSAGE_CATALOG for field in (entry.get('params') or ())}
    assert used <= set(_NUMERIC_FIELDS), f'поля вне общего набора: {sorted(used - set(_NUMERIC_FIELDS))}'
    assert set(AutoMessagePatch.model_fields) - {'enabled'} == set(_NUMERIC_FIELDS)


def test_subscriptions_declension() -> None:
    """«у 1 подписок» — то, что владелец увидел на экране. Больше не должно быть."""
    from app.utils.formatters import format_subscriptions_declension

    assert format_subscriptions_declension(1) == '1 подписки'
    assert format_subscriptions_declension(2) == '2 подписок'
    assert format_subscriptions_declension(11) == '11 подписок'
    assert format_subscriptions_declension(21) == '21 подписки'
