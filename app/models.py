"""SQLAlchemy tables and the User helper used by auth."""
from __future__ import annotations

from sqlalchemy import Boolean, Float, Integer, String, Text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base, PersistError, SessionLocal, UserExistsError, normalise_username, utc_now
from .schemas import Token, User as UserPublic, UserCreate
from .security import (
    DUMMY_HASH,
    access_token_expires,
    create_access_token,
    get_password_hash,
    verify_password,
)


class FieldRecord(Base):
    __tablename__ = "fields"

    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[str] = mapped_column(String(64), default="")


class Device(Base):
    __tablename__ = "devices"

    device_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_lat: Mapped[float] = mapped_column(Float)
    last_lng: Mapped[float] = mapped_column(Float)
    last_seen_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    heading: Mapped[float | None] = mapped_column(Float, nullable=True)
    speed_mps: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[str | None] = mapped_column(String(64), nullable=True)


class LocationPing(Base):
    __tablename__ = "location_pings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), index=True)
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    heading: Mapped[float | None] = mapped_column(Float, nullable=True)
    speed_mps: Mapped[float | None] = mapped_column(Float, nullable=True)
    recorded_at: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    received_at: Mapped[str | None] = mapped_column(String(64), nullable=True)


class BusCache(Base):
    __tablename__ = "bus_cache"

    region: Mapped[str] = mapped_column(String(64), primary_key=True)
    fetched_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    vehicles_json: Mapped[str] = mapped_column(Text, default="[]")
    trails_json: Mapped[str] = mapped_column(Text, default="{}")


class User(Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    hashed_password: Mapped[str] = mapped_column(String(256))
    photo: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str | None] = mapped_column(String(64), nullable=True)

    @classmethod
    def get(cls, username: str) -> User | None:
        key = normalise_username(username)
        if not key:
            return None
        with SessionLocal() as session:
            return session.get(cls, key)

    @classmethod
    def create(cls, payload: UserCreate) -> User:
        email = (payload.email or "").strip().lower() or None
        user = cls(
            username=normalise_username(payload.username),
            email=email,
            full_name=payload.full_name,
            disabled=False,
            hashed_password=get_password_hash(payload.password),
            updated_at=utc_now(),
        )
        user.save(create_only=True)
        return user

    @classmethod
    def get_by_email(cls, email: str) -> User | None:
        key = (email or "").strip().lower()
        if not key or "@" not in key:
            return None
        from sqlalchemy import select

        with SessionLocal() as session:
            return session.scalars(select(cls).where(cls.email == key)).first()

    @classmethod
    def authenticate(cls, username: str, password: str) -> User | None:
        user = cls.get(username)
        if not user and "@" in (username or ""):
            user = cls.get_by_email(username)
        if not user:
            verify_password(password, DUMMY_HASH)
            return None
        if not user.check_password(password):
            return None
        return user

    @classmethod
    def from_oauth(cls, *, email: str, full_name: str | None = None, photo: str | None = None) -> User:
        email = (email or "").strip().lower()
        if not email or "@" not in email:
            raise PersistError("Could not store user")
        existing = cls.get_by_email(email)
        if existing:
            if photo and not existing.photo:
                existing.photo = photo
                existing.save()
            if full_name and not existing.full_name:
                existing.full_name = full_name
                existing.save()
            return existing
        import re
        import secrets as _secrets

        base = re.sub(r"[^a-z0-9._-]", "", email.split("@")[0].lower()) or "user"
        name = base[:64]
        n = 0
        while cls.get(name):
            n += 1
            suffix = str(n)
            name = (base[: 64 - len(suffix)] + suffix)
        from .schemas import UserCreate

        user = cls.create(
            UserCreate(
                username=name,
                password=_secrets.token_urlsafe(24),
                email=email,
                full_name=full_name,
            )
        )
        if photo:
            user.photo = photo
            user.save()
        return user

    def rename(self, new_username: str) -> User:
        new_username = normalise_username(new_username)
        if not new_username or new_username == self.username:
            return self
        if User.get(new_username):
            raise UserExistsError("Username already registered")
        old_name = self.username
        clone = User(
            username=new_username,
            email=self.email,
            full_name=self.full_name,
            disabled=self.disabled,
            hashed_password=self.hashed_password,
            photo=self.photo,
            updated_at=utc_now(),
        )
        with SessionLocal() as session:
            try:
                session.add(clone)
                old = session.get(User, old_name)
                if old is not None:
                    session.delete(old)
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise UserExistsError("Username already registered") from exc
            except Exception as exc:
                session.rollback()
                raise PersistError("Could not store user") from exc
        found = User.get(new_username)
        if found is None:
            raise PersistError("Could not store user")
        return found

    def check_password(self, password: str) -> bool:
        return verify_password(password, self.hashed_password)

    def save(self, *, create_only: bool = False) -> None:
        self.username = normalise_username(self.username)
        self.updated_at = utc_now()
        with SessionLocal() as session:
            try:
                if create_only:
                    session.add(self)
                else:
                    session.merge(self)
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                if create_only:
                    raise UserExistsError("Username already registered") from exc
                raise PersistError("Could not store user") from exc
            except Exception as exc:
                session.rollback()
                raise PersistError("Could not store user") from exc

    def public(self) -> UserPublic:
        return UserPublic(
            username=self.username,
            email=self.email,
            full_name=self.full_name,
            photo=self.photo,
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
