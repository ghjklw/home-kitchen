import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select, update

from home_kitchen.mcp_oauth_storage import PostgresOAuthStore, oauth_records


@pytest.mark.asyncio
async def test_oauth_state_persists_encrypted_across_instances_with_ttl(test_database_url):
    engine = create_engine(test_database_url)
    key = Fernet.generate_key().decode()
    first = PostgresOAuthStore(engine, key)
    second = PostgresOAuthStore(engine, key)
    try:
        await first.put(
            "client", {"refresh_token": "test-refresh-value"}, collection="tokens", ttl=300
        )
        assert await second.get("client", collection="tokens") == {
            "refresh_token": "test-refresh-value"
        }
        _, ttl = await second.ttl("client", collection="tokens")
        assert 0 < ttl <= 300
        with engine.begin() as connection:
            encrypted = connection.execute(select(oauth_records.c.encrypted_payload)).scalar_one()
            assert "test-refresh-value" not in encrypted
            assert "refresh_token" not in encrypted
            connection.execute(
                update(oauth_records).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        assert await second.get("client", collection="tokens") is None
        await first.put("registration", {"client_secret": "test-client-value"})
        with engine.begin() as connection:
            assert connection.execute(select(oauth_records.c.key)).scalars().all() == [
                sha256(b"registration").hexdigest()
            ]
        assert await second.get("registration") == {"client_secret": "test-client-value"}
        assert await second.delete("registration")
        assert not await first.delete("registration")
        await first.put("one-use-code", {"value": "protected"}, collection="one-use", ttl=60)
        consumed = await asyncio.gather(
            first.take("one-use-code", collection="one-use"),
            second.take("one-use-code", collection="one-use"),
        )
        assert consumed.count({"value": "protected"}) == 1
        assert consumed.count(None) == 1
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_wrong_key_tampering_and_row_swapping_fail_closed(test_database_url):
    engine = create_engine(test_database_url)
    store = PostgresOAuthStore(engine, Fernet.generate_key().decode())
    wrong_key = PostgresOAuthStore(engine, Fernet.generate_key().decode())
    try:
        await store.put("one", {"token": "private"}, collection="tokens")
        assert await wrong_key.get("one", collection="tokens") is None
        with engine.begin() as connection:
            connection.execute(update(oauth_records).values(key=sha256(b"two").hexdigest()))
        assert await store.get("two", collection="tokens") is None
        with engine.begin() as connection:
            connection.execute(update(oauth_records).values(encrypted_payload="corrupted"))
        assert await store.get("two", collection="tokens") is None
    finally:
        engine.dispose()
