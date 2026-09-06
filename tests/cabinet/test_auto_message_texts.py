"""Сторожа этапа АС-10: владелец видит на карточке ТОТ текст, который уходит клиенту.

Проверяется не то, что текст красивый, а одно свойство: показанное и отправленное —
это одна и та же строка из одного и того же места. Копия текста в каталоге сделала бы
экран правдоподобным и ложным одновременно, и ни один прежний тест этого не ловил.
"""

import ast
import pathlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.cabinet.routes.admin_auto_messages import (
    _CABINET_LINK_IDS,
    _INSERT_KEYS,
    _LOCALE_TEXT_KEYS,
    AUTO_MESSAGE_CATALOG,
    _const_texts,
    _source_text_of,
    _text_facts,
    _validate_new_text,
)


_BOT_ROOT = pathlib.Path(__file__).resolve().parents[2]
_MONITORING = 'app/services/monitoring_service.py'
_DAILY = 'app/services/daily_subscription_service.py'
_CHANNEL_CHECKER = 'app/middlewares/channel_checker.py'
_SENDING_MODULES = (_MONITORING, _DAILY)

# Длиннее этого литерал уже не служебная строка, а письмо человеку.
_LETTER_LENGTH = 60


def test_every_card_shows_a_text() -> None:
    """Пустая карточка — это ровно тот случай, ради которого этап затеян."""
    silent = [entry['id'] for entry in AUTO_MESSAGE_CATALOG if not (_text_facts(entry['id'])['text'] or '').strip()]
    assert not silent, 'карточка без текста письма: ' + ', '.join(silent)


def test_the_text_map_covers_the_catalog_exactly() -> None:
    """Ни одного лишнего id и ни одного забытого: карта и каталог обязаны совпасть.

    Лишний id — это текст, который никому не показывается; забытый — карточка,
    молчащая о своём письме. Оба случая тихие, поэтому проверяются числом.
    """
    known = set(_LOCALE_TEXT_KEYS) | set(_const_texts())
    catalog = {entry['id'] for entry in AUTO_MESSAGE_CATALOG}
    assert known == catalog, f'лишние: {sorted(known - catalog)}; забытые: {sorted(catalog - known)}'
    assert not (set(_LOCALE_TEXT_KEYS) & set(_const_texts())), 'у сообщения два источника текста сразу'


