from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import DiscountOffer, User


class PromoOfferService:
    """Reject retired raw-Squad promo-access flows.

    Public-location entitlement exceptions need their own owner-approved
    policy and immutable snapshot model.  Until that exists, test-access
    offers must not alter a subscription's technical Squad projection.
    """

    async def grant_test_access(
        self,
        db: AsyncSession,
        user: User,
        offer: DiscountOffer,
    ) -> tuple[bool, list[str] | None, datetime | None, str]:
        del db, user, offer
        return False, None, None, 'public_location_exception_not_supported'

    async def cleanup_expired_test_access(self, db: AsyncSession) -> int:
        """Do not replay historical raw-Squad temporary-access records."""
        del db
        return 0


promo_offer_service = PromoOfferService()
