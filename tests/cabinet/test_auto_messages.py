"""Сторожа раздела кабинета «Автосообщения».

Три вещи, которые здесь стерегутся, и почему именно они:

1. **Полнота каталога.** Экран обещает показать ВСЕ автоматические сообщения. Если в
   боте появится новое, а в каталоге его не будет, экран молча соврёт — поэтому
   сторож читает сам ``monitoring_service`` и требует соответствия.
2. **Забор на деньги.** Процент скидки может менять не только владелец, но и роль
   Marketer. Нулевая итоговая цена в старой кассе выдаёт платную подписку бесплатно.
3. **Честность управления.** У пятнадцати сообщений из двадцати выключателя в коде
   нет. ``PATCH`` по ним обязан отказывать, а не делать вид, что сохранил.
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
    GLOBALLY_SWITCHED_IDS,
    MAX_OFFER_DISCOUNT_PERCENT,
    SETTER_NAMES,
    AutoMessagePatch,
    GlobalPatch,
    _assert_discount_is_safe,
    patch_auto_message,
    patch_global_switch,
)
from app.services.notification_settings_service import NotificationSettingsService


_ROUTE = 'app.cabinet.routes.admin_auto_messages'


# ---------------------------------------------------------------------------
# 1. Полнота каталога
# ---------------------------------------------------------------------------


def test_catalog_covers_every_recorded_notification_type() -> None:
    """Каждый тип, который бот пишет в sent_notifications, есть в каталоге.

    Сторож намеренно читает ИСХОДНИК monitoring_service, а не список из этого же
    файла: иначе он сверялся бы сам с собой и пропустил бы новое сообщение.
    """
    source = Path('app/services/monitoring_service.py').read_text(encoding='utf-8')
    recorded = set(re.findall(r"record_notification\(\s*db,[^)]*?'([a-z0-9_]+)'", source, re.S))
    assert recorded, 'не удалось вычитать типы из monitoring_service — сторож ослеп'

    catalogued = {entry['sent_type'] for entry in AUTO_MESSAGE_CATALOG if entry.get('sent_type')}
    missing = recorded - catalogued
    assert not missing, f'бот отправляет типы, которых нет в каталоге экрана: {sorted(missing)}'


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


def test_setters_exist_and_match_catalog_params() -> None:
    """Пишем только именованными сеттерами — в каждом сидит свой ограничитель."""
    for setter in SETTER_NAMES.values():
        assert hasattr(NotificationSettingsService, setter), f'нет сеттера {setter}'

    for entry in AUTO_MESSAGE_CATALOG:
        key = entry.get('settings_key')
        for field in entry.get('params') or ():
            assert (key, field) in SETTER_NAMES, f'нет сеттера для {key}.{field}'


def test_globally_switched_ids_exist_in_catalog() -> None:
    assert GLOBALLY_SWITCHED_IDS <= set(CATALOG_BY_ID)
    # Общий выключатель гасит ПЯТЬ сообщений, а не все двадцать: так в monitoring_service.
    # Если это число поедет, подпись на экране станет ложью.
    assert len(GLOBALLY_SWITCHED_IDS) == 5
    assert len(AUTO_MESSAGE_CATALOG) > len(GLOBALLY_SWITCHED_IDS)


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
async def test_discount_is_rejected_when_promo_group_zeroes_the_price() -> None:
    """Даже маленький процент отбивается, если промогруппа уже обнуляет цену.

    Забор проверяет ИТОГ, а не поле: скидки перемножаются, и ноль в цене
    страшнее любого отдельного числа.
    """
    with patch(f'{_ROUTE}._max_promo_group_percent', AsyncMock(return_value=100)):
        with pytest.raises(HTTPException) as exc:
            await _assert_discount_is_safe(AsyncMock(), 10)
    assert exc.value.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert 'нулевой' in exc.value.detail


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
@pytest.mark.parametrize(('field', 'value'), [('valid_hours', 0), ('valid_hours', 999), ('trigger_days', 0)])
async def test_patch_rejects_out_of_range_values(field: str, value: int) -> None:
    payload = AutoMessagePatch(**{field: value})
    with patch.object(NotificationSettingsService, f'set_third_wave_{field}') as setter:
        with pytest.raises(HTTPException) as exc:
            await patch_auto_message('return-wave3', payload, AsyncMock(), MagicMock())
    assert exc.value.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    setter.assert_not_called()


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


@pytest.mark.asyncio
async def test_patch_unknown_message_is_404() -> None:
    with pytest.raises(HTTPException) as exc:
        await patch_auto_message('нет-такого', AutoMessagePatch(enabled=True), AsyncMock(), MagicMock())
    assert exc.value.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_global_switch_answers_with_the_value_that_actually_applies() -> None:
    """Если настройка задана в окружении, запись в базу на живого бота не влияет.

    Экран обязан сказать правду, а не отрапортовать успех: отвечаем действующим
    значением и признаком applied.
    """
    with (
        patch('app.services.system_settings_service.BotConfigurationService.set_value', AsyncMock()),
        patch.object(NotificationSettingsService, 'are_notifications_globally_enabled', return_value=True),
    ):
        result = await patch_global_switch(GlobalPatch(enabled=False), AsyncMock(), MagicMock())

    assert result['enabled'] is True
    assert result['applied'] is False
    assert result['affects'] == len(GLOBALLY_SWITCHED_IDS)