@pytest.mark.parametrize('module_name', _SENDING_MODULES)
def test_no_sender_builds_its_letter_from_an_f_string(module_name: str) -> None:
    """Текст письма нельзя собирать прямо в отправителе — иначе его не показать.

    Так было до АС-10: восемь писем жили f-строками, подстановка была вшита в
    литерал, и отделить текст от подстановки было нечем. Сторож держит границу:
    вернётся f-строка — покраснеет здесь, а не через месяц на карточке.
    """
    tree = ast.parse((_BOT_ROOT / module_name).read_text(encoding='utf-8'))
    offenders: list[str] = []

    def written_length(value: ast.expr) -> int:
        """Сколько букв литерала в выражении — БЕЗ подстановок.

        Смотрим только на само выражение, а не внутрь вызовов: запасной текст
        внутри ``texts.t(КЛЮЧ, '...')`` — законная идиома проекта, там ключ
        и есть источник. Ловим ровно голый литерал, из которого письмо собрано
        на месте.
        """
        if isinstance(value, ast.Constant):
            return len(value.value) if isinstance(value.value, str) else 0
        if isinstance(value, ast.JoinedStr):
            return sum(
                len(part.value)
                for part in value.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
            return written_length(value.left) + written_length(value.right)
        return 0

    def looks_like_a_letter(value: ast.expr) -> bool:
        return written_length(value) > _LETTER_LENGTH

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            named = any(isinstance(target, ast.Name) and target.id == 'message' for target in targets)
            if named and looks_like_a_letter(node.value):
                offenders.append(f'строка {node.lineno}: message = <литерал письма>')
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in ('text', 'caption', 'telegram_message') and looks_like_a_letter(keyword.value):
                    offenders.append(f'строка {node.lineno}: {keyword.arg}=<литерал письма>')

    assert not offenders, f'{module_name}: ' + '; '.join(offenders)


@pytest.mark.asyncio
async def test_the_shown_text_is_the_one_that_is_actually_sent() -> None:
    """Настоящий отправитель вызывается по-настоящему, и отправленное сверяется с показанным.

    Это единственный сторож, который отвечает на вопрос «доезжает ли»: остальные
    читают файлы. Мутация «показывать копию» краснит именно его.
    """
    from app.services.monitoring_service import MonitoringService

    service = MonitoringService.__new__(MonitoringService)
    sent: dict[str, str] = {}

    async def capture(**kwargs):
        sent['text'] = kwargs['text']
        return True

    service._send_message_with_logo = capture
    # Числа намеренно не совпадают ни с одним умолчанием соседнего кода.
    user = SimpleNamespace(id=907, telegram_id=5207068834, language='ru')
    subscription = SimpleNamespace(id=233, end_date=datetime.now(UTC) + timedelta(hours=7), tariff=None)

    assert await service._send_trial_ending_notification(user, subscription, warn_hours=9) is True

    shown = _text_facts('trial-2h')['text']
    skeleton = shown.split('{')[0]
    assert skeleton and skeleton in sent['text'], 'отправлено не то, что показано на карточке'
    tail = shown.rsplit('}', 1)[-1]
    assert tail and sent['text'].endswith(tail), 'хвост письма на карточке не совпал с отправленным'


# Кто ИМЕННО шлёт каждое сообщение. Таблица написана здесь заново и намеренно: сторож
# обязан утверждать правду сам, а не сверяться с той же картой, которую проверяет.
# Без неё обе подмены проходили насквозь — и подмена ключа словаря (карточка «Трафик»
# показывала бы письмо про баланс), и подмена константы на выдуманную копию, то есть
# ровно то, что владелец запретил прямым текстом.
_SENDER_OF: dict[str, tuple[str, str, str]] = {
    # id: (модуль, функция-отправитель, имя ключа локали ИЛИ имя константы)
    'trial-not-connected': (_MONITORING, '_send_trial_not_connected_notification', 'TRIAL_NOT_CONNECTED_TEXT'),
    'trial-2h': (_MONITORING, '_send_trial_ending_notification', 'TRIAL_ENDING_TEXT'),
    'trial-expired': (_MONITORING, '_send_trial_expired_notification', 'TRIAL_EXPIRED_NOTIFICATION'),
    'trial-discount': (_MONITORING, '_send_trial_expired_discount_notification', 'TRIAL_EXPIRED_DISCOUNT'),
    'paid-3d': (_MONITORING, '_send_subscription_expiring_notification', 'SUBSCRIPTION_EXPIRING_PAID'),
    'paid-1d': (_MONITORING, '_send_subscription_expiring_notification', 'SUBSCRIPTION_EXPIRING_PAID'),
    'paid-expired': (_MONITORING, '_send_subscription_expired_notification', 'SUBSCRIPTION_EXPIRED_TEXT'),
    'return-day1': (_MONITORING, '_send_expired_day1_notification', 'SUBSCRIPTION_EXPIRED_1D'),
    'return-wave2': (_MONITORING, '_send_expired_discount_notification', 'SUBSCRIPTION_EXPIRED_SECOND_WAVE'),
    'return-wave3': (_MONITORING, '_send_expired_discount_notification', 'SUBSCRIPTION_EXPIRED_THIRD_WAVE'),
    'traffic-80': (_MONITORING, '_check_traffic_warnings', 'TRAFFIC_WARNING_ALERT'),
    'channel-left': (_MONITORING, '_send_trial_channel_unsubscribed_notification', 'TRIAL_CHANNEL_UNSUBSCRIBED'),
    'channel-back': (
        _CHANNEL_CHECKER,
        '_reactivate_subscription_on_subscribe',
        'SUBSCRIPTION_REACTIVATED_CHANNEL_SUBSCRIBE',
    ),
    'grace-2d': (_MONITORING, '_send_grace_started_notification', 'GRACE_STARTED_TEXT'),
    'low-balance': (_MONITORING, '_check_low_balance_alerts', 'LOW_BALANCE_ALERT'),
    'autopay-ok': (_MONITORING, '_send_autopay_success_notification', 'AUTOPAY_SUCCESS'),
    'autopay-fail': (_MONITORING, '_send_autopay_failed_notification', 'AUTOPAY_FAILED'),
    'autopay-final': (_MONITORING, '_send_autopay_failed_notification', 'AUTOPAY_FAILED_FINAL'),
    'autopay-legacy': (_MONITORING, '_process_autopayments', 'AUTOPAY_LEGACY_TEXT'),
    'daily-charge': (_DAILY, '_notify_daily_charge', 'DAILY_CHARGE_TEXT'),
    'daily-paused': (_DAILY, '_notify_insufficient_balance', 'DAILY_PAUSED_TEXT'),
    'traffic-reset': (_DAILY, '_notify_traffic_reset', 'TRAFFIC_RESET_TEXT'),
}


def _function_node(module_name: str, function_name: str) -> ast.AST:
    source = (_BOT_ROOT / module_name).read_text(encoding='utf-8')
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return node
    raise AssertionError(f'{module_name}: функции {function_name} больше нет')


def _names_used_in(node: ast.AST) -> set[str]:
    """Имена, которые функция реально называет, — по узлам дерева, не по тексту.

    Текстовый поиск засчитывал бы имя, упомянутое в комментарии или в докстринге,
    то есть ровно там, где оно ничего не делает.
    """
    used: set[str] = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name):
            used.add(inner.id)
        elif isinstance(inner, ast.Attribute):
            used.add(inner.attr)
        elif isinstance(inner, ast.Constant) and isinstance(inner.value, str):
            used.add(inner.value)
    return used


