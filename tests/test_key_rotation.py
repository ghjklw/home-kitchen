"""Unit tests for MultiFernet key rotation and credential re-encryption."""

from uuid import uuid4

import pytest
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from pydantic import SecretStr, ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from home_kitchen.connector_store import (
    ConnectorStore,
    connector_credentials,
    resolve_connector_cipher,
)
from home_kitchen.connector_store import (
    metadata as connector_metadata,
)
from home_kitchen.crypto import build_multi_fernet
from home_kitchen.mcp_oauth_storage import PostgresOAuthStore
from home_kitchen.oda.repository import OdaRepository
from home_kitchen.oda.repository import metadata as oda_metadata
from home_kitchen.oda.service import OdaService
from home_kitchen.settings import Settings

TEST_DB_URL = "postgresql+psycopg://user:pwd@localhost/db"


@pytest.fixture
def connector_engine():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    connector_metadata.create_all(engine)
    return engine


@pytest.fixture
def oda_engine():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    oda_metadata.create_all(engine)
    return engine


def test_build_multi_fernet_single_key_backwards_compatibility():
    key = Fernet.generate_key().decode()
    cipher = build_multi_fernet(key)
    raw = Fernet(key.encode())

    assert isinstance(cipher, MultiFernet)
    from_multi = cipher.encrypt(b"secret-payload")
    assert raw.decrypt(from_multi) == b"secret-payload"

    from_raw = raw.encrypt(b"legacy-payload")
    assert cipher.decrypt(from_raw) == b"legacy-payload"


def test_build_multi_fernet_comma_separated_rotation():
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()

    cipher = build_multi_fernet(f" {new_key} , {old_key} , ")
    old_fernet = Fernet(old_key.encode())
    new_fernet = Fernet(new_key.encode())

    old_ciphertext = old_fernet.encrypt(b"from-old-key")
    assert cipher.decrypt(old_ciphertext) == b"from-old-key"

    new_ciphertext = cipher.encrypt(b"encrypted-with-primary")
    assert new_fernet.decrypt(new_ciphertext) == b"encrypted-with-primary"
    with pytest.raises(InvalidToken):
        old_fernet.decrypt(new_ciphertext)


@pytest.mark.parametrize(
    "bad_secret",
    [
        "",
        "   ",
        ",",
        " , , ",
        "not-a-valid-fernet-key",
        f"{Fernet.generate_key().decode()},not-a-valid-fernet-key",
        f"not-a-valid-fernet-key,{Fernet.generate_key().decode()}",
    ],
)
def test_build_multi_fernet_rejects_empty_or_invalid_keys(bad_secret: str):
    with pytest.raises(ValueError):
        build_multi_fernet(bad_secret)


def test_settings_accepts_rotated_keys_and_rejects_invalid_keys():
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()
    mcp_old = Fernet.generate_key().decode()
    mcp_new = Fernet.generate_key().decode()

    settings = Settings(
        local_dev=True,
        database_url=SecretStr(TEST_DB_URL),
        oda_encryption_key=SecretStr(f"{new_key},{old_key}"),
        connector_encryption_key=SecretStr(f"{new_key}, {old_key}"),
    )
    assert settings.oda_encryption_key is not None
    assert settings.connector_encryption_key is not None

    prod_oauth = Settings(
        local_dev=False,
        database_url=SecretStr(TEST_DB_URL),
        entra_tenant_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        entra_audience="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        entra_allowed_group="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        mcp_oauth_enabled=True,
        mcp_public_url="https://kitchen.example/mcp",
        mcp_client_secret=SecretStr("test-only-upstream-client-secret"),
        mcp_signing_key=SecretStr("test-only-broker-signing-material-32-bytes"),
        mcp_encryption_key=SecretStr(f"{mcp_new},{mcp_old}"),
        oda_encryption_key=SecretStr(f"{new_key},{old_key}"),
    )
    assert prod_oauth.mcp_encryption_key is not None

    for invalid_value in ("", ",", "invalid", f"{new_key},invalid", f"invalid,{old_key}"):
        with pytest.raises(ValidationError):
            Settings(
                local_dev=True,
                database_url=SecretStr(TEST_DB_URL),
                oda_encryption_key=SecretStr(invalid_value),
            )
        with pytest.raises(ValidationError):
            Settings(
                local_dev=True,
                database_url=SecretStr(TEST_DB_URL),
                connector_encryption_key=SecretStr(invalid_value),
            )
        with pytest.raises(ValidationError):
            Settings(
                local_dev=False,
                database_url=SecretStr(TEST_DB_URL),
                entra_tenant_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                entra_audience="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                entra_allowed_group="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                mcp_oauth_enabled=True,
                mcp_public_url="https://kitchen.example/mcp",
                mcp_client_secret=SecretStr("test-only-upstream-client-secret"),
                mcp_signing_key=SecretStr("test-only-broker-signing-material-32-bytes"),
                mcp_encryption_key=SecretStr(invalid_value),
            )

    with pytest.raises(ValidationError):
        Settings(
            local_dev=False,
            database_url=SecretStr(TEST_DB_URL),
            entra_tenant_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            entra_audience="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            entra_allowed_group="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            mcp_oauth_enabled=True,
            mcp_public_url="https://kitchen.example/mcp",
            mcp_client_secret=SecretStr("test-only-upstream-client-secret"),
            mcp_signing_key=SecretStr("test-only-broker-signing-material-32-bytes"),
            mcp_encryption_key=SecretStr(f"{mcp_new},{old_key}"),
            oda_encryption_key=SecretStr(f"{new_key},{old_key}"),
        )


