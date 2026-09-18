"""Language persistence at Telegram Cabinet authentication boundaries."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.cabinet.routes import auth
from app.cabinet.schemas.auth import TelegramAuthRequest, TelegramOIDCAuthRequest, TelegramWidgetAuthRequest
from app.database.models import UserStatus
from app.localization import loader


def _user(*, language: str = 'en', status: str = UserStatus.ACTIVE.value) -> SimpleNamespace:
    return SimpleNamespace(
        id=101,
        telegram_id=101,
        username=None,
        first_name=None,
        last_name=None,
        language=language,
        status=status,
        cabinet_last_login=None,
    )


def _response() -> SimpleNamespace:
    return SimpleNamespace(refresh_token='refresh-token', campaign_bonus=None)


def _common_patches(user: SimpleNamespace | None):
    return [
        patch.object(auth, 'get_client_ip', return_value='127.0.0.1'),
        patch.object(auth.RateLimitCache, 'is_ip_rate_limited', new=AsyncMock(return_value=False)),
        patch.object(auth, 'get_user_by_telegram_id', new=AsyncMock(return_value=user)),
        patch.object(auth, '_create_auth_response', new=AsyncMock(return_value=_response())),
        patch.object(auth, '_store_refresh_token', new=AsyncMock()),
        patch.object(auth, '_process_referral_code', new=AsyncMock()),
        patch.object(auth, '_process_campaign_bonus', new=AsyncMock(return_value=None)),
    ]


@pytest.mark.asyncio
async def test_initdata_creation_passes_validated_telegram_language_to_create_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = AsyncMock()
    created = _user(language='en')
    create = AsyncMock(return_value=created)
    monkeypatch.setattr(type(auth.settings), 'get_available_languages', lambda _: ['ru', 'en'])
    monkeypatch.setattr(auth.settings, 'DEFAULT_LANGUAGE', 'ru')
    monkeypatch.setattr(loader, 'has_locale', lambda code: code in {'ru', 'en'})
    patches = _common_patches(None)
    patches.extend(
        [
            patch.object(
                auth,
                'validate_telegram_init_data',
                return_value={'id': 101, 'first_name': 'New', 'language_code': 'EN_gb'},
            ),
            patch.object(auth, 'create_user', new=create),
        ]
    )
    with ExitStack() as stack:
        for active_patch in patches:
            stack.enter_context(active_patch)
        await auth.auth_telegram(TelegramAuthRequest(init_data='signed'), MagicMock(), db)

    assert create.await_args.kwargs['language'] == 'en'


@pytest.mark.asyncio
async def test_initdata_create_race_keeps_language_of_existing_winner() -> None:
    """The CRUD create race may return a /start-created account unchanged."""
    db = AsyncMock()
    winner = _user(language='ru')
    create = AsyncMock(return_value=winner)
    patches = _common_patches(None)
    patches.extend(
        [
            patch.object(
                auth,
                'validate_telegram_init_data',
                return_value={'id': 101, 'first_name': 'New', 'language_code': 'en-US'},
            ),
            patch.object(auth, 'get_telegram_language', return_value='en'),
            patch.object(auth, 'create_user', new=create),
        ]
    )
    with ExitStack() as stack:
        for active_patch in patches:
            stack.enter_context(active_patch)
        await auth.auth_telegram(TelegramAuthRequest(init_data='signed'), MagicMock(), db)

    assert create.await_args.kwargs['language'] == 'en'
    assert winner.language == 'ru'


@pytest.mark.asyncio
async def test_initdata_existing_and_deleted_users_keep_stored_language() -> None:
    for status in (UserStatus.ACTIVE.value, UserStatus.DELETED.value):
        db = AsyncMock()
        existing = _user(language='en', status=status)
        create = AsyncMock()
        resolve = MagicMock(return_value='ru')
        patches = _common_patches(existing)
        patches.extend(
            [
                patch.object(
                    auth,
                    'validate_telegram_init_data',
                    return_value={'id': 101, 'first_name': 'Changed', 'language_code': 'ru-RU'},
                ),
                patch.object(auth, 'get_telegram_language', new=resolve),
                patch.object(auth, 'create_user', new=create),
                patch('app.services.user_revival_service.revive_deleted_user', new=AsyncMock()),
                patch('app.services.referral_service.attach_referrer_if_missing', new=AsyncMock()),
            ]
        )
        with ExitStack() as stack:
            for active_patch in patches:
                stack.enter_context(active_patch)
            await auth.auth_telegram(TelegramAuthRequest(init_data='signed'), MagicMock(), db)

        create.assert_not_awaited()
        resolve.assert_not_called()
        assert existing.language == 'en'


@pytest.mark.asyncio
async def test_widget_creation_uses_shared_fallback_without_a_language_claim() -> None:
    db = AsyncMock()
    created = _user(language='ru')
    resolve = MagicMock(return_value='ru')
    create = AsyncMock(return_value=created)
    patches = _common_patches(None)
    patches.extend(
        [
            patch.object(auth, 'validate_telegram_login_widget', return_value=True),
            patch.object(auth.TokenReplayCache, 'is_token_replayed', new=AsyncMock(return_value=False)),
            patch.object(auth, 'get_telegram_language', new=resolve),
            patch.object(auth, 'create_user', new=create),
        ]
    )
    request = TelegramWidgetAuthRequest(id=101, first_name='New', auth_date=1, hash='a' * 64)
    with ExitStack() as stack:
        for active_patch in patches:
            stack.enter_context(active_patch)
        await auth.auth_telegram_widget(request, MagicMock(), db)

    resolve.assert_called_once_with(None)
    assert create.await_args.kwargs['language'] == 'ru'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('user_status', 'expected_status'), [(UserStatus.ACTIVE.value, None), (UserStatus.DELETED.value, 403)]
)
async def test_widget_existing_or_deleted_user_keeps_stored_language_without_resolving(
    user_status: str,
    expected_status: int | None,
) -> None:
    db = AsyncMock()
    existing = _user(language='en', status=user_status)
    resolve = MagicMock(return_value='ru')
    create = AsyncMock()
    revive = AsyncMock()
    patches = _common_patches(existing)
    patches.extend(
        [
            patch.object(auth, 'validate_telegram_login_widget', return_value=True),
            patch.object(auth.TokenReplayCache, 'is_token_replayed', new=AsyncMock(return_value=False)),
            patch.object(auth, 'get_telegram_language', new=resolve),
            patch.object(auth, 'create_user', new=create),
            patch('app.services.referral_service.attach_referrer_if_missing', new=AsyncMock()),
            patch('app.services.user_revival_service.revive_deleted_user', new=revive),
        ]
    )
    request = TelegramWidgetAuthRequest(id=101, first_name='Changed', auth_date=1, hash='a' * 64)
    with ExitStack() as stack:
        for active_patch in patches:
            stack.enter_context(active_patch)
        if expected_status:
            with pytest.raises(HTTPException) as error:
                await auth.auth_telegram_widget(request, MagicMock(), db)
            assert error.value.status_code == expected_status
        else:
            await auth.auth_telegram_widget(request, MagicMock(), db)

    create.assert_not_awaited()
    resolve.assert_not_called()
    revive.assert_not_awaited()
    assert existing.language == 'en'


@pytest.mark.asyncio
@pytest.mark.parametrize(('locale', 'resolved'), [('EN_gb', 'en'), (None, 'ru')])
async def test_oidc_creation_routes_optional_locale_through_shared_resolver(
    locale: str | None,
    resolved: str,
) -> None:
    db = AsyncMock()
    created = _user(language=resolved)
    resolve = MagicMock(return_value=resolved)
    create = AsyncMock(return_value=created)
    patches = _common_patches(None)
    claims = {
        'id': 101,
        'name': 'New',
        'exp': int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
    }
    if locale is not None:
        claims['locale'] = locale
    patches.extend(
        [
            patch.object(auth, 'get_setting_value', new=AsyncMock(return_value=None)),
            patch.object(auth.settings, 'TELEGRAM_OIDC_ENABLED', True),
            patch.object(auth.settings, 'TELEGRAM_OIDC_CLIENT_ID', 'client-id'),
            patch.object(auth, 'validate_telegram_oidc_token', new=AsyncMock(return_value=claims)),
            patch.object(auth.TokenReplayCache, 'is_token_replayed', new=AsyncMock(return_value=False)),
            patch.object(auth, 'get_telegram_language', new=resolve),
            patch.object(auth, 'create_user', new=create),
        ]
    )
    request = TelegramOIDCAuthRequest(id_token='signed')
    with ExitStack() as stack:
        for active_patch in patches:
            stack.enter_context(active_patch)
        await auth.auth_telegram_oidc(request, MagicMock(), db)

    resolve.assert_called_once_with(locale)
    assert create.await_args.kwargs['language'] == resolved


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('user_status', 'expected_status'), [(UserStatus.ACTIVE.value, None), (UserStatus.DELETED.value, 403)]
)
async def test_oidc_existing_or_deleted_user_keeps_stored_language_without_resolving(
    user_status: str,
    expected_status: int | None,
) -> None:
    db = AsyncMock()
    existing = _user(language='en', status=user_status)
    resolve = MagicMock(return_value='ru')
    create = AsyncMock()
    revive = AsyncMock()
    patches = _common_patches(existing)
    patches.extend(
        [
            patch.object(auth, 'get_setting_value', new=AsyncMock(return_value=None)),
            patch.object(auth.settings, 'TELEGRAM_OIDC_ENABLED', True),
            patch.object(auth.settings, 'TELEGRAM_OIDC_CLIENT_ID', 'client-id'),
            patch.object(
                auth,
                'validate_telegram_oidc_token',
                new=AsyncMock(
                    return_value={
                        'id': 101,
                        'name': 'Changed',
                        'locale': 'ru-RU',
                        'exp': int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
                    }
                ),
            ),
            patch.object(auth.TokenReplayCache, 'is_token_replayed', new=AsyncMock(return_value=False)),
            patch.object(auth, 'get_telegram_language', new=resolve),
            patch.object(auth, 'create_user', new=create),
            patch('app.services.referral_service.attach_referrer_if_missing', new=AsyncMock()),
            patch('app.services.user_revival_service.revive_deleted_user', new=revive),
        ]
    )
    with ExitStack() as stack:
        for active_patch in patches:
            stack.enter_context(active_patch)
        if expected_status:
            with pytest.raises(HTTPException) as error:
                await auth.auth_telegram_oidc(TelegramOIDCAuthRequest(id_token='signed'), MagicMock(), db)
            assert error.value.status_code == expected_status
        else:
            await auth.auth_telegram_oidc(TelegramOIDCAuthRequest(id_token='signed'), MagicMock(), db)

    create.assert_not_awaited()
    resolve.assert_not_called()
    revive.assert_not_awaited()
    assert existing.language == 'en'


@pytest.mark.asyncio
async def test_rejected_telegram_credentials_never_resolve_or_create_a_user() -> None:
    async def assert_rejected(call, patches) -> None:
        resolve = MagicMock()
        create = AsyncMock()
        patches.extend(
            [
                patch.object(auth, 'get_telegram_language', new=resolve),
                patch.object(auth, 'create_user', new=create),
            ]
        )
        with ExitStack() as stack:
            for active_patch in patches:
                stack.enter_context(active_patch)
            with pytest.raises(HTTPException) as error:
                await call()
        assert error.value.status_code == 401
        resolve.assert_not_called()
        create.assert_not_awaited()

    await assert_rejected(
        lambda: auth.auth_telegram(TelegramAuthRequest(init_data='invalid'), MagicMock(), AsyncMock()),
        _common_patches(None) + [patch.object(auth, 'validate_telegram_init_data', return_value=None)],
    )
    await assert_rejected(
        lambda: auth.auth_telegram_widget(
            TelegramWidgetAuthRequest(id=101, first_name='New', auth_date=1, hash='a' * 64), MagicMock(), AsyncMock()
        ),
        _common_patches(None) + [patch.object(auth, 'validate_telegram_login_widget', return_value=False)],
    )
    await assert_rejected(
        lambda: auth.auth_telegram_oidc(TelegramOIDCAuthRequest(id_token='invalid'), MagicMock(), AsyncMock()),
        _common_patches(None)
        + [
            patch.object(auth, 'get_setting_value', new=AsyncMock(return_value=None)),
            patch.object(auth.settings, 'TELEGRAM_OIDC_ENABLED', True),
            patch.object(auth.settings, 'TELEGRAM_OIDC_CLIENT_ID', 'client-id'),
            patch.object(auth, 'validate_telegram_oidc_token', new=AsyncMock(return_value=None)),
        ],
    )
