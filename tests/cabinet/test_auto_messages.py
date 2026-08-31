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
    # Управляемых ровно пять — столько выключателей есть в боте.
    assert sum(1 for entry in AUTO_MESSAGE_CATALOG if entry['control'] == 'toggle') == 5


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
@pytest.mark.parametrize('message_id', ['trial-2h', 'paid-1d', 'grace-2d', 'low-balance'])
async def test_patch_on_unmanaged_message_is_refused(message_id: str) -> None:
    """У этих сообщений выключателя в коде нет. Отказ — единственный честный ответ."""
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
    assert _params_for(CATALOG_BY_ID['trial-2h']) is None


def test_limits_are_per_message_and_reach_the_screen() -> None:
    """Пол «через сколько дней» разный у разных сообщений — экран берёт его отсюда."""
    from app.cabinet.routes.admin_auto_messages import _limits_for

    assert _limits_for(CATALOG_BY_ID['return-wave3'])['trigger_days'][0] == 2
    assert _limits_for(CATALOG_BY_ID['trial-discount'])['trigger_days'][0] == 1
    assert _limits_for(CATALOG_BY_ID['return-wave2'])['discount_percent'] == [1, 50]
    assert _limits_for(CATALOG_BY_ID['trial-2h']) is None
