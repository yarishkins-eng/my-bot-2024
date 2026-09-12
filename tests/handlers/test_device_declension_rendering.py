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
