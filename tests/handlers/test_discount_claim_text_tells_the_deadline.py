"""Текст «скидка активирована» обязан назвать срок, если срок есть.

Этап СК-1б сделал скидки волн «2-3 дня» и «5 дней» смертными. До него обещание
«применится при следующей оплате» было правдой всегда, теперь — только внутри срока.

⛔ Второй ключ локали заведён НЕ ради красоты: `_format_text_with_placeholders`
печатает `{expires_at}` буквально, если значения нет. Свести два текста в один —
показать человеку без срока фигурные скобки.
"""

import json
from pathlib import Path

import pytest

from app.localization.texts import get_texts


LOCALES = Path(__file__).resolve().parents[2] / 'app' / 'localization' / 'locales'


@pytest.mark.parametrize('language', ['ru', 'en'])
def test_both_claim_texts_exist_and_only_one_of_them_asks_for_a_deadline(language: str):
    data = json.loads((LOCALES / f'{language}.json').read_text(encoding='utf-8'))
    plain = data['DISCOUNT_CLAIM_SUCCESS']
    with_expiry = data['DISCOUNT_CLAIM_SUCCESS_WITH_EXPIRY']

    assert '{expires_at}' in with_expiry, 'текст со сроком обязан подставлять сам срок'
    assert '{expires_at}' not in plain, 'текст без срока не должен просить подстановку — покажет скобки'
    assert '{percent}' in plain and '{percent}' in with_expiry


@pytest.mark.parametrize('language', ['ru', 'en'])
def test_the_deadline_text_renders_with_a_real_value(language: str):
    texts = get_texts(language)
    rendered = texts.get('DISCOUNT_CLAIM_SUCCESS_WITH_EXPIRY').format(percent=13, expires_at='29.08.2026 06:20')

    assert '13' in rendered
    assert '29.08.2026 06:20' in rendered
    assert '{' not in rendered, 'непокрытая подстановка уедет человеку фигурными скобками'


def test_the_admin_promo_log_can_name_the_checkout_reason():
    """Владелец обязан прочитать причину по-русски, а не латиницей."""
    from app.handlers.admin.promo_offers import REASON_LABEL_KEYS

    key = REASON_LABEL_KEYS['device_first_checkout']
    for language in ('ru', 'en'):
        data = json.loads((LOCALES / f'{language}.json').read_text(encoding='utf-8'))
        assert key in data, f'{key} нет в {language}.json — экран покажет сырой ключ'
