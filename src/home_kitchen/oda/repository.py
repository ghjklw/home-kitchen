"""Encrypted actor-scoped Oda OAuth records; plaintext tokens never enter SQL."""

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256

from cryptography.fernet import Fernet
from sqlalchemy import Column, DateTime, Integer, MetaData, Table, Text, delete, insert, select
from sqlalchemy.dialects.postgresql import insert as conflict_insert
from sqlalchemy.engine import Engine

metadata = MetaData()
lifecycles = Table(
    "kitchen_oda_lifecycles",
    metadata,
    Column("actor_id", Text, primary_key=True),
    Column("generation", Integer, nullable=False),
)
connections = Table(
    "kitchen_oda_connections",
    metadata,
    Column("actor_id", Text, primary_key=True),
    Column("encrypted_payload", Text, nullable=False),
)
states = Table(
    "kitchen_oda_states",
    metadata,
    Column("state_hash", Text, primary_key=True),
    Column("actor_id", Text, nullable=False, index=True),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("encrypted_payload", Text, nullable=False),
)


class OdaRepository:
    def __init__(self, engine: Engine, cipher: Fernet):
        self.engine = engine
        self.cipher = cipher

    def encrypt(self, value: dict) -> str:
        return self.cipher.encrypt(json.dumps(value).encode()).decode()

    def decrypt(self, value: str) -> dict:
        return json.loads(self.cipher.decrypt(value.encode()))

    def _bump_generation(self, connection, actor: str) -> int:
        connection.execute(
            conflict_insert(lifecycles)
            .values(actor_id=actor, generation=0)
            .on_conflict_do_nothing(index_elements=[lifecycles.c.actor_id])
        )
        return connection.execute(
            lifecycles.update()
            .where(lifecycles.c.actor_id == actor)
            .values(generation=lifecycles.c.generation + 1)
            .returning(lifecycles.c.generation)
        ).scalar_one()

    def begin_connect(self, actor: str) -> int:
        with self.engine.begin() as connection:
            generation = self._bump_generation(connection, actor)
            connection.execute(delete(states).where(states.c.actor_id == actor))
            return generation

    def _accept_generation(self, connection, actor: str, generation: int) -> bool:
        # The conditional UPDATE holds the actor row lock through the following
        # write. Disconnect cannot invalidate the generation between check/save.
        return (
            connection.execute(
                lifecycles.update()
                .where(
                    lifecycles.c.actor_id == actor,
                    lifecycles.c.generation == generation,
                )
                .values(generation=generation)
                .returning(lifecycles.c.actor_id)
            ).scalar_one_or_none()
            is not None
        )

    def save_state(
        self, state: str, actor: str, expires_at: datetime, payload: dict, generation: int
    ) -> bool:
        with self.engine.begin() as connection:
            if not self._accept_generation(connection, actor, generation):
                return False
            connection.execute(delete(states).where(states.c.expires_at < datetime.now(UTC)))
            # A fresh Connect invalidates earlier pending flows for this actor.
            connection.execute(delete(states).where(states.c.actor_id == actor))
            connection.execute(
                insert(states).values(
                    state_hash=sha256(state.encode()).hexdigest(),
                    actor_id=actor,
                    expires_at=expires_at,
                    encrypted_payload=self.encrypt(payload),
                )
            )
        return True

    def consume_state(self, state: str, actor: str, now: datetime) -> dict | None:
        with self.engine.begin() as connection:
            payload = connection.execute(
                delete(states)
                .where(
                    states.c.state_hash == sha256(state.encode()).hexdigest(),
                    states.c.actor_id == actor,
                    states.c.expires_at > now,
                )
                .returning(states.c.encrypted_payload)
            ).scalar_one_or_none()
        return self.decrypt(payload) if payload else None

    def save_connection(self, actor: str, payload: dict, generation: int) -> bool:
        with self.engine.begin() as connection:
            if not self._accept_generation(connection, actor, generation):
                return False
            connection.execute(delete(connections).where(connections.c.actor_id == actor))
            connection.execute(
                insert(connections).values(
                    actor_id=actor,
                    encrypted_payload=self.encrypt(payload),
                )
            )
        return True

    def has_connection(self, actor: str) -> bool:
        with self.engine.connect() as connection:
            return (
                connection.execute(
                    select(connections.c.actor_id).where(connections.c.actor_id == actor)
                ).first()
                is not None
            )

    def disconnect(self, actor: str) -> dict | None:
        # Fence in-flight Connect and callback HTTP operations before deletion.
        # Also wait for a concurrent refresh to finish, then revoke its new token.
        with self.engine.begin() as connection:
            self._bump_generation(connection, actor)
            payload = connection.execute(
                select(connections.c.encrypted_payload)
                .where(connections.c.actor_id == actor)
                .with_for_update()
            ).scalar_one_or_none()
            self.delete_in_transaction(connection, actor)
        return self.decrypt(payload) if payload else None

    def delete_in_transaction(self, connection, actor: str) -> None:
        connection.execute(delete(connections).where(connections.c.actor_id == actor))
        connection.execute(delete(states).where(states.c.actor_id == actor))

    @contextmanager
    def locked_connection(self, actor: str):
        with self.engine.begin() as connection:
            payload = connection.execute(
                select(connections.c.encrypted_payload)
                .where(connections.c.actor_id == actor)
                .with_for_update()
            ).scalar_one_or_none()
            yield connection, self.decrypt(payload) if payload else None

    def update_connection(self, connection, actor: str, payload: dict) -> None:
        connection.execute(
            connections.update()
            .where(connections.c.actor_id == actor)
            .values(encrypted_payload=self.encrypt(payload))
        )
