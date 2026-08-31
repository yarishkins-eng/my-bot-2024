"""РЕК-1: внутреннее имя рекламной кампании не показывается клиенту.

Клиент, пришедший по рекламе, читал дословно: «🎉 Вы получили 50 ₽ за регистрацию по
кампании «Кувалда 7000₽»!» — то есть внутреннюю кличку кампании вместе с её бюджетом.
При пустой таблице `welcome_texts` (а на боевом она пуста) это первое и единственное
приветствие перед меню.

⛔ Лечение — НЕ переименование кампании в админке: имя несёт владельцу рекламный бюджет
и различает кампании на экране статистики. Имя убрано из КЛИЕНТСКОГО текста и намеренно
оставлено в АДМИНСКОМ уведомлении — третий тест это закрепляет, иначе следующий исполнитель
вычистит «за компанию» и то, по чему владелец различает кампании.

Сторож стережёт СВОЙСТВО («клиентское сообщение не называет кампанию»), а не букву фразы:
он берёт ВСЕ ключи семейства во ВСЕХ локалях, поэтому новый ключ не протащит имя молча.
"""

import html
import json
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import AdvertisingCampaign
from app.services.admin_notification_service import AdminNotificationService


LOCALES = pathlib.Path(__file__).resolve().parents[1] / 'app' / 'localization' / 'locales'

# Подстановка, которой имя кампании попадало в текст. Забор именно на неё: она и есть механизм.
CAMPAIGN_NAME_PLACEHOLDER = '{name}'

CAMPAIGN_NAME = 'Кувалда 7000₽'


def _client_bonus_texts() -> dict[tuple[str, str], str]:
    found: dict[tuple[str, str], str] = {}
    for path in sorted(LOCALES.glob('*.json')):
        data = json.loads(path.read_text(encoding='utf-8'))
        for key, value in data.items():
            if key.startswith('CAMPAIGN_BONUS_') and isinstance(value, str):
                found[(path.stem, key)] = value
    return found


def test_no_client_bonus_text_in_any_locale_prints_the_campaign_name():
    texts = _client_bonus_texts()
    # Защита от пустого прогона: если ключи переименуют, сторож обязан упасть, а не позеленеть.
    assert len(texts) >= 10, f'ожидались ключи бонуса кампании во всех локалях, найдено {len(texts)}'

    offenders = [
        f'{locale}.json:{key}' for (locale, key), value in texts.items() if CAMPAIGN_NAME_PLACEHOLDER in value
    ]
    assert not offenders, (
        'Имя рекламной кампании снова попало в текст, который читает КЛИЕНТ: '
        + ', '.join(sorted(offenders))
        + '. Имя кампании — внутреннее (в нём бюджет и различение кампаний), место ему '
        'в админском уведомлении и в статистике, а не в приветствии покупателя.'
    )


def test_assembled_client_message_never_carries_the_campaign_name():
    """Проверяем СОБРАННОЕ сообщение, а не шаблон: так его видит человек."""
    for (locale, key), template in _client_bonus_texts().items():
        # Ровно те аргументы, что подставляет `start.py` (лишний ключ `str.format` игнорирует).
        assembled = template.format(
            amount='50 ₽',
            name=html.escape(CAMPAIGN_NAME),
            days=30,
            traffic='5 ГБ',
            devices=1,
            tariff_name='Базовый',
        )
        assert CAMPAIGN_NAME not in assembled, f'{locale}.json:{key} печатает клиенту имя кампании'


def _campaign() -> AdvertisingCampaign:
    """Настоящая модель, а не подделка: подпись бонуса читает её свойства (`is_balance_bonus`)."""
    return AdvertisingCampaign(
        id=4, name=CAMPAIGN_NAME, start_parameter='teplo2', bonus_type='balance', balance_bonus_kopeks=5000
    )


def _service() -> AdminNotificationService:
    service = AdminNotificationService(bot=SimpleNamespace())
    service._is_enabled = lambda: True
    service._record_subscription_event = AsyncMock(return_value=None)
    service._record_campaign_link_visit = AsyncMock(return_value=None)
    service._get_user_promo_group = AsyncMock(return_value=None)
    service._send_message = AsyncMock(return_value=True)
    return service


async def _registration_notification(service: AdminNotificationService) -> None:
    await service.send_campaign_registration_notification(
        db=AsyncMock(),
        telegram_user_id=777,
        telegram_user_name='Тестовый',
        telegram_username=None,
        campaign=_campaign(),
        user=SimpleNamespace(id=1),
        bonus_type='balance',
        balance_kopeks=5000,
    )


async def _link_visit_notification(service: AdminNotificationService) -> None:
    await service.send_campaign_link_visit_notification(
        db=AsyncMock(),
        telegram_user=SimpleNamespace(id=777, full_name='Тестовый', username=None),
        campaign=_campaign(),
        user=None,
    )


# 🔴 Уведомлений про кампанию ДВА, и вскрыла это мутация, а не чтение: первый прогон заменил
# имя в переходе по ссылке, сторож промолчал — он стерёг только регистрацию. Закрываем оба:
# владелец различает кампании по обоим, и вычистить имя «за компанию» можно из любого.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('label', 'call'),
    [('регистрация по кампании', _registration_notification), ('переход по ссылке', _link_visit_notification)],
)
async def test_admin_notification_still_names_the_campaign(label, call):
    """⛔ Обратная сторона забора: владелец обязан ПРОДОЛЖАТЬ видеть, по какой кампании пришёл человек."""
    service = _service()

    await call(service)

    service._send_message.assert_awaited()
    sent_text = service._send_message.await_args.args[0]
    assert CAMPAIGN_NAME in sent_text, f'владелец перестал видеть имя кампании: {label}'
    assert 'teplo2' in sent_text, f'владелец перестал видеть метку кампании: {label}'