def test_every_card_names_the_source_its_own_sender_uses() -> None:
    """Карточка читает ровно тот ключ (или ту константу), которым пользуется ЕЁ отправитель.

    🔴 Без этого сторожа подмена одного ключа в карте проходила сквозь весь набор:
    карточка «Израсходовано много трафика» показывала бы письмо про низкий баланс, и
    ничто бы не покраснело. Имя ищется ВНУТРИ тела отправителя, а не по всему файлу —
    иначе годится любой ключ, который в этом файле вообще встречается.
    """
    wrong: list[str] = []
    for message_id, (module_name, function_name, name) in _SENDER_OF.items():
        if name not in _names_used_in(_function_node(module_name, function_name)):
            wrong.append(f'{message_id}: {name} не называется в {function_name}')
    assert not wrong, 'карточка читает не тот источник: ' + '; '.join(wrong)


def test_the_map_covers_every_card_and_names_the_right_kind_of_source() -> None:
    """Таблица отправителей полна, и словарные не перепутаны с константными."""
    assert set(_SENDER_OF) == {entry['id'] for entry in AUTO_MESSAGE_CATALOG}
    for message_id, (_, _, name) in _SENDER_OF.items():
        if message_id in _LOCALE_TEXT_KEYS:
            assert _LOCALE_TEXT_KEYS[message_id] == name, f'{message_id}: карта указывает на другой ключ'
        else:
            assert name.endswith('_TEXT'), f'{message_id}: у бесключевого письма источник должен быть константой'


def test_every_template_is_filled_with_exactly_the_names_it_asks_for() -> None:
    """Метки шаблона совпадают с тем, что подставляет отправитель, — имя в имя.

    🔴 Заведён критиком полноты волны 2. Пока письма были f-строками, опечатку в
    имени подстановки ловил линтер: `F821`, неопределённое имя, и ворота проекта
    не пускали такой код. Как только текст переехал в строковый литерал, имя стало
    невидимо для линтера: мутация `{amount}` → `{amount_typo}` прошла `ruff check`
    и все 3591 тестов, а в бою дала бы `KeyError` на живом клиенте. У суточного
    списания это ещё и превращает успешное списание в «ошибку» в журнале.

    То есть перевод в шаблоны снял чужую защиту, и вернуть её обязан этот сторож.
    """
    import string

    from app.services import daily_subscription_service as daily, monitoring_service as monitoring

    modules = {_MONITORING: monitoring, _DAILY: daily}
    checked = 0
    problems: list[str] = []

    for module_name, module in modules.items():
        source = (_BOT_ROOT / module_name).read_text(encoding='utf-8')
        tree = ast.parse(source)
        # Все константы-тексты этого модуля: ИМЯ_ЧЕГО_ТО_TEXT или строка тарифа.
        const_names = {
            name
            for name in dir(module)
            if (name.endswith('_TEXT') or name == 'AUTOPAY_TARIFF_LINE') and isinstance(getattr(module, name), str)
        }
        assert const_names, f'{module_name}: констант-текстов не найдено, сторож стал пустым'

        def formatted_const(call: ast.Call) -> str | None:
            """Имя константы, к которой применён `.format(...)`.

            Форм две, и обе законные: голая константа и обёртка `text_for('ИМЯ', ИМЯ)`,
            через которую с этапа АС-11 проходит правка владельца. Сторож обязан знать обе,
            иначе после обёртки он молча перестал бы проверять что-либо.
            """
            if not (isinstance(call.func, ast.Attribute) and call.func.attr == 'format'):
                return None
            target = call.func.value
            if isinstance(target, ast.Name) and target.id in const_names:
                return target.id
            if (
                isinstance(target, ast.Call)
                and isinstance(target.func, ast.Attribute)
                and target.func.attr == 'text_for'
                and len(target.args) == 2
                and isinstance(target.args[1], ast.Name)
                and target.args[1].id in const_names
            ):
                assert isinstance(target.args[0], ast.Constant) and target.args[0].value == target.args[1].id, (
                    'имя в хранилище правок разошлось с именем константы'
                )
                return target.args[1].id
            return None

        called_with: dict[str, list[set[str]]] = {name: [] for name in const_names}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                const = formatted_const(node)
                if const:
                    assert not node.args, f'{const}: позиционная подстановка, имена не проверить'
                    called_with[const].append({kw.arg for kw in node.keywords if kw.arg})

        for name in sorted(const_names):
            wanted = {field for _, field, _, _ in string.Formatter().parse(getattr(module, name)) if field is not None}
            calls = called_with[name]
            if not calls:
                if wanted:
                    problems.append(f'{name}: в тексте есть метки {sorted(wanted)}, а .format() никто не зовёт')
                checked += 1
                continue
            for given in calls:
                if given != wanted:
                    problems.append(f'{name}: в тексте {sorted(wanted)}, подставляют {sorted(given)}')
                checked += 1

    assert checked >= 9, f'сторож проверил всего {checked} шаблонов — он ослеп'
    assert not problems, 'подстановка разошлась с текстом: ' + '; '.join(problems)


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """Хранилище правок в своей папке: боевой `data/notification_settings.json` не трогаем."""
    from app.services.notification_settings_service import NotificationSettingsService as Settings

    monkeypatch.setattr(Settings, '_storage_path', tmp_path / 'notification_settings.json')
    monkeypatch.setattr(Settings, '_data', {}, raising=False)
    monkeypatch.setattr(Settings, '_loaded', False, raising=False)
    monkeypatch.setattr(Settings, '_readonly', False, raising=False)
    from app.localization.loader import clear_locale_cache

    clear_locale_cache()
    return Settings


