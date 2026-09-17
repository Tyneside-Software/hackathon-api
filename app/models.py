"""Domain models.

Not SQLAlchemy: users live in Firestore (Datastore fallback). FastAPI's
SQLAlchemy/SQLModel examples assume a SQL database. Here the User object
is Pydantic plus get/save/authenticate, which is the usual NoSQL shape.
"""
from __future__ import annotations

from pydantic import BaseModel

from .db import load_user_row, normalise_username, write_user_row
from .schemas import Token, User as UserPublic, UserCreate
from .security import (
    DUMMY_HASH,
    access_token_expires,
    create_access_token,
    get_password_hash,
    verify_password,
)


class User(BaseModel):
    username: str
    email: str | None = None
    full_name: str | None = None
    disabled: bool = False
    hashed_password: str

    @classmethod
    def get(cls, username: str) -> User | None:
        row = load_user_row(username)
        if not row:
            return None
        return cls(**row)

    @classmethod
    def create(cls, payload: UserCreate) -> User:
        user = cls(
            username=normalise_username(payload.username),
            email=payload.email,
            full_name=payload.full_name,
            disabled=False,
            hashed_password=get_password_hash(payload.password),
        )
        user.save(create_only=True)
        return user

    @classmethod
    def authenticate(cls, username: str, password: str) -> User | None:
        user = cls.get(username)
        if not user:
            verify_password(password, DUMMY_HASH)
            return None
        if not user.check_password(password):
            return None
        return user

    def check_password(self, password: str) -> bool:
        return verify_password(password, self.hashed_password)

    def save(self, *, create_only: bool = False) -> None:
        write_user_row(self.model_dump(), create_only=create_only)

    def public(self) -> UserPublic:
        return UserPublic(
            username=self.username,
            email=self.email,
            full_name=self.full_name,
            disabled=self.disabled,
        )

    def issue_token(self) -> Token:
        return Token(
            access_token=create_access_token(
                data={"sub": self.username},
                expires_delta=access_token_expires(),
            ),
            token_type="bearer",
        )
