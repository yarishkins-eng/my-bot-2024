"""Очередь сверки платежей не должна забиваться навсегда.

Замер на боевом 01.09.2026: выборка воркера (двадцать мест, порядок по сроку
следующей проверки) была целиком занята успешными выданными продажами. Они
остаются в `paid_processing` навсегда — это запись идемпотентности, — а ветка,
которая их обрабатывает, выходила через `continue` мимо назначения следующей
проверки. Срок у них не сдвигался, и живой платёж, ждавший досверки с 29.08,
не досматривался ни разу за три дня.
"""

from __future__ import annotations

import inspect

from app.services import device_first_payment_service as service


def test_backoff_grows_and_is_capped_at_an_hour() -> None:
    minutes = [int(service._reconcile_backoff(n).total_seconds() // 60) for n in range(10)]
    # Растёт: 1, 2, 4, 8, 16, 32, потом упирается в час.
    assert minutes[:6] == [1, 2, 4, 8, 16, 32]
    assert all(value == 60 for value in minutes[6:]), minutes
    # 🔴 Оба конца шкалы: проверка «не меньше минуты» прошла бы и с потолком в
    # неделю, то есть не отличала бы верное от сломанного.
    assert minutes[0] < minutes[3] < minutes[6]


def test_the_paid_branch_defers_instead_of_clogging_the_queue() -> None:
    """Ветка выданных продаж обязана отодвигать свой срок перед выходом.

    Сторож привязан к ТЕЛУ функции, а не к файлу: иначе он зеленел бы от
    любого упоминания отката где-нибудь ещё в модуле.
    """
    source = inspect.getsource(service.reconcile_device_first_payments)
    paid_branch = source[source.index("if attempt.status == 'paid_processing'") :]
    paid_branch = paid_branch[: paid_branch.index('\n        try:')]

    assert '_defer_settled_direct_attempt' in paid_branch, (
        'Ветка `paid_processing` снова выходит, не назначив следующую проверку. '
        'Такая строка остаётся в голове очереди навсегда и не пропускает живые платежи.'
    )
    # 🔴 Откат обязан стоять в `finally`, а не в `except`: выданная продажа
    # возвращается ШТАТНО и исключения не бросает. Первая редакция починки
    # стояла в `except` и на боевом не сдвинула бы ни одной строки из двадцати.
    assert paid_branch.index('finally:') < paid_branch.index('_defer_settled_direct_attempt'), (
        'Откат снова стоит в `except` — выданные продажи туда не попадают'
    )
    # И откат обязан стоять ДО освобождения аренды: после освобождения строка
    # уже не наша, и обновление молча ничего не изменит.
    assert paid_branch.index('_defer_settled_direct_attempt') < paid_branch.index('_release_direct_attempt_lease'), (
        'Откат обязан идти до освобождения аренды, иначе он не применится'
    )


def test_deferral_requires_an_owned_lease_and_is_not_silent() -> None:
    """Без своей аренды строку трогать нельзя — её мог взять другой проход."""
    source = inspect.getsource(service._defer_settled_direct_attempt)
    assert '_lock_owned_direct_attempt_lease' in source
    assert 'if lease_token is None or lease_epoch is None:' in source
    assert 'if attempt is None:' in source
    # Потеря аренды случается ровно тогда, когда батч медленный, — то есть
    # когда откат нужнее всего. Молчаливый отказ здесь невидим.
    assert 'logger.warning' in source


def test_only_a_delivered_sale_is_deferred() -> None:
    """Оплативший и ждущий подписку обязан сохранить быстрый повтор."""
    source = inspect.getsource(service._defer_settled_direct_attempt)
    assert "fulfillment_state == 'fulfilled'" in source
    assert "lifecycle_state.in_(['fulfilling', 'ready'])" in source
    assert 'if delivered is None:' in source, 'невыданный заказ обязан уходить без отката'


def test_one_backoff_formula_for_everyone() -> None:
    """Две копии формулы разошлись бы, и одна очередь снова забилась бы."""
    source = inspect.getsource(service.reconcile_device_first_payments)
    assert source.count('_reconcile_backoff(') == 1
    assert '2 ** min(' not in source, 'формула снова написана на месте, а не взята из общей'