@pytest.mark.asyncio
async def test_an_edit_reaches_the_letter_that_is_actually_sent(storage) -> None:
    """Правка владельца доезжает ДО ОТПРАВКИ, а не только показывается на карточке.

    🔴 Это главный сторож этапа АС-11 и единственный, отвечающий на вопрос «доезжает ли».
    Настоящий отправитель вызывается по-настоящему; проверяется письмо-константа, у которой
    свой путь чтения, отличный от словарного.
    """
    from app.services.monitoring_service import MonitoringService

    assert storage.set_text_override('TRIAL_ENDING_TEXT', '🎁 Пробный кончается через {hours_text}.')

    service = MonitoringService.__new__(MonitoringService)
    sent: dict[str, str] = {}

    async def capture(**kwargs):
        sent['text'] = kwargs['text']
        return True

    service._send_message_with_logo = capture
    user = SimpleNamespace(id=907, telegram_id=5207068834, language='ru')
    subscription = SimpleNamespace(id=233, end_date=datetime.now(UTC) + timedelta(hours=7), tariff=None)

    assert await service._send_trial_ending_notification(user, subscription, warn_hours=9) is True
    assert sent['text'].startswith('🎁 Пробный кончается через'), 'правка не доехала до отправки'
    assert '{hours_text}' not in sent['text'], 'метка не подставилась'


def test_an_edit_reaches_a_dictionary_letter_too(storage) -> None:
    """Словарные письма читают правку через тот же `Texts`, что и отправитель."""
    from app.localization.texts import get_texts

    assert storage.set_text_override(
        'SUBSCRIPTION_EXPIRED_1D', 'Свой текст про {end_date}, цену {price} и тариф{tariff_label}.'
    )
    assert (
        get_texts('ru').t('SUBSCRIPTION_EXPIRED_1D') == 'Свой текст про {end_date}, цену {price} и тариф{tariff_label}.'
    )


def test_letters_nobody_edited_are_untouched(storage) -> None:
    """Правка одного письма не задевает остальные — иначе один этап тихо перепишет два десятка."""
    import json

    ru = json.loads((_BOT_ROOT / 'app/localization/locales/ru.json').read_text(encoding='utf-8'))
    storage.set_text_override(
        'SUBSCRIPTION_EXPIRED_1D', 'Свой текст про {end_date}, цену {price} и тариф{tariff_label}.'
    )

    from app.localization.texts import get_texts

    texts = get_texts('ru')
    for message_id, key in _LOCALE_TEXT_KEYS.items():
        if key == 'SUBSCRIPTION_EXPIRED_1D':
            continue
        assert texts.t(key) == ru[key], f'{message_id}: письмо изменилось, хотя его не правили'


def test_an_edit_does_not_leak_into_english(storage) -> None:
    """Решение владельца: правится РУССКИЙ. Английским клиентам уходит `en.json`, как и было."""
    from app.localization.texts import get_texts

    before = get_texts('en').t('SUBSCRIPTION_EXPIRED_1D')
    storage.set_text_override('SUBSCRIPTION_EXPIRED_1D', 'Свой русский текст про {end_date}, {price} и{tariff_label}.')
    assert get_texts('en').t('SUBSCRIPTION_EXPIRED_1D') == before


def test_only_letters_of_this_section_can_be_overridden(storage) -> None:
    """Крючок стоит внутри `Texts` — без белого списка он подменял бы ЛЮБОЙ ключ локали."""
    assert storage.set_text_override('BALANCE_TOPUP', 'чужой ключ') is False
    assert storage.get_text_override('BALANCE_TOPUP') is None

    from app.localization.texts import get_texts

    assert get_texts('ru').t('BALANCE_TOPUP') != 'чужой ключ'


def test_an_unreadable_settings_file_is_not_overwritten(storage, tmp_path) -> None:
    """🔴 Испорченный файл раньше ЗАТИРАЛСЯ дефолтами: с правкой текстов это стёрло бы всё,
    что владелец написал. Теперь такой файл остаётся нетронутым."""
    broken = tmp_path / 'notification_settings.json'
    broken.write_text('{это не json', encoding='utf-8')

    assert storage.is_enabled('expired_1d') is True, 'служба обязана работать на умолчаниях'
    assert broken.read_text(encoding='utf-8') == '{это не json', 'испорченный файл переписан'
    assert storage.set_text_override('SUBSCRIPTION_EXPIRED_1D', 'x') is False, 'запись в нечитаемый файл'


def test_the_twins_share_one_edit(storage) -> None:
    """У пары «за 3 дня / завтра» текст ОДИН — правка обязана быть общей, как и говорит карточка."""
    storage.set_text_override(
        'SUBSCRIPTION_EXPIRING_PAID',
        'Общий текст{tariff_label} про {days_text} до {end_date}. {autopay_status} {action_text}',
    )
    assert 'Общий текст' in (_text_facts('paid-3d')['text'] or '')
    assert 'Общий текст' in (_text_facts('paid-1d')['text'] or '')
    assert _text_facts('paid-3d')['text_source'] == 'custom'


