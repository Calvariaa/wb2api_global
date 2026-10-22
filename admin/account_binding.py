"""Account ownership shared by key management and every gateway selection path."""
from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import aliased

from admin.models import Account, ApiKey


def scope_accounts(query, account_id=None, key_id=None):
    """Apply ownership before health, sticky routing or fallback selection."""
    if account_id:
        owner = select(ApiKey.id).where(
            ApiKey.id == key_id, ApiKey.account_id == account_id,
            ApiKey.status == "active").exists()
        return query.filter(Account.id == account_id, owner)
    reserved = aliased(Account)
    owned = select(ApiKey.id).select_from(ApiKey).outerjoin(
        reserved, reserved.id == ApiKey.account_id).where(
            ApiKey.account_id.is_not(None),
            or_(ApiKey.account_id == Account.id,
                (reserved.uid != "") & (reserved.uid == Account.uid))).exists()
    return query.filter(~owned)


def validate_binding(db, account_id, key_id=None):
    if account_id in (None, 0, "0", ""):
        return None
    try:
        aid = int(account_id)
        if isinstance(account_id, bool) or str(aid) != str(account_id) or aid < 1:
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise HTTPException(400, "账号 ID 必须是正整数或 0（不绑定）") from None
    account = db.query(Account).filter(Account.id == aid).first()
    if not account:
        raise HTTPException(400, f"账号 #{aid} 不存在")
    # Serialize reservations even when legacy rows share the same upstream UID.
    peers = db.query(Account).filter(
        or_(Account.id == aid, (Account.uid == account.uid) & (Account.uid != ""))
    ).order_by(Account.id).with_for_update().all()
    if not any(a.id == aid for a in peers):
        raise HTTPException(400, f"账号 #{aid} 不存在")
    conflict = db.query(ApiKey).filter(ApiKey.account_id.in_([a.id for a in peers]))
    if key_id:
        conflict = conflict.filter(ApiKey.id != key_id)
    if conflict.with_for_update().first():
        raise HTTPException(409, "该账号已被其他 API Key 独占绑定，请先解除原绑定")
    return aid


def binding_owners(db):
    """Map duplicate account records to the same visible reservation."""
    rows = db.query(Account).all()
    keys = {k.account_id: k for k in db.query(ApiKey).filter(ApiKey.account_id.is_not(None)).all()}
    by_uid = {a.uid: keys[a.id] for a in rows if a.uid and a.id in keys}
    return {a.id: keys.get(a.id) or by_uid.get(a.uid) for a in rows}
