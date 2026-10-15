"""Скидочный хвост письма использует собственную цену и фактическую долю кошелька."""

from types import SimpleNamespace

import pytest

from app.localization.texts import get_texts
from app.services import monitoring_service as module


def options(*, percent=10, end='2026-10-15T21:30:00+00:00', trial=True):
    offer = {'discount_percent': percent, 'source': 'trial_ending_discount', 'expires_at': end}
    return {
        'eligible': True,
        'tariff': {'id': 31, 'base_device_limit': 1},
        'current_subscription': {'id': 2, 'is_trial': trial, 'tariff_id': 17},
        'price_matrix': [
            {
                'period_days': days,
                'prices': [
                    {
                        'device_limit': 1,
                        'price_kopeks': net,
                        'breakdown': {'promo_offer_discount_kopeks': before - net, 'promo_offer': offer},
                    }
                ],
            }
            for days, net, before in [(30, 13400, 14900), (90, 35900, 39900)]
        ],
    }


@pytest.mark.parametrize(
    'balance,minimum,expected',
    [
        (0, 100, '1 месяц — 134 ₽ вместо <s>149 ₽</s>'),
        (5000, 100, 'с вашими 50 ₽ на балансе — доплатить 84 ₽'),
        (5000, 10000, 'с вашими 34 ₽ на балансе — доплатить 100 ₽'),
        (13400, 100, '134 ₽ спишутся с баланса, доплачивать не нужно.'),
        (1490, 100, 'с вашими 14 ₽ на балансе — доплатить 120 ₽'),
    ],
)
def test_discount_receipt_uses_exact_breakdown(monkeypatch, balance, minimum, expected):
    monkeypatch.setattr(module.settings, 'PLATEGA_MIN_AMOUNT_KOPEKS', minimum)
    user = SimpleNamespace(balance_kopeks=balance)
    text = module.format_trial_discount_lines(options(), user, get_texts('ru'))
    assert expected in text
    assert '3 месяца — 359 ₽ вместо <s>399 ₽</s>' in text
    assert 'до 16.10.2026, 00:30 (МСК)' in text
    assert 'любой срок и число устройств' in text


@pytest.mark.parametrize('language,zone', [('ru', 'МСК'), ('en', 'MSK')])
@pytest.mark.parametrize(
    'traffic,shown',
    [(None, False), (0, False), (1_000_000, False), (1024**3 - 1, False), (1024**3, True), (True, False)],
)
def test_traffic_and_deadline_are_honest(language, zone, traffic, shown):
    text = module.format_trial_discount_lines(
        options(), SimpleNamespace(balance_kopeks=0), get_texts(language), traffic
    )
    assert f'16.10.2026, 00:30 ({zone})' in text
    assert ('ГБ' in text or 'GB' in text) is shown


@pytest.mark.parametrize(
    'reset,carry,trial,target,show',
    [
        (True, True, True, 31, True),
        (False, True, True, 31, False),
        (True, True, True, 17, False),
        (True, True, False, 31, False),
    ],
)
def test_start_now_depends_on_real_policy(monkeypatch, reset, carry, trial, target, show):
    monkeypatch.setattr(module.settings, 'TARIFF_SWITCH_RESET_FREE_DAYS', reset)
    monkeypatch.setattr(module.settings, 'TRIAL_ADD_REMAINING_DAYS_TO_PAID', carry)
    data = options(trial=trial)
    data['tariff']['id'] = target
    text = module.format_trial_discount_lines(data, SimpleNamespace(balance_kopeks=0), get_texts('ru'))
    assert ('Подписка начнётся сразу после оплаты' in text) is show


def test_no_personal_benefit_means_ordinary_letter():
    data = options()
    data['price_matrix'][0]['prices'][0]['breakdown']['promo_offer_discount_kopeks'] = 0
    assert module.format_trial_discount_lines(data, SimpleNamespace(balance_kopeks=0), get_texts('ru')) == ''
