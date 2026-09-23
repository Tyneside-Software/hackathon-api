"""Authentication routes — OAuth2 password flow + register."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from ..database import PersistError, UserExistsError
from ..dependencies import get_current_active_user
from ..models import User
from ..schemas import LoginRequest, Token, User as UserPublic, UserCreate, UserUpdate

router = APIRouter()


def login_or_401(username: str, password: str) -> Token:
    user = User.authenticate(username, password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email, username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if user.disabled:
        raise HTTPException(status_code=400, detail="Inactive user")
    return user.issue_token()


@router.post("/register", response_model=UserPublic, status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate) -> UserPublic:
    if User.get(payload.username):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already registered")
    email = (payload.email or "").strip().lower()
    if email:
        if "@" not in email:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Enter a real email")
        if User.get_by_email(email):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")
    try:
        return User.create(payload).public()
    except UserExistsError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already registered")
    except PersistError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not store user",
        )


@router.post("/token", response_model=Token)
def login_for_access_token(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
) -> Token:
    """OAuth2 password flow. Website/app Swagger Authorize uses this."""
    return login_or_401(form_data.username, form_data.password)


@router.post("/login", response_model=Token)
def login(body: LoginRequest) -> Token:
    """JSON login for the website and app. Same token as POST /token."""
    return login_or_401(body.username, body.password)


@router.get("/users/me", response_model=UserPublic)
async def read_users_me(
    current_user: Annotated[User, Depends(get_current_active_user)],
) -> UserPublic:
    return current_user.public()


@router.patch("/users/me")
def update_me(
    payload: UserUpdate,
    current_user: Annotated[User, Depends(get_current_active_user)],
):
    from ..database import PersistError, UserExistsError

    if payload.photo is not None:
        if len(payload.photo) > 500_000:
            raise HTTPException(status_code=400, detail="Picture is too large")
        current_user.photo = payload.photo
    if payload.full_name is not None:
        current_user.full_name = payload.full_name.strip() or None
    token = None
    try:
        if payload.username:
            current_user = current_user.rename(payload.username)
            token = current_user.issue_token().access_token
        else:
            current_user.save()
    except UserExistsError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already registered")
    except PersistError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Could not store user")
    body = current_user.public().model_dump()
    if token:
        body["access_token"] = token
        body["token_type"] = "bearer"
    return body
