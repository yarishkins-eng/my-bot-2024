"""РС-14д: сторож на подсказку о пределах медиа, которую читает человек в кабинете.

Экран рассылок пишет словами: «Картинка, видео или документ — до 10 МБ» и «с вложением текст
ограничен 1024 знаками». Это текст-инструкция, а такие протухают молча: числа живут в коде бота,
а фраза — в четырёх локалях кабинета, в ДРУГОМ репозитории.

🔴 Первая версия этого сторожа проверяла `settings.MEDIA_MAX_VIDEO_SIZE_MB` (50) и была зелёной,
пока фраза на экране УЖЕ врала: рассылочное вложение грузится через `/cabinet/media/upload`, а там
свой предел `MAX_FILE_SIZE = 10 МБ` на все типы, и настройки из конфига этот маршрут не читает вовсе.
Урок: сторож обязан смотреть на ту величину, которой подчиняется ЭТОТ экран, а не на похожую соседнюю.

⛔ Если тест упал — не «поправить число здесь». Сначала решить, менялся ли предел намеренно, и если
да — обновить `admin.broadcasts.mediaHint` во ВСЕХ четырёх локалях кабинета
(`cabinet-code/src/locales/{ru,en,fa,zh}.json`), а уже потом это ожидание.
"""

from app.cabinet.routes.media import MAX_FILE_SIZE
from app.services.broadcast_service import caption_exceeds_telegram_limit


# Числа, которые СЕГОДНЯ написаны человеку на экране создания рассылки.
HINT_FILE_MB = 10
HINT_CAPTION_CHARS = 1024


def test_media_hint_still_tells_the_truth_about_size() -> None:
    assert MAX_FILE_SIZE == HINT_FILE_MB * 1024 * 1024, (
        'подсказка кабинета обещает вложение до 10 МБ — предел загрузки изменился, фраза стала ложью'
    )


def test_size_limit_is_the_same_for_every_attachment_type() -> None:
    """Подсказка называет ОДНО число на картинку, видео и документ — так и должно быть в коде.

    Если у типов появятся разные пределы, фраза «до 10 МБ» станет полуправдой.
    """
    import inspect

    from app.cabinet.routes import media

    source = inspect.getsource(media.upload_media) if hasattr(media, 'upload_media') else inspect.getsource(media)
    assert source.count('MAX_FILE_SIZE') >= 1, 'проверка размера исчезла из маршрута загрузки'
    assert 'MEDIA_MAX_VIDEO_SIZE_MB' not in source, (
        'в маршрут вернулся отдельный предел для видео — подсказка обещает одно число на все типы'
    )


def test_caption_limit_in_hint_matches_the_splitter() -> None:
    """Порог подписи проверяем ПОВЕДЕНИЕМ: 1024 влезает, 1025 уже нет.

    Сторож на подстроку «1024» в исходнике ничего не стережёт — число встречается и в комментариях.
    """
    assert caption_exceeds_telegram_limit('я' * HINT_CAPTION_CHARS) is False, (
        'подсказка обещает, что 1024 знака ещё влезают в подпись'
    )
    assert caption_exceeds_telegram_limit('я' * (HINT_CAPTION_CHARS + 1)) is True, (
        'подсказка обещает деление на два сообщения после 1024 знаков'
    )