def test_the_logo_list_matches_the_senders() -> None:
    """Про логотип обещаем ровно тем письмам, которым отправитель его прикладывает.

    Обещать «уйдёт без логотипа» письму, которое логотипа не знает, — обещать потерю того,
    чего не бывает. Список сверяется с телами отправителей, а не пишется на глаз.
    """
    from app.cabinet.routes.admin_auto_messages import _WITH_LOGO_IDS

    real: set[str] = set()
    for message_id, (module_name, function_name, _) in _SENDER_OF.items():
        body = ast.get_source_segment(
            (_BOT_ROOT / module_name).read_text(encoding='utf-8'),
            _function_node(module_name, function_name),
        )
        if body and '_send_message_with_logo' in body:
            real.add(message_id)
    assert real == _WITH_LOGO_IDS, f'лишние: {sorted(_WITH_LOGO_IDS - real)}; забытые: {sorted(real - _WITH_LOGO_IDS)}'


def test_only_letters_with_an_english_key_promise_english() -> None:
    """У восьми писем английской версии нет вовсе — им нельзя обещать, что английский не тронут."""
    import json

    en = json.loads((_BOT_ROOT / 'app/localization/locales/en.json').read_text(encoding='utf-8'))
    for entry in AUTO_MESSAGE_CATALOG:
        key = _LOCALE_TEXT_KEYS.get(entry['id'])
        expected = bool(key and en.get(key))
        assert _text_facts(entry['id'])['text_has_english'] is expected, entry['id']


def test_a_corrupted_settings_file_is_never_overwritten(storage, tmp_path) -> None:
    """Файл, который не удалось прочитать, остаётся нетронутым — и валидный не-объект тоже."""
    broken = tmp_path / 'notification_settings.json'
    for content in ('{это не json', '[]', 'null', '"строка"'):
        broken.write_text(content, encoding='utf-8')
        storage._loaded = False
        storage._readonly = False
        assert storage.is_enabled('expired_1d') is True, f'служба упала на {content!r}'
        assert broken.read_text(encoding='utf-8') == content, f'файл переписан на {content!r}'


def test_a_readable_file_lifts_the_refusal_to_write(storage, tmp_path) -> None:
    """Один сбой чтения не должен запирать сохранения до перезапуска бота."""
    path = tmp_path / 'notification_settings.json'
    path.write_text('{сломано', encoding='utf-8')
    storage._loaded = False
    storage.is_enabled('expired_1d')
    assert storage._readonly is True

    path.write_text('{}', encoding='utf-8')
    storage._loaded = False
    storage.is_enabled('expired_1d')
    assert storage._readonly is False, 'отказ на запись не снялся после починки файла'
    assert storage.set_text_override('SUBSCRIPTION_EXPIRED_1D', 'Про {end_date}, {price} и{tariff_label}.')


def test_a_failed_write_does_not_reach_clients(storage, tmp_path) -> None:
    """🔴 Память — это и есть то, что уходит клиентам: бот и кабинет один процесс.

    Записать правку в память ДО успешной записи на диск значит отправить клиентам текст,
    про который владельцу тут же ответят «не сохранено». Нашли критик полноты и скептик
    независимо, оба живым прогоном. Соседняя, числовая половина обработчика эту мину уже
    чинила — текстовая её повторила.
    """
    (tmp_path / 'notification_settings.json').write_text('{сломано', encoding='utf-8')
    storage._loaded = False
    storage.is_enabled('expired_1d')  # выставит _readonly

    assert storage.set_text_override('SUBSCRIPTION_EXPIRED_1D', 'Текст про {end_date}, {price}{tariff_label}.') is False
    assert storage.get_text_override('SUBSCRIPTION_EXPIRED_1D') is None, 'несохранённый текст ушёл бы клиентам'


def test_a_conversion_cannot_sneak_into_a_letter(storage) -> None:
    """`{balance!r}` не меняет набора имён, а клиенту дописывает кавычки вокруг суммы."""
    from fastapi import HTTPException

    source = _source_text_of('low-balance')
    assert source and '{balance}' in source
    with pytest.raises(HTTPException):
        _validate_new_text(source, source.replace('{balance}', '{balance!r}'))


def test_the_tariff_marker_is_tolerated_only_where_the_code_has_it(storage) -> None:
    """Метку тарифа терпим в обе стороны — но только у писем, где она есть в исходнике.

    Первая редакция вычёркивала её у ВСЕХ, и `{tariff_label}` пролезал в любое письмо:
    отправитель такого не подставляет, и письмо не уходит вовсе. Нашёл скептик.
    """
    from fastapi import HTTPException

    from app.cabinet.routes.admin_auto_messages import (
        _OPTIONAL_MARKER,
        _marker_set,
        _raw_source_text_of,
    )

    def optional(message_id: str) -> bool:
        return _OPTIONAL_MARKER in _marker_set(_raw_source_text_of(message_id) or '')

    own = _source_text_of('paid-expired')
    assert _validate_new_text(own, own + ' {tariff_label}', tariff_optional=optional('paid-expired'))

    alien = _source_text_of('trial-discount')
    with pytest.raises(HTTPException):
        _validate_new_text(alien, alien + ' {tariff_label}', tariff_optional=optional('trial-discount'))