def test_resolve_connector_cipher_reads_old_and_encrypts_with_new(connector_engine):
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()
    household_id = uuid4()

    legacy_store = ConnectorStore(connector_engine, Fernet(old_key.encode()))
    legacy_store.save_connection(
        household_id=household_id,
        provider_id="rema1000",
        auth_mode="session_token",
        credentials={"session_token": "old-secret"},
    )

    rotated_settings = Settings(
        local_dev=True,
        database_url=SecretStr(TEST_DB_URL),
        connector_encryption_key=SecretStr(f"{new_key},{old_key}"),
    )
    rotated_cipher = resolve_connector_cipher(rotated_settings)
    assert isinstance(rotated_cipher, MultiFernet)
    rotated_store = ConnectorStore(connector_engine, rotated_cipher)

    assert rotated_store.get_credentials(household_id, "rema1000") == {
        "session_token": "old-secret"
    }

    rotated_store.save_connection(
        household_id=household_id,
        provider_id="trumf",
        auth_mode="session_cookie",
        credentials={"session_cookie": "new-secret"},
    )
    with connector_engine.connect() as conn:
        trumf_encrypted = conn.execute(
            select(connector_credentials.c.encrypted_credentials).where(
                connector_credentials.c.household_id == household_id,
                connector_credentials.c.provider_id == "trumf",
            )
        ).scalar_one()

    assert b"new-secret" in Fernet(new_key.encode()).decrypt(trumf_encrypted.encode())
    with pytest.raises(InvalidToken):
        Fernet(old_key.encode()).decrypt(trumf_encrypted.encode())

    fallback_settings = Settings(
        local_dev=True,
        database_url=SecretStr(TEST_DB_URL),
        oda_encryption_key=SecretStr(f"{new_key},{old_key}"),
    )
    fallback_cipher = resolve_connector_cipher(fallback_settings)
    assert isinstance(fallback_cipher, MultiFernet)
    assert fallback_cipher.decrypt(Fernet(old_key.encode()).encrypt(b"ok")) == b"ok"


