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
    _LOCALE_TEXT_KEYS,
    AUTO_MESSAGE_CATALOG,
    _const_texts,
    _text_facts,
)


_BOT_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SENDING_MODULES = (
    'app/services/monitoring_service.py',
    'app/services/daily_subscription_service.py',
)

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


def test_a_dictionary_letter_shows_the_dictionary_value_not_the_inline_fallback() -> None:
    """У вызовов вида ``texts.t(КЛЮЧ, 'запасной текст')`` формулировки РАЗНЫЕ.

    Клиенту уходит значение из ``ru.json``, а встроенный запасной мёртв. Показать
    запасной — значит показать текст, который никому не уходит.
    """
    import json

    ru = json.loads((_BOT_ROOT / 'app/localization/locales/ru.json').read_text(encoding='utf-8'))
    for message_id, key in _LOCALE_TEXT_KEYS.items():
        assert _text_facts(message_id)['text'] == ru[key], f'{message_id}: показан не {key} из ru.json'


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
