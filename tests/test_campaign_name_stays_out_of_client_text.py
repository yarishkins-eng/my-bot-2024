"""РЕК-1: внутреннее имя рекламной кампании не показывается клиенту.

Клиент, пришедший по рекламе, читал дословно: «🎉 Вы получили 50 ₽ за регистрацию по
кампании «Кувалда 7000₽»!» — то есть внутреннюю кличку кампании вместе с её бюджетом.
При пустой таблице `welcome_texts` (а на боевом она пуста) это первое и единственное
приветствие перед меню.

⛔ Лечение — НЕ переименование кампании в админке: имя несёт владельцу рекламный бюджет
и различает кампании на экране статистики. Имя убрано из КЛИЕНТСКОГО текста и намеренно
оставлено в АДМИНСКОМ уведомлении — третий тест это закрепляет, иначе следующий исполнитель
вычистит «за компанию» и то, по чему владелец различает кампании.

Сторож стережёт СВОЙСТВО («клиентское сообщение не называет кампанию»), а не букву фразы.

⚠️ ГРАНИЦА, названная честно (её нашла линза корректности мутациями, а не чтение):
сторож накрывает ключи с префиксом `CAMPAIGN_BONUS_` в `locales/*.json` и подпись проводки.
Клиентский текст, заведённый под ДРУГИМ именем ключа, он не увидит. Это цена того, что
префикс — единственный машинный признак «текст про бонус кампании», который у нас есть.
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

# 🔴 Имя С АМПЕРСАНДОМ выбрано намеренно. С «Кувалда 7000₽» `html.escape` — пустая операция,
# и сравнение проходило по совпадению: утечка имени, содержащего `&`, уезжала клиенту как
# `Кувалда &amp; Ко` и сторож её не видел. Имя владелец пишет руками, `&` в нём законен.
CAMPAIGN_NAME = 'Кувалда & Ко 7000₽'


# 🔴 Ключи перечислены ПОИМЁННО, а не посчитаны. Прежний сторож требовал «хотя бы 10 штук»
# и переживал удаление целого ключа из всех пяти локалей: 5×3=15, минус 5 = ровно 10.
# А `start.py:421` на пропавшем ключе роняет каждую рекламную регистрацию.
REQUIRED_KEYS = ('CAMPAIGN_BONUS_BALANCE', 'CAMPAIGN_BONUS_SUBSCRIPTION', 'CAMPAIGN_BONUS_TARIFF')


def _locale_files() -> list[pathlib.Path]:
    files = sorted(LOCALES.glob('*.json'))
    assert files, 'локалей не найдено — сторож обязан упасть, а не позеленеть на пустоте'
    return files


def _client_bonus_texts() -> dict[tuple[str, str], str]:
    found: dict[tuple[str, str], str] = {}
    for path in _locale_files():
        data = json.loads(path.read_text(encoding='utf-8'))
        for key, value in data.items():
            if key.startswith('CAMPAIGN_BONUS_') and isinstance(value, str):
                found[(path.stem, key)] = value
    return found


def test_every_locale_still_carries_every_bonus_key():
    """Пропажа ключа — не косметика: `texts.CAMPAIGN_BONUS_*` роняет регистрацию по рекламе."""
    texts = _client_bonus_texts()
    missing = [
        f'{path.stem}.json:{key}' for path in _locale_files() for key in REQUIRED_KEYS if (path.stem, key) not in texts
    ]
    assert not missing, 'пропали клиентские тексты бонуса кампании: ' + ', '.join(missing)


def test_no_client_bonus_text_in_any_locale_prints_the_campaign_name():
    texts = _client_bonus_texts()
    offenders = [f'{locale}.json:{key}' for (locale, key), value in texts.items() if CAMPAIGN_NAME_PLACEHOLDER in value]
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
        # Ищем ОБЕ формы: как имя пишет владелец и как его экранирует вызывающий код.
        for form in (CAMPAIGN_NAME, html.escape(CAMPAIGN_NAME)):
            assert form not in assembled, f'{locale}.json:{key} печатает клиенту имя кампании'


def _campaign() -> AdvertisingCampaign:
    """Настоящая модель, а не подделка: подпись бонуса читает её свойства (`is_balance_bonus`)."""
    # `is_active=True` задаётся ЯВНО: у колонки default применяется при вставке в базу,
    # а не при создании объекта в памяти — без него ветка бонуса отваливается на первой проверке.
    return AdvertisingCampaign(
        id=4,
        name=CAMPAIGN_NAME,
        start_parameter='teplo2',
        bonus_type='balance',
        balance_bonus_kopeks=5000,
        is_active=True,
        partner_user_id=None,
    )


def _service() -> AdminNotificationService:
    service = AdminNotificationService(bot=SimpleNamespace())
    service._is_enabled = lambda: True
    service._record_subscription_event = AsyncMock(return_value=None)
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
    # Админский текст хранит имя УЖЕ экранированным (`html.escape(campaign.name)`), поэтому
    # ищем именно эту форму: сравнение с сырым именем дало бы ложный отказ на имени с «&».
    assert html.escape(CAMPAIGN_NAME) in sent_text, f'владелец перестал видеть имя кампании: {label}'
    assert 'teplo2' in sent_text, f'владелец перестал видеть метку кампании: {label}'


@pytest.mark.asyncio
async def test_wallet_ledger_entry_never_carries_the_campaign_name(monkeypatch):
    """🔴 Вторая дверь, найденная волной 1: подпись проводки в истории кошелька.

    Её печатают КЛИЕНТУ две поверхности — «📊 История операций» бота
    (`app/handlers/balance/main.py`) и вкладка «Баланс» кабинета
    (`app/cabinet/routes/balance.py` → `Balance.tsx`). До РЕК-1 там стояло
    «Бонус за регистрацию по кампании 'Кувалда 7000₽'», и на боевом такие строки
    лежали у всех 114 пришедших по рекламе.

    ⛔ Проверяем ВЫЗОВОМ, а не чтением исходника: сторож, ищущий подстроку в файле,
    в этом проекте уже дважды оказывался пустым.
    """
    from app.services import campaign_service as module

    captured: dict[str, object] = {}

    async def fake_add_user_balance(db, user, amount, description=None, **kwargs):
        captured['description'] = description
        captured['amount'] = amount
        return True

    async def fake_record_campaign_registration(db, **kwargs):
        return (object(), True)

    monkeypatch.setattr(module, 'add_user_balance', fake_add_user_balance)
    monkeypatch.setattr(module, 'record_campaign_registration', fake_record_campaign_registration)

    result = await module.AdvertisingCampaignService().apply_campaign_bonus(
        AsyncMock(),
        SimpleNamespace(id=1, telegram_id=777),
        _campaign(),
    )

    assert result.success, 'бонус перестал начисляться — сторож обязан ловить и это'
    description = str(captured['description'])
    assert description, 'подпись проводки исчезла: человек перестанет понимать, откуда деньги'
    for form in (CAMPAIGN_NAME, html.escape(CAMPAIGN_NAME)):
        assert form not in description, f'имя кампании вернулось в историю кошелька клиента: {description!r}'
    # Причину начисления подпись называть ОБЯЗАНА: приветствие о ней больше не говорит,
    # и история кошелька — единственное оставшееся объяснение «откуда 50 ₽».
    assert 'регистрац' in description.lower(), f'подпись перестала называть причину начисления: {description!r}'
