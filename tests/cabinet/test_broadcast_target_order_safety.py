"""РС-14е: «Все» не стоит соседней строкой с «Только мне».

Экран рассылок рисует кнопки в порядке `FILTER_LABELS` и группирует по `FILTER_GROUPS`.
Пока «Все» была второй строкой сразу под «Тест: только мне», промах пальцем на одну строку
означал отправку всей базе. Подтверждение с числом получателей от этого спасает, но оно
было ЕДИНСТВЕННЫМ рубежом — а рассылками теперь занимается не только владелец.
"""

from app.cabinet.routes.admin_broadcasts import (
    EMAIL_FILTER_GROUPS,
    EMAIL_FILTER_LABELS,
    FILTER_GROUPS,
    FILTER_LABELS,
)


def test_safe_and_broad_targets_are_not_neighbours() -> None:
    keys = list(FILTER_LABELS)
    assert abs(keys.index('all') - keys.index('self')) > 1, (
        'РС-14е: «Все» снова стоит вплотную к «только мне» — промах на строку отправит всей базе'
    )


def test_broad_target_is_last_in_the_server_order() -> None:
    """Самое опасное — последним в ответе сервера.

    ⚠️ Это НЕ значит «последним на экране»: кабинет дописывает после базовых фильтров
    тарифные и кастомные. Порядок групп на экране стережёт отдельный тест в кабинете
    (`AdminBroadcastCreate.submitFailures.test.tsx`, «Вся база рисуется ПОСЛЕДНЕЙ группой»).
    Ревью поймало, что прежняя формулировка этого теста утверждала то, чего на экране нет.
    """
    assert list(FILTER_LABELS)[-1] == 'all'


def test_broad_target_lives_in_its_own_group() -> None:
    """Своя группа = свой заголовок на экране, а не общая пачка с безопасным тестом."""
    assert FILTER_GROUPS['self'] != FILTER_GROUPS['all']
    assert [key for key, group in FILTER_GROUPS.items() if group == FILTER_GROUPS['all']] == ['all']


def test_email_broad_target_is_separated_too() -> None:
    """У почты «все» стояла первой строкой в той же раскладке, что признана опасной.

    Своей канарейки «только мне» у почтового канала нет вовсе — сухой прогон письма
    сделать нечем, поэтому цена промаха здесь выше, а не ниже.
    """
    assert list(EMAIL_FILTER_LABELS)[-1] == 'all_email'
    assert EMAIL_FILTER_GROUPS['all_email'] == FILTER_GROUPS['all']
    assert EMAIL_FILTER_GROUPS['all_email'] != 'basic'


def test_every_target_still_has_label_and_group() -> None:
    """Перестановка не должна была осиротить ни один ключ."""
    assert set(FILTER_LABELS) == set(FILTER_GROUPS)
