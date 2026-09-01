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

    assert '_defer_direct_attempt_reconcile' in paid_branch, (
        'Ветка `paid_processing` снова выходит, не назначив следующую проверку. '
        'Такая строка остаётся в голове очереди навсегда и не пропускает живые платежи.'
    )
    # И откат обязан стоять ДО освобождения аренды: после освобождения строка
    # уже не наша, и обновление молча ничего не изменит.
    assert paid_branch.index('_defer_direct_attempt_reconcile') < paid_branch.index('_release_direct_attempt_lease'), (
        'Откат обязан идти до освобождения аренды, иначе он не применится'
    )


def test_deferral_requires_an_owned_lease() -> None:
    """Без своей аренды строку трогать нельзя — её мог взять другой проход."""
    source = inspect.getsource(service._defer_direct_attempt_reconcile)
    assert '_lock_owned_direct_attempt_lease' in source
    assert 'if lease_token is None or lease_epoch is None:' in source
    assert 'if attempt is None:' in source


def test_one_backoff_formula_for_everyone() -> None:
    """Две копии формулы разошлись бы, и одна очередь снова забилась бы."""
    source = inspect.getsource(service.reconcile_device_first_payments)
    assert source.count('_reconcile_backoff(') == 1
    assert '2 ** min(' not in source, 'формула снова написана на месте, а не взята из общей'
