"""РС-14а: ссылка кнопки рассылки проверяется целиком, а не по первым восьми буквам.

Опечатка в URL-кнопке — единственная оставшаяся дорога к «0 доставлено у 100 % аудитории»:
Телеграм отбивает кнопку `BUTTON_URL_INVALID` у КАЖДОГО получателя, а повторов у отправщика нет.
"""

import pytest
from pydantic import ValidationError

from app.cabinet.schemas.broadcasts import CustomBroadcastButton


def _url_button(value: str) -> CustomBroadcastButton:
    return CustomBroadcastButton(label='Открыть', action_type='url', action_value=value)


@pytest.mark.parametrize(
    'broken',
    [
        'https://пример.рф/акция ?utm=telegram',  # пробел внутри — прежняя проверка пропускала
        'https://teplo.example/акция летом',
        'https://',  # обрезано до схемы
        'tg://',
        'https://teplo.example/a\u200b',  # невидимый символ из копипасты
        'https://teplo.example/a\nb',
        'https://localhost/page',  # хост без точки недостижим для клиента
        'https://.example.com/page',
    ],
)
def test_broken_button_url_is_rejected(broken: str) -> None:
    with pytest.raises(ValidationError):
        _url_button(broken)


@pytest.mark.parametrize(
    'good',
    [
        'https://teplo.example/акция',
        'https://xn--e1afmkfd.xn--p1ai/promo?utm=telegram&id=7',
        'https://t.me/teplo_VPN_bot?start=promo',
        'tg://resolve?domain=teplo_VPN_bot',
    ],
)
def test_valid_button_url_still_accepted(good: str) -> None:
    assert _url_button(good).action_value == good


def test_surrounding_spaces_are_trimmed_not_rejected() -> None:
    """Пробелы по краям — след копипасты, а не опечатка: их снимаем, ссылку принимаем."""
    assert _url_button('  https://teplo.example/акция  ').action_value == 'https://teplo.example/акция'


def test_callback_buttons_are_not_touched_by_url_rules() -> None:
    """Забор ставится ТОЛЬКО на ссылки: callback с пробелом и без точки обязан жить."""
    button = CustomBroadcastButton(label='На главную', action_type='callback', action_value='menu_buy')
    assert button.action_value == 'menu_buy'
