"""Account tables: the analyst, and the refresh tokens issued to them.

SQLModel classes, so each declaration is both the Pydantic model and the table.
The shared fields live on ``UserBase``, which the API schemas in
:mod:`models.schemas` inherit from — the email and name rules are written once
and the request bodies, the response bodies and the column definitions all come
from that one place.

SQLModel skips validation on ``table=True`` classes: ``User(email="nonsense")``
builds a row without complaint. ``User.model_validate()`` does *not* skip it,
which is why :meth:`services.auth_service.AuthService.signup` builds rows that
way — the rules on ``UserBase`` then hold for anything that reaches the table,
not only for what arrived through a request body.

The two tables are linked by a foreign key with ``ON DELETE CASCADE`` and no
ORM relationship. Nothing here traverses from a user to their tokens — the
queries go straight at ``refresh_tokens`` — and an unused relationship is a
liability under async: reading an unloaded one raises ``MissingGreenlet`` from
wherever it happened to be touched. The database enforces the cascade instead;
see :mod:`core.database` for the pragma that makes SQLite honour it.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import EmailStr, field_validator
from sqlmodel import Field, SQLModel

from models.columns import as_utc, utcnow
from models.columns import timestamp_column as _timestamp
from models.columns import uuid_hex as _uuid


class UserBase(SQLModel):
    """The fields that describe an analyst, wherever they appear.

    Shared by the table, the signup request and the user in every response, so
    there is one definition of what an email and a name are allowed to be.
    """

    email: EmailStr = Field(max_length=320)
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        """Trim the name, and refuse one that is nothing but whitespace.

        On the base rather than the request schema so it holds wherever a name
        does — ``min_length`` alone would happily accept three spaces.
        """
        name = value.strip()
        if not name:
            raise ValueError("Name cannot be blank")
        return name


class User(UserBase, table=True):
    """A registered analyst.

    Only the bcrypt hash of the password is kept — the password itself is
    never written anywhere, including the logs.
    """

    __tablename__ = "users"

    id: str = Field(default_factory=_uuid, primary_key=True, max_length=32)
    # Stored lower-cased so an account cannot be opened twice under the same
    # address in different case. The unique index is what enforces that, rather
    # than a prior SELECT, so two simultaneous signups cannot both win.
    email: EmailStr = Field(max_length=320, index=True, unique=True)
    password_hash: str = Field(max_length=128)
    is_active: bool = Field(default=True)
    created_at: datetime = Field(
        default_factory=utcnow, sa_column=_timestamp(nullable=False)
    )


class RefreshToken(SQLModel, table=True):
    """One issued refresh token, by its ``jti``.

    The token's own signature already proves it was minted here; this row is
    what makes it *revocable* — logging out, rotating on use, or dropping every
    session a user has, all come down to marking rows revoked. A refresh token
    whose jti has no live row here is refused however well it is signed.
    """

    __tablename__ = "refresh_tokens"

    id: str = Field(default_factory=_uuid, primary_key=True, max_length=32)
    jti: str = Field(max_length=32, index=True, unique=True)
    user_id: str = Field(
        foreign_key="users.id", index=True, ondelete="CASCADE", max_length=32
    )
    issued_at: datetime = Field(
        default_factory=utcnow, sa_column=_timestamp(nullable=False)
    )
    expires_at: datetime = Field(sa_column=_timestamp(nullable=False))
    revoked_at: datetime | None = Field(default=None, sa_column=_timestamp(nullable=True))

    @property
    def is_usable(self) -> bool:
        """Whether this token may still be exchanged for a new pair."""
        return self.revoked_at is None and as_utc(self.expires_at) > utcnow()
