"""Security: identity / auth secrets must not be editable via the settings API.

Privilege-escalation guard. PUT /cabinet/admin/settings/{key} only requires the
`settings:edit` permission, and the settings service exposes every model field
except EXCLUDED_KEYS. ADMIN_EMAILS grants admin (settings.is_admin), and the JWT
/ web-api / webhook secrets let an attacker forge sessions and requests — none of
these may be writable by a delegated admin, or they self-promote to superadmin.
"""

from __future__ import annotations

import pytest

from app.services.system_settings_service import BotConfigurationService


EXCLUDED_AUTH_KEYS = [
    'ADMIN_EMAILS',
    'CABINET_JWT_SECRET',
    'WEB_API_DEFAULT_TOKEN',
    'WEB_API_TOKEN_HMAC_SECRET',
    'WEBHOOK_SECRET_TOKEN',
]


def test_identity_and_auth_secrets_are_excluded() -> None:
    for key in EXCLUDED_AUTH_KEYS:
        assert key in BotConfigurationService.EXCLUDED_KEYS, f'{key} must be in EXCLUDED_KEYS'


def test_excluded_keys_have_no_editable_definition() -> None:
    # No definition -> admin_settings update_setting's get_definition() raises
    # KeyError -> 404, so a settings:edit admin cannot write these keys.
    for key in EXCLUDED_AUTH_KEYS:
        with pytest.raises(KeyError):
            BotConfigurationService.get_definition(key)


def test_dead_trial_warning_hours_is_not_offered_on_the_settings_screen() -> None:
    """ВК-4 (АП-0): «Предупреждать за N часов» бот не читает нигде, а в боевой базе уже стоит «3».

    Письмо «пробный скоро истекает» берёт срок из раздела «Автосообщения» (6 ч). Экран «Настройки»
    обещал рычаг, которого нет. Соседний ключ того же раздела обязан остаться — иначе проверка
    прошла бы и на пустом списке настроек.
    """
    assert 'TRIAL_WARNING_HOURS' in BotConfigurationService.EXCLUDED_KEYS
    with pytest.raises(KeyError):
        BotConfigurationService.get_definition('TRIAL_WARNING_HOURS')
    assert BotConfigurationService.get_definition('TRIAL_DURATION_DAYS').key == 'TRIAL_DURATION_DAYS'
