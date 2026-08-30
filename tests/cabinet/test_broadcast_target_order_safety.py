"""РС-14е: «Все» не стоит соседней строкой с «Только мне».

Экран рассылок рисует кнопки в порядке `FILTER_LABELS` и группирует по `FILTER_GROUPS`.
Пока «Все» была второй строкой сразу под «Тест: только мне», промах пальцем на одну строку
означал отправку всей базе. Подтверждение с числом получателей от этого спасает, но оно
было ЕДИНСТВЕННЫМ рубежом — а рассылками теперь занимается не только владелец.
"""

from app.cabinet.routes.admin_broadcasts import FILTER_GROUPS, FILTER_LABELS


def test_safe_and_broad_targets_are_not_neighbours() -> None:
    keys = list(FILTER_LABELS)
    assert abs(keys.index('all') - keys.index('self')) > 1, (
        'РС-14е: «Все» снова стоит вплотную к «только мне» — промах на строку отправит всей базе'
    )


def test_broad_target_is_last_on_the_screen() -> None:
    """Самое опасное — в самом низу: до него нельзя доехать случайно сверху."""
    assert list(FILTER_LABELS)[-1] == 'all'


def test_broad_target_lives_in_its_own_group() -> None:
    """Своя группа = свой заголовок на экране, а не общая пачка с безопасным тестом."""
    assert FILTER_GROUPS['self'] != FILTER_GROUPS['all']
    assert [key for key, group in FILTER_GROUPS.items() if group == FILTER_GROUPS['all']] == ['all']


def test_every_target_still_has_label_and_group() -> None:
    """Перестановка не должна была осиротить ни один ключ."""
    assert set(FILTER_LABELS) == set(FILTER_GROUPS)
