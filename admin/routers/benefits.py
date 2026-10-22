"""Admin-only compensation status, single claims and asynchronous batches."""
import threading

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, validator
from sqlalchemy.orm import Session

from admin import compensation, jobrunner
from admin.db import SessionLocal, get_db
from admin.models import Account
from admin.security import require_admin

router = APIRouter(prefix='/api/benefits', tags=['benefits'], dependencies=[Depends(require_admin)])
_start_lock = threading.Lock()
JOB_KEY = 'compensation-claim'


class ClaimIn(BaseModel):
    confirm: bool = False


class BatchIn(ClaimIn):
    ids: list[int] | None = None

    @validator('ids')
    def limit_ids(cls, value):
        # Android uses Pydantic 1, where max_length is invalid for lists.
        if value is not None and len(value) > 5000:
            raise ValueError('一次最多处理 5000 个账号')
        return value


@router.get('/compensation')
def overview(db: Session = Depends(get_db)):
    from admin.scheduler import _account_label
    accounts = db.query(Account).order_by(Account.id).all()
    return {'campaign': compensation.campaign(),
            'items': [{'id': account.id, 'name': _account_label(account), 'enabled': account.status == 'active',
                       **compensation.cached(db, account)} for account in accounts]}


def _account(db, account_id):
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(404, '账号不存在')
    return account


@router.post('/compensation/accounts/{account_id}/refresh')
def refresh(account_id: int, db: Session = Depends(get_db)):
    return compensation.process_account(db, _account(db, account_id), claim=False)


@router.post('/compensation/accounts/{account_id}/claim')
def claim(account_id: int, body: ClaimIn, db: Session = Depends(get_db)):
    if not body.confirm:
        raise HTTPException(400, '请确认领取本期补偿权益')
    return compensation.process_account(db, _account(db, account_id))


@router.post('/compensation/batch')
def batch(body: BatchIn, db: Session = Depends(get_db)):
    if not body.confirm:
        raise HTTPException(400, '请确认批量领取本期补偿权益')
    with _start_lock:
        running = jobrunner.RUNNER.get(JOB_KEY)
        if running and running.status == 'running':
            return {'reused': True, **running.snapshot()}
        if compensation.campaign()['expired']:
            raise HTTPException(400, '补偿活动已截止')
        query = db.query(Account).filter(Account.status == 'active')
        if body.ids is not None:
            query = query.filter(Account.id.in_(body.ids))
        ids = [account.id for account in query.order_by(Account.id).all()]
        if not ids:
            raise HTTPException(400, '请选择至少一个启用账号')

        def worker(job):
            with SessionLocal() as worker_db:
                job.set_phase('核对并领取补偿权益')
                return compensation.run_batch(worker_db, ids, job=job, source='manual')

        job = jobrunner.RUNNER.start(JOB_KEY, len(ids), worker, title='DeepSeek 补偿权益批量领取')
        return job.snapshot()
