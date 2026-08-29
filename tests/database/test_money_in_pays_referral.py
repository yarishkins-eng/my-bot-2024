"""РФ-1 п.1.5: контракт «деньги зашли — рефералка обязана быть решена».

🔴 **Зачем.** Реферальная комиссия платится ровно из двух мест: `process_referral_topup`
(его зовут платёжные провайдеры) и очередь `device_first_deposit_outbox_service`. Главный
путь продаж — прямая оплата подписки — не звал НИ ОДНО из них с 02.08.2026 по 26.08.2026, и
через эту дыру мимо партнёров прошло 6 197 ₽. Никакой тест этого не заметил, потому что
никто не сторожил САМ ФАКТ появления нового денежного пути.

**Контракт:** любое место, создающее запись о приходе денег от пользователя, обязано либо
позвать `process_referral_topup`, либо завести работу через `ensure_deposit_outbox`.
Третьего пути нет. Появился новый — этот сторож краснеет и требует вписать вердикт.

⚠️ **Граница честно: это сторож ИНВЕНТАРЯ, а не поведения.** Он доказывает, что новый путь
не появился незамеченным. Он НЕ доказывает, что существующий платит верно — за это отвечают
сторожа в `tests/services/test_device_first_deposit_outbox_service.py` и
`tests/services/test_referral_service.py`.
"""

from __future__ import annotations

import ast
import collections
import pathlib


APP = pathlib.Path(__file__).resolve().parents[2] / 'app'

# Типы записей, означающие «деньги пришли от пользователя».
MONEY_IN_TYPES = {'DEPOSIT', 'PROVIDER_RECEIPT'}
# Способы создать такую запись.
CREATING_CALLS = {'Transaction', 'create_transaction', 'create_trans', 'add_user_balance'}

