"""Authenticated upgrade guidance and online SQLite backup; no host execution."""
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from sqlalchemy.orm import Session

from admin.db import engine, get_db
from admin.models import Account, CreditEvent
from admin.security import require_admin
from admin.routers.releases import service
from desktop.common import VERSION, RELEASES_URL

router = APIRouter(prefix="/api/maintenance", tags=["maintenance"], dependencies=[Depends(require_admin)])


@router.get("/update")
def update_info():
    from packaging.version import Version, InvalidVersion
    snapshot = service().check()
    candidates = []
    for release in snapshot.get("releases", []):
        try:
            version = Version(release["tag"].lstrip("vV"))
            if not version.is_prerelease and re.fullmatch(r"[vV]?\d+\.\d+\.\d+", release["tag"]):
                candidates.append((version, release))
        except (InvalidVersion, KeyError, TypeError):
            continue
    latest = max(candidates, key=lambda item: item[0], default=(None, None))[1]
    tag = latest["tag"] if latest else None
    commands = None
    if tag:
        commands = {
            "docker": f"git status --short\ngit fetch origin --tags\ngit checkout {tag}\ndocker compose --env-file .env.docker up -d --build admin\ndocker compose --env-file .env.docker logs --tail=100 admin",
            "linux": f"git status --short\ngit fetch origin --tags\ngit checkout {tag}\npython -m pip install -r requirements.txt\n# 使用当前部署对应的 systemd / supervisor / 启动脚本重启服务",
        }
    return {"version": VERSION, "latest": {k: latest.get(k) for k in ("tag", "name", "url", "date")} if latest else None,
            "available": bool(latest and Version(tag.lstrip("vV")) > Version(VERSION)),
            "error": snapshot.get("error", ""), "releases_url": RELEASES_URL,
            "database": engine.dialect.name, "backup_supported": engine.dialect.name == "sqlite",
            "commands": commands,
            "backup_note": "备份含账号凭据和 API Key，请私密保存。升级前先备份；确认 git status 无本地改动后，在项目目录执行命令。NAS 可使用容器管理器重新构建并重启，保留 /data 数据卷。",
            "mysql_backup_command": "mysqldump --single-transaction --routines --triggers -h <数据库主机> -u <数据库用户> -p <数据库名> > workbuddy-backup.sql" if engine.dialect.name != "sqlite" else None}


@router.post("/backup")
def backup():
    if engine.dialect.name != "sqlite":
        raise HTTPException(409, "当前数据库不支持页面在线备份，请使用数据库管理工具或 mysqldump 完成一致性备份")
    fd, name = tempfile.mkstemp(prefix="workbuddy-backup-", suffix=".db")
    os.close(fd)
    path = Path(name)
    try:
        with engine.connect() as source:
            raw = source.connection.driver_connection
            with closing(sqlite3.connect(name)) as target:
                raw.backup(target, pages=256, sleep=0.01)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("backup integrity check failed")
        filename = "workbuddy-backup-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + ".db"
        return FileResponse(path, media_type="application/octet-stream", filename=filename,
                            headers={"Cache-Control": "no-store"}, background=BackgroundTask(path.unlink, missing_ok=True))
    except Exception:
        path.unlink(missing_ok=True)
        raise


@router.get("/credit-events")
def credit_events(account_id: int | None = None, source: str = "", kind: str = "",
                  start: datetime | None = None, end: datetime | None = None,
                  page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
                  db: Session = Depends(get_db)):
    from admin.credit_journal import backfill
    importing = backfill(db)
    query = db.query(CreditEvent)
    if account_id is not None:
        query = query.filter(CreditEvent.account_id == account_id)
    if source:
        query = query.filter(CreditEvent.source == source)
    if kind:
        if kind not in ("confirmed", "observed"):
            raise HTTPException(400, "记录类型无效")
        query = query.filter(CreditEvent.kind == kind)
    def utc(value):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    if start:
        query = query.filter(CreditEvent.created_at >= utc(start))
    if end:
        query = query.filter(CreditEvent.created_at <= utc(end))
    if start and end and utc(start) > utc(end):
        raise HTTPException(400, "开始时间不能晚于结束时间")
    total = query.count()
    rows = query.order_by(CreditEvent.created_at.desc(), CreditEvent.id.desc()).offset((page-1)*page_size).limit(page_size).all()
    historical = db.query(CreditEvent.account_id, CreditEvent.account_name).distinct().all()
    account_names = {aid: name for aid, name in historical}
    account_names.update({aid: name for aid, name in db.query(Account.id, Account.name)})
    return {"items": [{"id": r.id, "account_id": r.account_id, "account_name": r.account_name,
                        "kind": r.kind, "source": r.source, "delta": r.delta, "balance_after": r.balance_after,
                        "created_at": r.created_at.isoformat() + "Z"} for r in rows],
            "total": total, "page": page, "page_size": page_size, "importing": importing,
            "sources": [s for (s,) in db.query(CreditEvent.source).distinct().order_by(CreditEvent.source)],
            "accounts": [{"id": aid, "name": name} for aid, name in sorted(account_names.items())]}
