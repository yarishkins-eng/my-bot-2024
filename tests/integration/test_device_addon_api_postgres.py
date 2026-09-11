"""HTTP contract + real PostgreSQL; auth, payment provider and VPN panel are controlled fakes."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import func, select

from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
from app.cabinet.routes.subscription_modules.device_addon import router
from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.models import DeviceAddonIntent, DeviceAddonTopupAttempt, PlategaPayment, Tariff, Transaction, User
from app.services import device_addon_payment_service as payments, device_addon_worker as worker_module
from app.services.device_addon_service import (
    calculate_device_addon,
    create_intent,
    purchase_intent,
    quote_for_calculation,
)
from app.services.platega_service import PlategaService
from tests.integration.test_device_addon_core_postgres import _active_target, _fake_remnawave_service
from tests.integration.test_device_addon_lifecycle_postgres import DATABASE_URL, seed, sessions  # noqa: F401


pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not DATABASE_URL, reason='Requires isolated addon PostgreSQL')]


def owned_app(sessions, user_id):
    app = FastAPI()
    app.include_router(router, prefix='/cabinet/subscription')

    async def database():
        async with sessions() as db:
            yield db

    async def current_user(db=Depends(get_cabinet_db)):
        return await get_user_by_id(db, user_id)

    app.dependency_overrides[get_cabinet_db] = database
    app.dependency_overrides[get_current_cabinet_user] = current_user
    return app


async def test_other_authenticated_owner_cannot_read_or_mutate_operation(sessions, monkeypatch):
    async with sessions() as db:
        owner, sub, intent, _, attempt = await seed(db, status='pending')
        other = User(telegram_id=7788012250, status='active', referral_code=uuid.uuid4().hex[:12])
        db.add(other)
        await db.commit()
        owner_id, other_id, intent_id, attempt_id, sub_id = (
            owner.id,
            other.id,
            intent.public_id,
            attempt.public_id,
            sub.id,
        )
    provider = AsyncMock(side_effect=AssertionError('an ownership failure must not contact a provider'))
    monkeypatch.setattr(payments, 'PlategaService', provider)
    app = owned_app(sessions, other_id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        responses = [
            await client.get(f'/cabinet/subscription/devices/intents/{intent_id}'),
            await client.get(f'/cabinet/subscription/devices/topups/{attempt_id}'),
            await client.get('/cabinet/subscription/devices/quote', params={'subscription_id': sub_id, 'devices': 1}),
            await client.post(
                f'/cabinet/subscription/devices/intents/{intent_id}/purchase',
                json={'quote_token': 'untrusted-token-value'},
            ),
            await client.post(
                f'/cabinet/subscription/devices/intents/{intent_id}/topup',
                json={
                    'idempotency_key': str(uuid.uuid4()),
                    'expected_amount_kopeks': 10000,
                    'payment_option': '2',
                },
            ),
        ]
        assert [response.status_code for response in responses] == [404] * 5
    provider.assert_not_called()
    async with sessions() as db:
        assert (await db.get(User, owner_id)).balance_kopeks == 0
        assert await db.scalar(select(func.count(Transaction.id))) == 0


async def test_kill_switch_preserves_receipt_and_blocks_new_money_actions(sessions, monkeypatch):
    monkeypatch.setattr(settings, 'DEVICE_ADDON_PURCHASE_ENABLED', True)
    async with sessions() as db:
        user, sub = await _active_target(db, balance=10000)
        quote = quote_for_calculation(
            await calculate_device_addon(db, user=user, subscription_id=sub.id, devices_to_add=1), user_id=user.id
        )
        bought = await create_intent(db, user=user, quote_token=quote['quote_token'], idempotency_key='bought')
        await purchase_intent(db, user=user, public_id=bought.public_id, quote_token=quote['quote_token'])
        fresh = quote_for_calculation(
            await calculate_device_addon(db, user=user, subscription_id=sub.id, devices_to_add=1), user_id=user.id
        )
        draft = await create_intent(db, user=user, quote_token=fresh['quote_token'], idempotency_key='draft')
        user_id, bought_id, draft_id = user.id, bought.public_id, draft.public_id
        before_balance = user.balance_kopeks
        ledger_count = await db.scalar(select(func.count(Transaction.id)))
    monkeypatch.setattr(settings, 'DEVICE_ADDON_PURCHASE_ENABLED', False)
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.test')
    monkeypatch.setattr(payments, 'available_platega_methods_for_db', AsyncMock(return_value=[{'provider_code': 2}]))
    app = owned_app(sessions, user_id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        replay = await client.post(
            f'/cabinet/subscription/devices/intents/{bought_id}/purchase', json={'quote_token': quote['quote_token']}
        )
        assert replay.status_code == 200 and replay.json()['purchase_state'] == 'purchased'
        actions = [
            await client.post(
                '/cabinet/subscription/devices/intents',
                json={'quote_token': fresh['quote_token'], 'idempotency_key': 'new-after-switch'},
            ),
            await client.post(
                f'/cabinet/subscription/devices/intents/{draft_id}/purchase', json={'quote_token': fresh['quote_token']}
            ),
            await client.post(
                f'/cabinet/subscription/devices/intents/{draft_id}/topup',
                json={'payment_option': '2', 'expected_amount_kopeks': 10000, 'idempotency_key': 'new-invoice'},
            ),
        ]
        assert [response.status_code for response in actions] == [503] * 3
    async with sessions() as db:
        assert (await db.get(User, user_id)).balance_kopeks == before_balance
        assert await db.scalar(select(func.count(Transaction.id))) == ledger_count
        assert await db.scalar(select(func.count(DeviceAddonTopupAttempt.id))) == 0


@pytest.mark.parametrize('return_surface', ['cabinet', 'telegram'])
async def test_http_quote_invoice_owned_return_and_manual_purchase(sessions, monkeypatch, return_surface):
    monkeypatch.setattr(settings, 'DEVICE_ADDON_PURCHASE_ENABLED', True)
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.test')
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'teplo_test_bot')
    monkeypatch.setattr(settings, 'PLATEGA_MIN_AMOUNT_KOPEKS', 10000)
    monkeypatch.setattr(settings, 'PLATEGA_MAX_AMOUNT_KOPEKS', 10000000)
    monkeypatch.setattr(settings, 'CABINET_JWT_SECRET', 'device-addon-local-test-secret')
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(payments, 'available_platega_methods_for_db', AsyncMock(return_value=[{'provider_code': 2}]))
    async with sessions() as db:
        user, sub, intent, _, _ = await seed(db, with_attempt=False)
        user.remnawave_uuid = str(uuid.uuid4())
        sub.remnawave_uuid = user.remnawave_uuid
        sub.status = 'active'
        sub.end_date = datetime.now(UTC) + timedelta(days=29)
        tariff = Tariff(name='addon api test', device_limit=2, max_device_limit=10, device_price_kopeks=5000)
        db.add(tariff)
        await db.flush()
        sub.tariff_id = tariff.id
        await db.delete(intent)
        await db.commit()
        user_id, sub_id = user.id, sub.id

    provider_id = str(uuid.uuid4())
    observed = {}
    provider_posts = []

    class FakeProvider(PlategaService):
        def __init__(self):
            self._max_retries = 3

        async def create_device_addon_payment(self, **kwargs):
            assert self._max_retries == 1
            provider_posts.append(kwargs)
            async with sessions() as check:
                saved = await check.scalar(select(DeviceAddonTopupAttempt))
                local = await check.get(PlategaPayment, saved.platega_payment_id)
                assert saved.status == 'dispatching'
                assert local.correlation_id in kwargs['payload']
            observed.update(
                {
                    'transactionId': provider_id,
                    'paymentMethod': 'SBPQR',
                    'status': 'PENDING',
                    'paymentDetails': {'amount': kwargs['amount'], 'currency': 'RUB'},
                    'payload': kwargs['payload'],
                    'redirect': 'https://bank.example.test/invoice',
                }
            )
            return dict(observed)

        async def get_transaction(self, transaction_id):
            assert transaction_id == provider_id
            return dict(observed)

    monkeypatch.setattr(payments, 'PlategaService', FakeProvider)
    app = FastAPI()
    app.include_router(router, prefix='/cabinet/subscription')

    async def database():
        async with sessions() as db:
            yield db

    async def current_user(db=Depends(get_cabinet_db)):
        return await get_user_by_id(db, user_id)

    app.dependency_overrides[get_cabinet_db] = database
    app.dependency_overrides[get_current_cabinet_user] = current_user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        quote_response = await client.get(
            '/cabinet/subscription/devices/quote', params={'subscription_id': sub_id, 'devices': 2}
        )
        assert quote_response.status_code == 200, quote_response.text
        quote = quote_response.json()
        assert quote['devices_to_add'] == 2
        created = await client.post(
            '/cabinet/subscription/devices/intents',
            json={'quote_token': quote['quote_token'], 'idempotency_key': str(uuid.uuid4())},
        )
        assert created.status_code == 201, created.text
        intent_id = created.json()['id']
        payload = {
            'idempotency_key': str(uuid.uuid4()),
            'expected_amount_kopeks': 10000,
            'payment_method': 'platega',
            'payment_option': '2',
            'return_surface': return_surface,
        }
        concurrent = await asyncio.gather(
            client.post(f'/cabinet/subscription/devices/intents/{intent_id}/topup', json=payload),
            client.post(f'/cabinet/subscription/devices/intents/{intent_id}/topup', json=payload),
        )
        assert [response.status_code for response in concurrent] == [201, 201]
        assert len({response.json()['attempt']['id'] for response in concurrent}) == 1
        # A concurrent replay may return the durable dispatching state before
        # the winning POST has finished canonical verification.
        topup = next(response for response in concurrent if response.json()['payment_url'])
        attempt_id = topup.json()['attempt']['id']
        assert topup.json()['payment_url'] == 'https://bank.example.test/invoice'
        replay = await client.post(f'/cabinet/subscription/devices/intents/{intent_id}/topup', json=payload)
        assert replay.status_code == 201, replay.text
        assert replay.json()['attempt']['id'] == attempt_id
        assert len(provider_posts) == 1
        returned = await client.get(f'/cabinet/subscription/devices/topups/{attempt_id}')
        assert returned.status_code == 200, returned.text
        assert returned.json()['payment_url'] == topup.json()['payment_url']
        assert returned.json()['attempt']['status'] == 'pending'
        if return_surface == 'telegram':
            assert provider_posts[0]['return_url'] == f'https://t.me/teplo_test_bot?startapp=dtu-{attempt_id}'
        else:
            assert f'/subscription/device-topup/{intent_id}' in provider_posts[0]['return_url']
            assert f'attempt={attempt_id}' in provider_posts[0]['return_url']

        observed['status'] = 'CONFIRMED'
        monkeypatch.setattr(settings, 'DEVICE_ADDON_PURCHASE_ENABLED', False)
        async with sessions() as db:
            attempt = await db.scalar(
                select(DeviceAddonTopupAttempt).where(DeviceAddonTopupAttempt.public_id == attempt_id)
            )
            for _ in range(2):
                await payments.reconcile_device_addon_payment(db, attempt_id=attempt.id, payload=dict(observed))
        owned = await client.get(f'/cabinet/subscription/devices/intents/{intent_id}')
        assert owned.status_code == 200, owned.text
        assert owned.json()['purchase_state'] == 'draft'
        assert owned.json()['devices_to_add'] == 2
        assert owned.json()['quote']['balance_kopeks'] == 10000
        monkeypatch.setattr(settings, 'DEVICE_ADDON_PURCHASE_ENABLED', True)
        purchase_payload = {'quote_token': owned.json()['quote']['quote_token']}
        bought = await client.post(f'/cabinet/subscription/devices/intents/{intent_id}/purchase', json=purchase_payload)
        assert bought.status_code == 200, bought.text
        repeated = await client.post(
            f'/cabinet/subscription/devices/intents/{intent_id}/purchase', json=purchase_payload
        )
        assert repeated.status_code == 200, repeated.text
        assert bought.json()['receipt'] == repeated.json()['receipt']
        assert bought.json()['receipt']['new_device_limit'] == 4
        assert bought.json()['fulfillment_status'] == 'pending'
        monkeypatch.setattr(settings, 'DEVICE_ADDON_PURCHASE_ENABLED', False)
        # Continue the same HTTP purchase through failed external delivery and
        # a fresh worker process object. The durable receipt must survive both.
        panel_calls = []
        monkeypatch.setattr(worker_module, 'AsyncSessionLocal', sessions)
        _fake_remnawave_service(monkeypatch, calls=panel_calls, result=None)
        worker = worker_module.DeviceAddonWorker()
        claim = await worker._claim_one()
        assert claim is not None
        await worker._fulfill_claim(*claim)
        pending = await client.get(f'/cabinet/subscription/devices/intents/{intent_id}')
        assert pending.json()['fulfillment_status'] == 'pending'
        assert pending.json()['receipt'] == bought.json()['receipt']
        async with sessions() as db:
            stored = await db.scalar(select(DeviceAddonIntent).where(DeviceAddonIntent.public_id == intent_id))
            stored.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
        _fake_remnawave_service(monkeypatch, calls=panel_calls, result=True)
        restarted_worker = worker_module.DeviceAddonWorker()
        claim = await restarted_worker._claim_one()
        assert claim is not None
        await restarted_worker._fulfill_claim(*claim)
        ready = await client.get(f'/cabinet/subscription/devices/intents/{intent_id}')
        assert ready.json()['fulfillment_status'] == 'ready'
        assert ready.json()['receipt'] == bought.json()['receipt']
        assert await restarted_worker._claim_one() is None
        assert len(panel_calls) == 2
        async with sessions() as db:
            assert await db.scalar(select(func.count(Transaction.id))) == 2
            current = await db.get(User, user_id)
            assert current.balance_kopeks == 10000 - bought.json()['receipt']['amount_paid_kopeks']
