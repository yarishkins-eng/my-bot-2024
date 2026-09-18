import re

import pytest

from app.localization.texts import get_texts
from app.utils.formatters import format_devices_declension


DEVICE_TEXT_KEYS = (
    'CHANGE_DEVICES_PROMPT_TARIFF',
    'CHANGE_DEVICES_PROMPT',
    'DEVICE_CHANGE_RESET_WARNING',
    'DEVICE_CHANGE_CONFIRMATION',
    'DEVICE_MANAGEMENT_OVERVIEW',
    'DEVICE_RESET_ALL_SUCCESS_MESSAGE',
    'DEVICE_RESET_PARTIAL_MESSAGE',
)


@pytest.mark.parametrize(('language', 'broken_form'), [('ru', '1 устройств'), ('en', '1 devices')])
def test_device_management_texts_render_one_device_correctly(language, broken_form):
    label = format_devices_declension(1, language)
    values = {
        'current_devices_label': label,
        'connected_devices_label': label,
        'new': 1,
        'new_devices_label': label,
        'total_devices_label': label,
        'count_devices_label': label,
        'success_devices_label': label,
        'failed_devices_label': label,
        'price': '149 ₽',
        'action': 'test',
        'cost': 'test',
        'page': 1,
        'pages': 1,
    }

    for key in DEVICE_TEXT_KEYS:
        rendered = get_texts(language).t(key).format(**values)
        assert label in rendered, key
        assert re.search(rf'{re.escape(broken_form)}(?!\w)', rendered) is None, key


def test_reset_warning_declines_only_the_connected_device_count():
    rendered = (
        get_texts('ru')
        .t('DEVICE_CHANGE_RESET_WARNING')
        .format(
            connected_devices_label=format_devices_declension(1, 'ru'),
            new=2,
        )
    )

    assert 'У вас подключено 1 устройство.' in rendered
    assert 'При уменьшении лимита до 2 все устройства будут сброшены.' in rendered
    assert 'до 2 устройства' not in rendered
