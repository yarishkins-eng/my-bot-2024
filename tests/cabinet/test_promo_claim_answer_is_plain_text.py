"""Кабинет обязан отвечать текстом БЕЗ телеграмной разметки.

Экран рисует ответ внутри `<span>{message}</span>` (`PromoOffersSection.tsx:241`),
React экранирует — теги `<b>` уехали бы к человеку буквами.

🔴 Этот сторож проверяет МАРШРУТ, а не помощника. Сторож на одного помощника
мутацию «маршрут перестал его звать» переживает — так и случилось при первом
заходе (М29, М30), урок 18.08 про «тесты на функцию не доказывают подключение».
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes import promo as route


PERCENT = 13


def _offer(*, hours: int | None):
    return SimpleNamespace(
        id=404,
        user_id=7,
        claimed_at=None,
        is_active=True,
        expires_at=datetime.now(UTC) + timedelta(hours=6),
        effect_type='percent_discount',
        discount_percent=PERCENT,
        bonus_amount_kopeks=0,
        notification_type='expired_discount_wave2',
        extra_data={'active_discount_hours': hours} if hours else {},
    )


@pytest.mark.parametrize('hours', [29, None])
@pytest.mark.asyncio
async def test_the_claim_answer_never_carries_markup_or_line_breaks(monkeypatch, hours):
    offer = _offer(hours=hours)
    user = SimpleNamespace(
        id=7,
        language='ru',
        promo_offer_discount_percent=0,
        promo_offer_discount_source=None,
        promo_offer_discount_expires_at=None,
        updated_at=None,
    )
    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock(), flush=AsyncMock(), add=lambda _row: None)
    monkeypatch.setattr(route, 'get_offer_by_id', AsyncMock(return_value=offer))
    monkeypatch.setattr(route, 'mark_offer_claimed', AsyncMock())

    result = await route.claim_promo_offer(SimpleNamespace(offer_id=offer.id), user=user, db=db)

    assert result.success is True
    assert '<' not in result.message and '>' not in result.message, result.message
    assert '\n' not in result.message, result.message
    assert str(PERCENT) in result.message
    # Русская локаль, а не английская заглушка: человек читает на своём языке.
    assert 'Скидка' in result.message or 'скидк' in result.message.lower()
    if hours:
        assert result.expires_at is not None
        # Срок обязан быть НАЗВАН, иначе обещание «применится к оплате» снова врёт.
        assert '20' in result.message
    else:
        assert result.expires_at is None


@pytest.mark.asyncio
async def test_an_unknown_placeholder_never_turns_into_a_500(monkeypatch):
    """Клиентский маршрут не имеет права падать от лишней подстановки в тексте.

    В промо-шаблонах живут `{server_name}` и `{valid_hours}`. Голый `str.format`
    дал бы KeyError → 500 у клиента; бот в том же случае просто оставит подстановку
    видимой. Нашла ревизия плана после этапа.
    """
    offer = _offer(hours=29)
    user = SimpleNamespace(
        id=7,
        language='ru',
        promo_offer_discount_percent=0,
        promo_offer_discount_source=None,
        promo_offer_discount_expires_at=None,
        updated_at=None,
    )
    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock(), flush=AsyncMock(), add=lambda _row: None)
    monkeypatch.setattr(route, 'get_offer_by_id', AsyncMock(return_value=offer))
    monkeypatch.setattr(route, 'mark_offer_claimed', AsyncMock())
    monkeypatch.setattr(
        route,
        'get_texts',
        lambda _language: SimpleNamespace(
            get=lambda _key, _default=None: 'Скидка {percent}% до {expires_at} на {server_name}'
        ),
    )

    result = await route.claim_promo_offer(SimpleNamespace(offer_id=offer.id), user=user, db=db)

    assert result.success is True
    assert '13' in result.message
    # Неизвестная подстановка остаётся видимой — это честнее пятисотки.
    assert '{server_name}' in result.message