def test_connector_store_rotate_encrypted_credentials(connector_engine):
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()
    household_a = uuid4()
    household_b = uuid4()

    old_store = ConnectorStore(connector_engine, Fernet(old_key.encode()))
    old_store.save_connection(
        household_id=household_a,
        provider_id="rema1000",
        auth_mode="session_token",
        credentials={"session_token": "secret-a1"},
    )
    old_store.save_connection(
        household_id=household_a,
        provider_id="coop",
        auth_mode="session_token",
        credentials={"session_token": "secret-a2"},
    )
    old_store.save_connection(
        household_id=household_b,
        provider_id="trumf",
        auth_mode="session_cookie",
        credentials={"session_cookie": "secret-b1"},
    )

    multi_store = ConnectorStore(connector_engine, build_multi_fernet(f"{new_key},{old_key}"))

    # Scoped rotation only re-encrypts household_a.
    assert multi_store.rotate_encrypted_credentials(household_id=household_a) == 2

    with connector_engine.connect() as conn:
        rows = {
            row.provider_id: row.encrypted_credentials
            for row in conn.execute(
                select(
                    connector_credentials.c.provider_id,
                    connector_credentials.c.encrypted_credentials,
                )
            ).all()
        }

    new_fernet = Fernet(new_key.encode())
    old_fernet = Fernet(old_key.encode())

    for provider in ("rema1000", "coop"):
        assert new_fernet.decrypt(rows[provider].encode())
        with pytest.raises(InvalidToken):
            old_fernet.decrypt(rows[provider].encode())

    # household_b is still encrypted with old_key until full rotation runs.
    assert old_fernet.decrypt(rows["trumf"].encode())

    # Full rotation re-encrypts all remaining rows.
    assert multi_store.rotate_encrypted_credentials() == 3

    # Dropping the old key entirely still allows reading every credential.
    new_only_store = ConnectorStore(connector_engine, build_multi_fernet(new_key))
    assert new_only_store.get_credentials(household_a, "rema1000") == {"session_token": "secret-a1"}
    assert new_only_store.get_credentials(household_a, "coop") == {"session_token": "secret-a2"}
    assert new_only_store.get_credentials(household_b, "trumf") == {"session_cookie": "secret-b1"}


def test_connector_store_rotate_supports_raw_fernet_and_fails_closed(connector_engine):
    key = Fernet.generate_key()
    household_id = uuid4()

    raw_store = ConnectorStore(connector_engine, Fernet(key))
    raw_store.save_connection(
        household_id=household_id,
        provider_id="rema1000",
        auth_mode="session_token",
        credentials={"session_token": "raw-secret"},
    )
    assert raw_store.rotate_encrypted_credentials() == 1
    assert raw_store.get_credentials(household_id, "rema1000") == {"session_token": "raw-secret"}

    unconfigured = ConnectorStore(connector_engine, None)
    with pytest.raises(RuntimeError):
        unconfigured.rotate_encrypted_credentials()

    wrong_store = ConnectorStore(
        connector_engine, build_multi_fernet(Fernet.generate_key().decode())
    )
    with pytest.raises(InvalidToken):
        wrong_store.rotate_encrypted_credentials()


def test_oda_service_and_repository_support_rotated_keys_and_raw_fernet(oda_engine):
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()

    legacy_repo = OdaRepository(oda_engine, Fernet(old_key.encode()))
    gen_legacy = legacy_repo.begin_connect("actor-old")
    assert legacy_repo.save_connection("actor-old", {"access_token": "oda-old-tok"}, gen_legacy)

    settings = Settings(
        local_dev=True,
        database_url=SecretStr(TEST_DB_URL),
        oda_encryption_key=SecretStr(f"{new_key},{old_key}"),
    )
    service = OdaService(oda_engine, settings)
    try:
        assert service.repository is not None
        assert isinstance(service.repository.cipher, MultiFernet)
        assert service.repository.has_connection("actor-old")

        with service.repository.locked_connection("actor-old") as (_, payload):
            assert payload == {"access_token": "oda-old-tok"}

        gen_new = service.repository.begin_connect("actor-new")
        assert service.repository.save_connection(
            "actor-new", {"access_token": "oda-new-tok"}, gen_new
        )

        new_only_repo = OdaRepository(oda_engine, new_key)
        with new_only_repo.locked_connection("actor-new") as (_, payload):
            assert payload == {"access_token": "oda-new-tok"}
    finally:
        service.close()


def test_postgres_oauth_store_accepts_rotated_keys_and_raw_fernet(oda_engine):
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()

    rotated_store = PostgresOAuthStore(oda_engine, f"{new_key},{old_key}")
    assert isinstance(rotated_store.fernet, MultiFernet)

    old_ciphertext = Fernet(old_key.encode()).encrypt(b"oauth-old")
    assert rotated_store.fernet.decrypt(old_ciphertext) == b"oauth-old"

    new_ciphertext = rotated_store.fernet.encrypt(b"oauth-new")
    assert Fernet(new_key.encode()).decrypt(new_ciphertext) == b"oauth-new"
    with pytest.raises(InvalidToken):
        Fernet(old_key.encode()).decrypt(new_ciphertext)

    raw_store = PostgresOAuthStore(oda_engine, Fernet(old_key.encode()))
    assert isinstance(raw_store.fernet, Fernet)
    assert raw_store.fernet.decrypt(old_ciphertext) == b"oauth-old"