def test_a_dropped_edit_is_not_called_edited(storage) -> None:
    """Правка перестала применяться — карточка обязана показать кодовый текст И снять значок.

    Иначе владелец видит свои слова исчезнувшими, а метка «изменён» висит, и понять
    случившееся неоткуда. Нашёл скептик.
    """
    storage.set_text_override('SUBSCRIPTION_EXPIRED_1D', 'Своё про {end_date}, {price}{tariff_label}.')
    assert _text_facts('return-day1')['text_source'] == 'custom'

    # Метки разошлись с кодом — правка отбрасывается на чтении.
    storage._data['message_texts']['SUBSCRIPTION_EXPIRED_1D'] = 'Своё про {no_such_marker}.'
    facts = _text_facts('return-day1')
    assert facts['text'] == _source_text_of('return-day1'), 'показан не кодовый текст'
    assert facts['text_source'] == 'code', 'значок «изменён» врёт'


def test_the_storage_key_of_every_card_is_the_name_its_sender_uses() -> None:
    """Имя, под которым лежит правка, — то же, что читает отправитель.

    🔴 Заведён волной 1: подмена одного имени в `_CONST_SOURCE_NAMES` переживала ВЕСЬ набор
    из 94 тестов. В бою это значит: владелец правит «Пробный кончается через 2 часа»,
    карточка показывает правку, а уходит старое письмо — и наоборот, молча меняется
    соседнее. Таблица `_SENDER_OF` написана в этом файле независимо и служит эталоном.
    """
    from app.cabinet.routes.admin_auto_messages import _CONST_SOURCE_NAMES, _source_name_of

    for message_id, (_, _, expected) in _SENDER_OF.items():
        assert _source_name_of(message_id) == expected, f'{message_id}: правка ляжет под чужим именем'

    assert set(_CONST_SOURCE_NAMES) == set(_const_texts()), 'карта констант разошлась с самими текстами'


def test_the_white_list_covers_every_card_and_nothing_else() -> None:
    """Белый список правимых имён = ровно источники 22 карточек.

    Лишнее имя — разрешение подменить чужой ключ локали; недостающее — карточка, у которой
    правка молча не сохранится.
    """
    from app.services.notification_settings_service import NotificationSettingsService as Settings

    sources = {_SENDER_OF[entry['id']][2] for entry in AUTO_MESSAGE_CATALOG}
    assert sources == Settings.EDITABLE_TEXT_NAMES, (
        f'лишние: {sorted(Settings.EDITABLE_TEXT_NAMES - sources)}; '
        f'недостающие: {sorted(sources - Settings.EDITABLE_TEXT_NAMES)}'
    )


def test_every_marker_of_every_letter_is_explained() -> None:
    """Значок без объяснения — это то, что мешает владельцу трогать текст.

    Владелец сказал прямо: «метка может сбивать менеджера». Сторож требует, чтобы каждая
    метка каждого из 22 писем была расшифрована — либо словами, либо списком вариантов.
    """
    import re as regex

    from app.cabinet.routes.admin_auto_messages import _MARKER_HINTS, _MARKER_HINTS_BY_MESSAGE

    unexplained: list[str] = []
    for entry in AUTO_MESSAGE_CATALOG:
        facts = _text_facts(entry['id'])
        for name in regex.findall(r'\{([^{}]+)\}', facts['text'] or ''):
            # 🔴 Спрашиваем ТАБЛИЦУ, а не собственный вывод функции. Прежняя редакция
            # проверяла `text_markers`, куда запись клалась на любое имя с заглушкой, —
            # снос всей таблицы расшифровок оставлял сторож зелёным. Нашла волна 1.
            explained = name in _INSERT_KEYS or (entry['id'], name) in _MARKER_HINTS_BY_MESSAGE or name in _MARKER_HINTS
            if not explained:
                unexplained.append(f'{entry["id"]}: {{{name}}}')
    assert not unexplained, 'метка без расшифровки: ' + ', '.join(sorted(set(unexplained)))
    assert _MARKER_HINTS, 'таблица расшифровок пуста — сторож стал бы бессмысленным'


@pytest.mark.parametrize(
    ('broken', 'why'),
    [
        (lambda src: src.replace('{percent}', ''), 'стёртая метка'),
        (lambda src: src + ' {выдумка}', 'незнакомая метка'),
        (lambda src: src.replace('{percent}', '{percent:.0f}'), 'подменённый спецификатор'),
        (lambda src: '   ', 'пустой текст'),
    ],
)
def test_a_broken_edit_is_refused_with_a_reason(storage, broken, why) -> None:
    """Метку легко задеть, правя соседнее слово. Стёртая даёт письмо с дырой, лишняя роняет
    отправку целиком — `.format()` бросает `KeyError`, и письмо не уходит вовсе и молча."""
    from fastapi import HTTPException

    source = _source_text_of('trial-discount')
    assert source and '{percent}' in source
    with pytest.raises(HTTPException) as failure:
        _validate_new_text(source, broken(source))
    assert failure.value.status_code == 422, why
    assert failure.value.detail, f'{why}: отказ без объяснения'


