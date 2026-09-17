"""Authentication routes — OAuth2 password flow + register."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from ..db import PersistError, UserExistsError
from ..dependencies import get_current_active_user
from ..models import User
from ..schemas import LoginRequest, Token, User as UserPublic, UserCreate

router = APIRouter()


def login_or_401(username: str, password: str) -> Token:
    user = User.authenticate(username, password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if user.disabled:
        raise HTTPException(status_code=400, detail="Inactive user")
    return user.issue_token()


@router.post("/register", response_model=UserPublic, status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate) -> UserPublic:
    if User.get(payload.username):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already registered")
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
