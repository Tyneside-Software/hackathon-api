"""Shop accounts in Firestore so they survive Cloud Run deploys.

Falls back to None when Firestore is missing (local SQLite still works).
"""
from __future__ import annotations

from .config import log
from .database import utc_now

_USERS = "katie_users"
_FRIENDS = "katie_friends"
_ADMINS = "katie_admins"
_PROFILES = "katie_profiles"

_client = None
_tried = False


def client():
    global _client, _tried
    if _tried:
        return _client
    _tried = True
    try:
        from google.cloud import firestore

        _client = firestore.Client()
        _client.collection(_USERS).document("_ping").get()
    except Exception as exc:
        log.warning("Firestore accounts off; SQLite only: %s", exc)
        _client = None
    return _client


def enabled() -> bool:
    return client() is not None


def _db():
    return client()


def user_doc(username: str) -> dict | None:
    db = _db()
    if db is None:
        return None
    snap = db.collection(_USERS).document(username).get()
    if not snap.exists:
        return None
    data = snap.to_dict() or {}
    data["username"] = username
    return data


def user_by_email(email: str) -> dict | None:
    db = _db()
    if db is None:
        return None
    hits = list(db.collection(_USERS).where("email", "==", email).limit(1).stream())
    if not hits:
        return None
    data = hits[0].to_dict() or {}
    data["username"] = hits[0].id
    return data


def put_user(data: dict, *, create_only: bool = False) -> None:
    db = _db()
    if db is None:
        return
    key = data["username"]
    ref = db.collection(_USERS).document(key)
    if create_only and ref.get().exists:
        from .database import UserExistsError

        raise UserExistsError("Username already registered")
    payload = {
        "username": key,
        "email": data.get("email"),
        "full_name": data.get("full_name"),
        "disabled": bool(data.get("disabled") or False),
        "hashed_password": data.get("hashed_password") or "",
        "photo": data.get("photo"),
        "updated_at": data.get("updated_at") or utc_now(),
    }
    ref.set(payload)


def delete_user(username: str) -> None:
    db = _db()
    if db is None:
        return
    db.collection(_USERS).document(username).delete()


def list_user_summaries() -> list[dict]:
    db = _db()
    if db is None:
        return []
    out = []
    query = db.collection(_USERS).select(
        ["username", "email", "full_name", "disabled", "hashed_password", "updated_at"]
    )
    for snap in query.stream():
        data = snap.to_dict() or {}
        data["username"] = data.get("username") or snap.id
        data["photo"] = None
        out.append(data)
    return out


def hydrate_photos(rows: list[dict]) -> list[dict]:
    db = _db()
    if db is None or not rows:
        return rows
    for row in rows:
        full = user_doc(row["username"])
        if full:
            row["photo"] = full.get("photo")
            row["hashed_password"] = full.get("hashed_password") or row.get("hashed_password")
    return rows


def search_users(needle: str, *, exclude: str | None = None, limit: int = 8) -> list[dict]:
    needle = (needle or "").strip().lower()
    exclude = (exclude or "").strip().lower() or None
    rows = []
    for data in list_user_summaries():
        name = (data.get("username") or "").lower()
        if data.get("disabled"):
            continue
        if exclude and name == exclude:
            continue
        if needle and needle not in name:
            continue
        rows.append(data)
    rows.sort(key=lambda d: (not (d.get("username") or "").startswith(needle), d.get("username") or ""))
    return hydrate_photos(rows[:limit])


def friend_id(user: str, friend: str) -> str:
    return f"{user}__{friend}"


def add_friend(user: str, friend: str) -> None:
    db = _db()
    if db is None:
        return
    db.collection(_FRIENDS).document(friend_id(user, friend)).set(
        {"user": user, "friend": friend, "created_at": utc_now()}
    )


def remove_friend(user: str, friend: str) -> None:
    db = _db()
    if db is None:
        return
    db.collection(_FRIENDS).document(friend_id(user, friend)).delete()


def friends_of(user: str) -> set[str]:
    db = _db()
    if db is None:
        return set()
    names = set()
    for snap in db.collection(_FRIENDS).where("user", "==", user).stream():
        data = snap.to_dict() or {}
        if data.get("friend"):
            names.add(data["friend"])
    return names


def retarget_friends(old: str, new: str) -> None:
    db = _db()
    if db is None:
        return
    for snap in db.collection(_FRIENDS).where("user", "==", old).stream():
        data = snap.to_dict() or {}
        friend = data.get("friend")
        if friend:
            add_friend(new, friend)
        snap.reference.delete()
    for snap in db.collection(_FRIENDS).where("friend", "==", old).stream():
        data = snap.to_dict() or {}
        user = data.get("user")
        if user:
            add_friend(user, new)
        snap.reference.delete()


def admin_has(username: str) -> bool:
    db = _db()
    if db is None:
        return False
    return db.collection(_ADMINS).document(username).get().exists


def admin_usernames() -> set[str]:
    db = _db()
    if db is None:
        return set()
    return {snap.id for snap in db.collection(_ADMINS).stream()}


def admin_ensure(username: str, *, founder: bool = False) -> None:
    db = _db()
    if db is None:
        return
    ref = db.collection(_ADMINS).document(username)
    snap = ref.get()
    if not snap.exists:
        ref.set({"username": username, "founder": founder, "created_at": utc_now()})
    elif founder:
        data = snap.to_dict() or {}
        if not data.get("founder"):
            ref.update({"founder": True})


def admin_add(username: str) -> None:
    admin_ensure(username, founder=False)


def admin_remove(username: str) -> str | None:
    db = _db()
    if db is None:
        return None
    ref = db.collection(_ADMINS).document(username)
    snap = ref.get()
    if not snap.exists:
        return None
    data = snap.to_dict() or {}
    if data.get("founder"):
        return "founder"
    others = [s.id for s in db.collection(_ADMINS).stream() if s.id != username]
    if not others:
        return "last"
    ref.delete()
    return None


def admin_people() -> list[dict]:
    db = _db()
    if db is None:
        return []
    rows = []
    for snap in db.collection(_ADMINS).stream():
        data = snap.to_dict() or {}
        rows.append({"username": snap.id, "founder": bool(data.get("founder"))})
    rows.sort(key=lambda r: r["username"])
    return rows


def retarget_admin(old: str, new: str) -> None:
    db = _db()
    if db is None:
        return
    ref = db.collection(_ADMINS).document(old)
    snap = ref.get()
    if not snap.exists:
        return
    data = snap.to_dict() or {}
    db.collection(_ADMINS).document(new).set(
        {
            "username": new,
            "founder": bool(data.get("founder")),
            "created_at": data.get("created_at") or utc_now(),
        }
    )
    ref.delete()


def put_profile(username: str, photo: str | None, full_name: str | None = None) -> None:
    db = _db()
    if db is None:
        return
    db.collection(_PROFILES).document(username).set(
        {
            "username": username,
            "photo": photo or "",
            "full_name": full_name or "",
            "updated_at": utc_now(),
        }
    )


def list_profiles() -> list[dict]:
    db = _db()
    if db is None:
        return []
    out = []
    for snap in db.collection(_PROFILES).stream():
        data = snap.to_dict() or {}
        name = data.get("username") or snap.id
        out.append(
            {
                "username": name,
                "photo": data.get("photo") or "",
                "full_name": data.get("full_name") or None,
            }
        )
    out.sort(key=lambda r: r["username"])
    return out