def test_a_good_edit_is_accepted_and_broken_markup_is_repaired(storage) -> None:
    """Незакрытый тег Телеграм не принял бы вовсе — сохраняем починенное, а не сырое."""
    source = _source_text_of('paid-expired')
    prepared, warning = _validate_new_text(source, '<b>Незакрытый ' + (source or ''))
    assert prepared.count('<b>') == prepared.count('</b>'), 'тег не починен'
    assert warning is None


def test_a_long_edit_is_saved_but_warns_about_the_logo(storage) -> None:
    """Выше 1024 письмо уходит без логотипа — это предупреждение, а не запрет."""
    source = _source_text_of('trial-not-connected')
    _, warning = _validate_new_text(source, (source or '') + 'я' * 1200)
    assert warning and 'логотип' in warning


def test_restoring_gives_back_the_text_from_the_code(storage) -> None:
    from app.localization.texts import get_texts

    original = get_texts('ru').t('SUBSCRIPTION_EXPIRED_1D')
    storage.set_text_override(
        'SUBSCRIPTION_EXPIRED_1D', 'Свой текст про {end_date}, цену {price} и тариф{tariff_label}.'
    )
    assert get_texts('ru').t('SUBSCRIPTION_EXPIRED_1D') != original
    assert storage.clear_text_override('SUBSCRIPTION_EXPIRED_1D')
    assert get_texts('ru').t('SUBSCRIPTION_EXPIRED_1D') == original


def test_a_shown_const_is_the_senders_own_object_not_a_copy_of_it() -> None:
    """Показанный текст — ТОТ ЖЕ объект, что у отправителя, а не равная ему строка.

    🔴 Прямой запрет владельца: копии текста быть не должно. Проверка на равенство
    его не держит — выдуманная копия, случайно совпавшая с оригиналом, равна ему.
    Держит только тождество объекта: подмени константу литералом — покраснеет.

    🔴 С этапа АС-11 утверждение стало ДВУСТОРОННИМ: `_const_texts()` остаётся чистым
    источником из кода, а правку владельца поверх него накладывает `_text_facts`. Здесь
    проверяется первая сторона; вторую держит `test_the_twins_share_one_edit`.
    """
    from app.services import daily_subscription_service as daily, monitoring_service as monitoring

    modules = {_MONITORING: monitoring, _DAILY: daily}
    for message_id, shown in _const_texts().items():
        module_name, _, const_name = _SENDER_OF[message_id]
        original = getattr(modules[module_name], const_name)
        assert shown is original, f'{message_id}: карточке подсунута копия текста, а не сам источник'


@pytest.fixture
def multi_tariff(monkeypatch):
    """Переключатель многотарифного режима. Подмена МЕТОДА pydantic-настроек — только
    на классе: на экземпляре она молча не применяется (урок ритуала от 19.08)."""
    from app.config import settings

    def _set(value: bool) -> None:
        monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: value)

    return _set


def test_a_dictionary_letter_shows_the_dictionary_value_not_the_inline_fallback(multi_tariff) -> None:
    """У вызовов вида ``texts.t(КЛЮЧ, 'запасной текст')`` формулировки РАЗНЫЕ.

    Клиенту уходит значение из ``ru.json``, а встроенный запасной мёртв. Показать
    запасной — значит показать текст, который никому не уходит.

    🔴 С этапа АС-11 это утверждение верно ПОКА ПРАВКИ НЕТ; вторую сторону («есть правка —
    показываем её») держит `test_an_edit_reaches_a_dictionary_letter_too`. Хранилище здесь
    намеренно не подменяется: набор проверяет именно чистое состояние.
    """
    import json

    multi_tariff(True)
    ru = json.loads((_BOT_ROOT / 'app/localization/locales/ru.json').read_text(encoding='utf-8'))
    for message_id, key in _LOCALE_TEXT_KEYS.items():
        assert _text_facts(message_id)['text'] == ru[key], f'{message_id}: показан не {key} из ru.json'


def test_the_tariff_never_appears_on_a_card_while_the_mode_that_fills_it_is_off(multi_tariff) -> None:
    """Метка тарифа и строка тарифа следуют за многотарифным режимом, как у отправителя.

    🔴 У отправителя ОДИННАДЦАТЬ мест, и каждое стоит за ``is_multi_tariff_enabled()``.
    Пока режим выключен, метка разворачивается в пустоту у каждого клиента. Показывать
    её владельцу — значит показывать кусок письма, которого никто не получит: ровно то,
    ради чего этап и делается. Первая редакция АС-10 показывала, три линзы нашли.
    """
    multi_tariff(False)
    for entry in AUTO_MESSAGE_CATALOG:
        facts = _text_facts(entry['id'])
        assert '{tariff_label}' not in (facts['text'] or ''), f'{entry["id"]}: метка тарифа при выключенном режиме'
        assert not any('Тариф' in suffix for suffix in facts['text_suffixes']), (
            f'{entry["id"]}: строка тарифа при выключенном режиме'
        )

    # А когда режим включён — обе на месте: сторож обязан отличать одно от другого,
    # иначе он проходит и на коде, который просто выбросил тариф навсегда.
    multi_tariff(True)
    assert '{tariff_label}' in _text_facts('trial-2h')['text']
    assert any('Тариф' in suffix for suffix in _text_facts('autopay-ok')['text_suffixes'])


