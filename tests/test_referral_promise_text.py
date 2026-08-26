"""РФ-1 п.1.8: обещание программы обязано совпадать с тем, что она делает.

До этапа бот обещал процент «с каждого пополнения», а комиссия платилась только с пополнений
кошелька — то есть текст был правдой ровно потому, что механизм был сломан. После п.1.2б
комиссия платится с любой оплаты, и слово «пополнение» стало ложью в меньшую сторону:
партнёр получал бы больше обещанного и не понимал, за что.
"""

import json
import pathlib


LOCALES = pathlib.Path(__file__).resolve().parents[1] / 'app' / 'localization' / 'locales'

# Ключи, которые ВИДИТ клиент на экране реферальной программы.
PROMISE_KEYS = (
    'REFERRAL_REWARDS_HEADER',
    'REFERRAL_REWARD_NEW_USER',
    'REFERRAL_REWARD_COMMISSION',
    'REFERRAL_REWARD_COMMISSION_LIMITED',
    'REFERRAL_SHORT_STATS',
    'REFERRAL_LIST_LEGEND',
    'REFERRAL_LIST_ITEM_TOPUPS',
    'REFERRAL_LIST_EMPTY',
    # 🔴 Найдено прогоном сценария: это текст, который партнёр ОТПРАВЛЯЕТ другу.
    # Экран уже говорил «при оплате», а уходящее сообщение — «при пополнении баланса».
    'REFERRAL_INVITE_BONUS',
)

# Слова, которыми обещание сужается до пополнения кошелька.
FORBIDDEN = {'ru': ('пополн',), 'en': ('top up', 'top-up', 'topped up')}


def _promise(locale: str) -> dict[str, str]:
    data = json.loads((LOCALES / f'{locale}.json').read_text(encoding='utf-8'))
    return {key: data[key] for key in PROMISE_KEYS if key in data}


def test_the_promise_never_narrows_back_to_a_wallet_top_up():
    for locale, words in FORBIDDEN.items():
        for key, text in _promise(locale).items():
            lowered = text.lower()
            for word in words:
                assert word not in lowered, (
                    f'{locale}/{key} снова обещает только пополнение кошелька: {text!r}. '
                    f'После РФ-1 комиссия платится с ЛЮБОЙ оплаты.'
                )


def test_the_promise_keys_still_exist_in_both_live_locales():
    """Улика против пустого сторожа: проверка выше пройдёт и на пустом словаре."""
    for locale in ('ru', 'en'):
        assert len(_promise(locale)) == len(PROMISE_KEYS), (
            f'в {locale}.json пропал один из ключей обещания — сторож выше стал бы бесполезен'
        )