# 🔴 Ключ — ПАРА (файл, функция), никогда не номер строки: строки уезжают от любой правки,
# и сторож, привязанный к ним, превращается в шум.
# Значение — вердикт. Пустых записей быть не может: смысл списка в том, чтобы заставить
# ответить «платит и через что» или «не платит и почему».
KNOWN_MONEY_IN = {
    # --- платят через process_referral_topup (зовут сразу после зачисления) ---
    ('app/services/payment/antilopay.py', '_finalize_antilopay_payment'): 'платит через process_referral_topup',
    ('app/services/payment/aurapay.py', '_finalize_aurapay_payment'): 'платит через process_referral_topup',
    ('app/services/payment/cloudpayments.py', 'process_cloudpayments_pay_webhook'): (
        'платит через process_referral_topup'
    ),
    ('app/services/payment/cryptobot.py', 'process_cryptobot_webhook'): 'платит через process_referral_topup',
    ('app/services/payment/donut.py', '_finalize_donut_payment'): 'платит через process_referral_topup',
    ('app/services/payment/etoplatezhi.py', '_finalize_etoplatezhi_payment'): 'платит через process_referral_topup',
    ('app/services/payment/freekassa.py', '_finalize_freekassa_payment'): 'платит через process_referral_topup',
    ('app/services/payment/heleket.py', '_process_heleket_payload'): 'платит через process_referral_topup',
    ('app/services/payment/jupiter.py', '_finalize_jupiter_payment'): 'платит через process_referral_topup',
    ('app/services/payment/kassa_ai.py', '_finalize_kassa_ai_payment'): 'платит через process_referral_topup',
    ('app/services/payment/lava.py', '_finalize_lava_payment'): 'платит через process_referral_topup',
    ('app/services/payment/mulenpay.py', 'process_mulenpay_callback'): 'платит через process_referral_topup',
    ('app/services/payment/overpay.py', '_finalize_overpay_payment'): 'платит через process_referral_topup',
    ('app/services/payment/pal24.py', '_finalize_pal24_payment'): 'платит через process_referral_topup',
    ('app/services/payment/paypear.py', '_finalize_paypear_payment'): 'платит через process_referral_topup',
    ('app/services/payment/platega.py', '_finalize_platega_payment'): (
        'платит через process_referral_topup — единственный включённый на боевом шлюз'
    ),
    ('app/services/payment/riopay.py', '_finalize_riopay_payment'): 'платит через process_referral_topup',
    ('app/services/payment/rollypay.py', '_finalize_rollypay_payment'): 'платит через process_referral_topup',
    ('app/services/payment/severpay.py', '_finalize_severpay_payment'): 'платит через process_referral_topup',
    ('app/services/payment/wata.py', '_finalize_wata_payment'): 'платит через process_referral_topup',
    ('app/services/apple_iap.py', 'fulfill_verified_transaction'): 'платит через process_referral_topup',
    # --- платят через очередь device-first ---
    ('app/services/device_first_checkout_service.py', '_complete_direct_sale_locked'): (
        'платит через ensure_deposit_outbox на приход от банка — это и есть РФ-1 п.1.2б'
    ),
    ('app/services/device_first_payment_service.py', 'settle_device_first_platega_payment'): (
        'платит через ensure_deposit_outbox — довнесение недостачи к кошельку'
    ),
    ('app/services/device_first_checkout_service.py', 'refund_operator_review_checkout'): (
        'ПЛАТИТ через ensure_deposit_outbox: оператор кладёт проверенные деньги клиенту НА '
        'БАЛАНС — это приход, а не возврат на карту (РФ-4, 29.08.2026). '
        '⛔ ЗДЕСЬ СТОЯЛО ОБРАТНОЕ — «НЕ платит СОЗНАТЕЛЬНО… ⛔ Не трогать» — и запись пережила '
        'саму правку: сторож сверяет НАЛИЧИЕ вердикта, а не его истинность. То есть реестр, '
        'заведённый против потери обязательства перед партнёром, полчаса содержал прямую '
        'инструкцию это обязательство обнулить. Нашли линза денег и скептик независимо.'
    ),
    # --- НЕ платят, и это правильно ---
    ('app/cabinet/routes/admin_bulk_actions.py', '_do_add_balance'): (
        'НЕ платит: ручное начисление админом — подарок, а не деньги клиента'
    ),
    ('app/cabinet/routes/admin_users.py', 'update_user_balance'): (
        'НЕ платит: ручное начисление админом — подарок, а не деньги клиента'
    ),
    # --- НЕ платят, известные дыры с нулевым оборотом (отдельный этап) ---
    ('app/handlers/webhooks.py', 'tribute_webhook'): 'НЕ платит: обработчик не подключён нигде, 0 ₽',
    ('app/handlers/webhooks.py', 'handle_successful_payment'): 'НЕ платит: обработчик не подключён нигде, 0 ₽',
    ('app/services/tribute_service.py', 'force_process_payment'): 'НЕ платит: мёртвый код, вызывающих ноль, 0 ₽',
    ('app/services/device_first_payment_service.py', '_settle_direct_platega_payment_locked'): (
        'ПЛАТИТ: поздняя оплата заводит ту же durable-работу (РФ-3, закрыло мины U и BU)'
    ),
    # --- зачисление без явного типа: умолчание помощника = «приход» ---
    # 🔴 Эти одиннадцать мест сторож не видел вовсе, пока не научился читать умолчание.
    # Ровно так выглядит самый обычный способ зачислить деньги в этом проекте.
    ('app/services/campaign_service.py', '_apply_balance_bonus'): (
        'НЕ платит: бонус рекламной кампании — подарок, денег клиента нет'
    ),
    ('app/services/promocode_service.py', '_apply_promocode_effects'): (
        'НЕ платит: промокод — подарок, денег клиента нет'
    ),
    ('app/services/wheel_service.py', '_apply_prize'): 'НЕ платит: выигрыш в колесе — подарок, денег клиента нет',
    ('app/services/user_service.py', 'update_user_balance'): (
        'НЕ платит: ручное начисление админом — подарок или компенсация, решение владельца'
    ),
    ('app/webapi/routes/users.py', 'update_balance'): 'НЕ платит: ручная правка баланса через API — не деньги клиента',
    ('app/handlers/simple_subscription.py', 'confirm_simple_subscription_purchase'): (
        'НЕ платит: возврат за несостоявшуюся выдачу — деньги возвращаются, а не приходят'
    ),
    ('app/handlers/simple_subscription.py', 'handle_simple_subscription_pay_with_balance'): (
        'НЕ платит: оплата с баланса — комиссия уже взята на входе денег'
    ),
    ('app/services/apple_iap.py', '_handle_refund_reversed'): (
        'НЕ платит: отмена возврата Apple — восстановление, отдельный этап; 0 ₽, шлюз выключен'
    ),
    ('app/services/payment_service.py', 'add_user_balance'): (
        'ОБЁРТКА: тип задаёт вызывающий. 🔴 Новый шлюз, написанный против неё, обязан звать '
        'process_referral_topup сам — обёртка за него этого не делает'
    ),
    ('app/services/payment_service.py', 'create_transaction'): 'ОБЁРТКА: то же, тип задаёт вызывающий',
    ('app/services/referral_service.py', 'process_referral_purchase'): (
        'МЁРТВАЯ функция: вызовов ноль, помечена «INTENTIONALLY UNUSED». Сама и есть выплата, а не приход денег'
    ),
    # --- фундамент базы: править эти файлы запрещает предохранитель выкладки ---
    ('app/database/crud/transaction.py', 'create_unique_tribute_transaction'): (
        'app/database/** — правка запрещена предохранителем deploy.yml, разбирается отдельно'
    ),
}

