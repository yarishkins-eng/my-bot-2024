from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import MissingGreenlet

from app.services.admin_notification_service import AdminNotificationService


class _UserWithUnloadedPromoGroup:
    id, telegram_id, promo_group_id = 214, 100_214, 7

    def __getattr__(self, name: str):
        if name == 'promo_group':
            raise MissingGreenlet('synchronous lazy load is forbidden')
        raise AttributeError(name)


@pytest.mark.asyncio
@pytest.mark.parametrize('refresh_fails', [False, True])
async def test_promo_group_lookup_refreshes_before_reading_unloaded_relationship(refresh_fails: bool) -> None:
    service = AdminNotificationService(MagicMock())
    user = _UserWithUnloadedPromoGroup()
    promo_group = SimpleNamespace(id=7, name='Organic')
    db = AsyncMock()

    async def load_relationship(target, *, attribute_names):
        assert target is user
        assert attribute_names == ['promo_group']
        target.__dict__['promo_group'] = promo_group

    db.refresh.side_effect = RuntimeError('refresh failed') if refresh_fails else load_relationship
    fallback = AsyncMock(return_value=promo_group)

    with patch('app.services.admin_notification_service.get_promo_group_by_id', fallback):
        assert await service._get_user_promo_group(db, user) is promo_group
    db.refresh.assert_awaited_once_with(user, attribute_names=['promo_group'])
    if refresh_fails:
        fallback.assert_awaited_once_with(db, user.promo_group_id)
    else:
        fallback.assert_not_awaited()