def test_the_cabinet_link_appendix_is_shown_where_the_sender_adds_it(monkeypatch) -> None:
    """Приписка со ссылкой — целый абзац письма. Не показать её — показать не то письмо."""
    from app.services import monitoring_service as sender

    # Подмена МЕТОДА pydantic-настроек работает только на классе: на экземпляре
    # она молча не применяется (урок ритуала от 19.08).
    monkeypatch.setattr(type(sender.settings), 'get_cabinet_link', lambda self: 'https://cabinet.example.test')
    for message_id in _CABINET_LINK_IDS:
        suffixes = _text_facts(message_id)['text_suffixes']
        assert any('cabinet.example.test' in suffix for suffix in suffixes), f'{message_id}: приписка не показана'

    # А там, где отправитель её НЕ дописывает, она не должна появляться.
    assert not any(
        'cabinet.example.test' in suffix for suffix in _text_facts('trial-not-connected')['text_suffixes']
    ), 'приписка показана письму, к которому отправитель её не добавляет'


def test_the_twins_name_each_other() -> None:
    """Пара «за 3 дня / завтра» шлёт ОДИН текст. Молчание об этом — будущее «поменял одно, изменилось два»."""
    assert _text_facts('paid-3d')['shares_text_with'] == 'Подписка истекает завтра'
    assert _text_facts('paid-1d')['shares_text_with'] == 'Подписка истекает через 3 дня'
    assert _text_facts('trial-expired')['shares_text_with'] is None, 'у одиночного письма выдуман близнец'


def test_a_variant_the_settings_cannot_produce_is_not_shown(monkeypatch) -> None:
    """Из двух взаимоисключающих фраз показана та, которую даёт текущая настройка.

    🔴 Прогон сценария 05.09.2026: владелец читает список вариантов сверху вниз и
    достраивает письмо, которого не бывает. Пару «включите автоплатёж» / «продлите
    вручную» отправитель выбирает не по клиенту, а по настройке бота — значит одна
    из них не уходит НИКОМУ, и показывать её нельзя по той же причине, по которой
    убрана строка тарифа.
    """
    from app.config import settings

    def shown() -> set[str]:
        for insert in _text_facts('paid-3d')['text_inserts']:
            if insert.name == 'action_text':
                return {variant.text for variant in insert.variants}
        raise AssertionError('метка action_text пропала с карточки')

    # Поле pydantic живёт на ЭКЗЕМПЛЯРЕ: классовая подмена молча не применилась бы
    # (урок ритуала от 19.08 — там же сказано, что для МЕТОДОВ наоборот).
    monkeypatch.setattr(settings, 'ENABLE_AUTOPAY', False)
    without = shown()
    monkeypatch.setattr(settings, 'ENABLE_AUTOPAY', True)
    with_autopay = shown()

    assert without != with_autopay, 'настройка не влияет на список — забор пустой'
    assert not (without & {phrase for phrase in with_autopay if 'втоплат' in phrase and 'ключите' in phrase})
    for variants in (without, with_autopay):
        assert len(variants) == 2, f'вариантов должно остаться два, а не {len(variants)}'


def test_every_variant_says_when_it_happens() -> None:
    """У каждой фразы написано, при каком условии она встаёт. Список без условий —
    это приглашение собрать в голове письмо, которого не бывает."""
    facts = _text_facts('paid-3d')
    assert facts['text_inserts'], 'вставки пропали'
    for insert in facts['text_inserts']:
        for variant in insert.variants:
            assert variant.when.strip(), f'{insert.name}: у варианта нет условия'
            assert variant.text.strip(), f'{insert.name}: у варианта нет текста'


def test_inserts_are_listed_exactly_where_the_placeholder_stands() -> None:
    """Метка, вместо которой встаёт другой ТЕКСТ, обязана быть расшифрована.

    Без этого карточка показывает предложение с невидимыми дырами: у писем об
    истечении две метки из трёх — это целые фразы из соседних ключей.
    """
    named = {insert.name for insert in _text_facts('paid-3d')['text_inserts']}
    assert named == {'autopay_status', 'action_text'}
    for insert in _text_facts('paid-3d')['text_inserts']:
        assert insert.variants, f'{insert.name}: варианты не подтянулись'

    assert {insert.name for insert in _text_facts('channel-left')['text_inserts']} == {'check_button'}
    assert _text_facts('daily-charge')['text_inserts'] == [], 'расшифрованы метки, которых в тексте нет'