# Места, где тип записи вычисляется переменной: обход его не видит, а дыры там водятся
# (платный триал через Stars и YooKassa — ровно отсюда).
KNOWN_DYNAMIC_TYPE = {
    ('app/database/crud/user.py', 'add_user_balance'): 'app/database/** — общий помощник, тип задаёт вызывающий',
    ('app/database/crud/user.py', 'add_user_balance_by_id'): 'app/database/** — то же',
    ('app/database/crud/user.py', 'subtract_user_balance'): 'app/database/** — списание, не приход',
    ('app/services/payment/stars.py', 'process_stars_payment'): (
        'платит через process_referral_topup; НО платный триал в stars_payments.py идёт мимо — 0 ₽, отдельный этап'
    ),
    ('app/services/payment/yookassa.py', '_process_successful_yookassa_payment'): (
        'платит через process_referral_topup; НО ветка платного триала идёт мимо — 0 ₽, отдельный этап'
    ),
}


def _scan() -> tuple[dict, list]:
    explicit: dict[tuple[str, str], list[int]] = collections.defaultdict(list)
    dynamic: list[tuple[str, str]] = []

    def declared_type(call: ast.Call) -> str | None:
        for kw in call.keywords:
            if kw.arg not in ('type', 'transaction_type'):
                continue
            value = kw.value
            if isinstance(value, ast.Attribute):
                if value.attr == 'value' and isinstance(value.value, ast.Attribute):
                    return value.value.attr
                return value.attr
            return '<переменная>'
        # 🔴 Найдено критиком полноты: без этой ветки сторож был слеп к САМОМУ обычному
        # способу зачислить деньги в этом проекте. У `add_user_balance` и `create_transaction`
        # умолчание параметра — DEPOSIT, поэтому вызов без явного типа означает «приход», а
        # обход возвращал `None` и молча пропускал такое место. Проверено мутацией: новая
        # функция с `add_user_balance(...)` без типа проходила сторожа насквозь.
        name = call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, 'attr', None)
        if name in ('add_user_balance', 'create_transaction', 'create_trans'):
            return 'DEPOSIT'
        return None

    for path in sorted(APP.rglob('*.py')):
        relative = path.relative_to(APP.parent).as_posix()
        tree = ast.parse(path.read_text(encoding='utf-8'))
        scope: list[str] = []

        class Walker(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                scope.append(node.name)
                self.generic_visit(node)
                scope.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, 'attr', None)
                if name in CREATING_CALLS:
                    kind = declared_type(node)
                    key = (relative, scope[-1] if scope else '<модуль>')
                    if kind in MONEY_IN_TYPES:
                        explicit[key].append(node.lineno)
                    elif kind == '<переменная>':
                        dynamic.append(key)
                self.generic_visit(node)

        Walker().visit(tree)

    return dict(explicit), sorted(set(dynamic))


def test_every_place_that_takes_money_has_a_written_verdict_about_the_referral():
    """Появился новый способ принять деньги — впиши, платит он комиссию или нет и почему."""
    explicit, _dynamic = _scan()

    new = sorted(set(explicit) - set(KNOWN_MONEY_IN))
    assert not new, (
        'Появилось место, принимающее деньги, без вердикта о реферальной комиссии.\n'
        'Впиши его в KNOWN_MONEY_IN и ответь: платит через что — или не платит и почему.\n'
        f'Новое: {new}'
    )

    gone = sorted(set(KNOWN_MONEY_IN) - set(explicit))
    assert not gone, f'Место исчезло или переименовано — обнови список: {gone}'


def test_places_where_the_transaction_type_is_computed_are_watched_separately():
    """Обход не видит тип, заданный переменной, — там и прячется платный триал двумя путями."""
    _explicit, dynamic = _scan()

    new = sorted(set(dynamic) - set(KNOWN_DYNAMIC_TYPE))
    assert not new, f'Новое место с вычисляемым типом записи, вердикта нет: {new}'

    gone = sorted(set(KNOWN_DYNAMIC_TYPE) - set(dynamic))
    assert not gone, f'Место с вычисляемым типом исчезло — обнови список: {gone}'


def test_the_verdict_list_never_holds_an_empty_answer():
    """Список без вердикта — это перечень имён, а не контракт: он ничего не заставляет объяснить."""
    for source in (KNOWN_MONEY_IN, KNOWN_DYNAMIC_TYPE):
        for key, verdict in source.items():
            assert verdict.strip(), f'у {key} пустой вердикт'
            assert len(verdict) > 20, f'у {key} вердикт слишком короткий, чтобы что-то объяснить'
