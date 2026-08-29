"""РС-2: длинный текст с картинкой уходит вторым сообщением, а не роняет кампанию.

Телеграм принимает подпись к медиа не длиннее 1024 символов. Экран рассылок
разрешает 4000 и про медиа не предупреждает, а доставка отдавала весь текст
подписью. `TelegramBadRequest` не транзиентный — повтора нет, и кампания
заканчивалась «0 доставлено» у КАЖДОГО получателя, при пустом логе.

Чат-админка это давно делает правильно (`handlers/admin/messages.py:1279-1298`):
медиа без подписи, текст следом. Здесь тот же приём перенесён в общую доставку,
поэтому чинится сразу и кабинетная рассылка, и старый Web API.

🔴 Почему деление, а не отказ на входе. Первый вариант правки отбивал такую
кампанию с кодом 400. Две независимые волны ревью показали, что владелец этого
отказа НЕ УВИДИТ: у мутации отправки в кабинете нет `onError`, ошибка нигде не
рисуется (`AdminBroadcastCreate.tsx:157-163`). То есть отказ поменял бы «молча
падает у всех» на «молча не нажимается» — хуже прежнего. Деление доставляет.

Сторожа здесь про разное:
1. длинный текст → медиа БЕЗ подписи + второе сообщение с текстом и кнопками;
2. короткий текст → одно сообщение с подписью, деления нет;
3. длину меряет штатный помощник проекта (после разбора разметки), а не `len()`:
   текст с тегами и ссылкой, влезающий по разбору, делиться не должен;
4. `_finished_status` не выдаёт «частично» за кампанию, не дошедшую ни до кого.

⛔ Границы. Пункт 4 намеренно НЕ считает провалом заблокировавших бота: кампания
отработала, аудитория недостижима — это разные вещи. Кампания с нулём получателей
по-прежнему «завершена» (`_finished_status(0, 0, 0)`), и это осознанно: слать было
некому, а не «не дошло».

🔴 ЗАПИСАННЫЙ ПРОБЕЛ: `logger.warning` в ветке отказа Телеграма сторожем не закрыт —
ветка живёт внутри замыкания в теле рассылки, вытащить её тестом дешевле, чем
переписать функцию, не вышло. Мутация «убрать логирование» ни один тест не красит.
Записано намеренно, чтобы следующий знал: эта строка без присмотра.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.broadcast_service import (
    BroadcastConfig,
    BroadcastMediaConfig,
    BroadcastService,
    _finished_status,
)
from app.utils.message_patch import caption_exceeds_telegram_limit


# Длины намеренно не круглые и не совпадают ни с одним умолчанием рядом: совпадение
# превратило бы сторож в проверку равенства, а не в проверку защиты.
_LONG = 'я' * 1337
_SHORT = 'коротко и по делу'
_TELEGRAM_ID = 918273645


def _service_with_bot() -> tuple[BroadcastService, AsyncMock]:
    service = BroadcastService()
    bot = AsyncMock()
    service.set_bot(bot)
    return service, bot


def _config(text: str, media_type: str = 'photo') -> BroadcastConfig:
    return BroadcastConfig(
        target='all',
        message_text=text,
        selected_buttons=[],
        media=BroadcastMediaConfig(type=media_type, file_id='AgACAgIAAx0-РС2'),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(('media_type', 'method_name'), [('video', 'send_video'), ('document', 'send_document')])
async def test_split_works_for_every_media_type(media_type: str, method_name: str) -> None:
    """Деление работает не только для фото.

    🔴 Дыру нашёл мутационный скептик: остальные сторожа берут только `photo`, и сузить
    ветку до одного типа можно было бы незаметно. Видео и документ ходят через тот же
    словарь методов, значит и делиться обязаны так же.
    """
    service, bot = _service_with_bot()

    await service._deliver_message(_TELEGRAM_ID, _config(_LONG, media_type), None)

    send_method = getattr(bot, method_name)
    send_method.assert_awaited_once()
    assert 'caption' not in send_method.await_args.kwargs
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs['text'] == _LONG


@pytest.mark.asyncio
async def test_long_caption_is_split_into_media_and_text() -> None:
    """Пункт 1: длинный текст уходит вторым сообщением, кнопки — на нём."""
    service, bot = _service_with_bot()
    keyboard = SimpleNamespace(inline_keyboard=[])

    await service._deliver_message(_TELEGRAM_ID, _config(_LONG), keyboard)

    # Медиа ушло БЕЗ подписи — иначе Телеграм отбил бы всё сообщение целиком.
    bot.send_photo.assert_awaited_once()
    assert 'caption' not in bot.send_photo.await_args.kwargs

    # Текст ушёл отдельно и унёс с собой клавиатуру.
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs['text'] == _LONG
    assert bot.send_message.await_args.kwargs['reply_markup'] is keyboard


@pytest.mark.asyncio
async def test_short_caption_stays_one_message() -> None:
    """Пункт 2: короткий текст делить не надо — деление не должно срабатывать лишний раз."""
    service, bot = _service_with_bot()

    await service._deliver_message(_TELEGRAM_ID, _config(_SHORT), None)

    bot.send_photo.assert_awaited_once()
    assert bot.send_photo.await_args.kwargs['caption'] == _SHORT
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_length_is_measured_after_markup_is_parsed() -> None:
    """Пункт 3: считаем как Телеграм — после разбора разметки, а не сырые символы.

    Видимого текста тут заведомо меньше лимита, но сырая строка его превышает за счёт
    тегов и длинной ссылки. Голый `len()` разделил бы законное сообщение зря.
    """
    visible = 'ц' * 900
    link = '<a href="https://cabinet.lilulalu.xyz/subscription/purchase?from=broadcast">ссылка</a>'
    text = f'<b>{visible}</b>{link}{link}'
    # Оба предохранителя обязательны: без них сторож прошёл бы «по совпадению» —
    # то есть не отличал бы штатный помощник от голого len().
    assert len(text) > 1024, 'фикстура обязана превышать лимит ПО СЫРОЙ длине, иначе сторож пуст'
    assert not caption_exceeds_telegram_limit(text), 'по разобранной длине она обязана влезать'

    service, bot = _service_with_bot()
    await service._deliver_message(_TELEGRAM_ID, _config(text), None)

    # Деления не было: влезает по разобранной длине.
    bot.send_photo.assert_awaited_once()
    assert bot.send_photo.await_args.kwargs['caption'] == text
    bot.send_message.assert_not_awaited()


def test_finished_status_marks_total_failure_as_failed() -> None:
    """Пункт 4: ноль доставленных при неудачах — провал, а не «частично»."""
    # Именно этот вход отличает новое поведение от старого: раньше было 'partial'.
    assert _finished_status(sent_count=0, failed_count=17, blocked_count=0) == 'failed'


def test_finished_status_keeps_partial_and_completed() -> None:
    """Пункт 4, границы: частичный успех и чистый успех не переехали в 'failed'."""
    assert _finished_status(sent_count=9, failed_count=17, blocked_count=0) == 'partial'
    assert _finished_status(sent_count=23, failed_count=0, blocked_count=0) == 'completed'


def test_finished_status_blocked_only_is_not_failure() -> None:
    """Заблокировавшие бота — недостижимая аудитория, а не провал кампании."""
    assert _finished_status(sent_count=0, failed_count=0, blocked_count=6) == 'completed'
    assert _finished_status(sent_count=12, failed_count=0, blocked_count=6) == 'completed'


def test_finished_status_default_blocked_count_is_zero() -> None:
    """Умолчание третьего счётчика — ноль, и на него опирается почтовая ветка.

    🔴 Дыру нашёл мутационный скептик: все остальные сторожа передают `blocked_count`
    явно, а единственный живой вызов с умолчанием — почтовая рассылка
    (`broadcast_service._mark_email_finished`). Сдвинь умолчание на единицу — и успешная
    почтовая кампания молча станет «Частично», ни один тест бы этого не заметил.
    """
    assert _finished_status(sent_count=31, failed_count=0) == 'completed'


@pytest.mark.asyncio
async def test_retry_does_not_send_media_twice() -> None:
    """Мина GA: повтор после сбоя не шлёт картинку заново.

    🔴 Дефект завела сама правка РС-2 и нашла ревизия плана. Деление на два сообщения
    стоит ВНУТРИ цикла повторов `send_single`: упал второй вызов на FloodWait или сети —
    повтор начинает доставку заново, и человек получает картинку второй и третий раз.
    До деления вызов был один, и повтор был безвреден.

    Здесь воспроизводим ровно это: первый заход роняет отправку ТЕКСТА, второй проходит.
    Картинка обязана уйти один раз.
    """
    service, bot = _service_with_bot()
    state: dict[str, bool] = {}
    bot.send_message.side_effect = [RuntimeError('сеть моргнула'), None]

    with pytest.raises(RuntimeError):
        await service._deliver_message(_TELEGRAM_ID, _config(_LONG), None, state)

    # Улика, что момент действительно наступил: медиа ушло и это записано.
    assert state == {'media_sent': True}, 'без отметки повтор пошлёт картинку заново'

    # Повтор — тот же вызов с тем же состоянием, как это делает send_single.
    await service._deliver_message(_TELEGRAM_ID, _config(_LONG), None, state)

    assert bot.send_photo.await_count == 1, 'картинка ушла дважды — мина GA вернулась'
    assert bot.send_message.await_count == 2
