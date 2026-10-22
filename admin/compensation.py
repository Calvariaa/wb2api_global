"""Time-limited DeepSeek compensation with persisted claims and shared leases."""
from datetime import datetime, timedelta, timezone
import hashlib
import time

from admin import maintenance
from admin.backend import AccountSession
from admin.growth_center import identity, record_result
from admin.growth_plan import _Lease, _read, _write
from admin.models import Account

CAMPAIGN = 'deepseek-202610'
TITLE = 'DeepSeek 模型服务异常额外补偿'
CREDIT = 500
TASK = 'claim_compensation'
DAY_TZ = timezone(timedelta(hours=8))
END = datetime(2026, 10, 22, 23, 59, 59, tzinfo=DAY_TZ)
RECHECK_SECONDS = 600


def campaign():
    return {'id': CAMPAIGN, 'title': TITLE, 'credit': CREDIT,
            'end_time': END.isoformat(), 'expired': _now() >= END,
            'official_url': 'https://www.workbuddy.cn/profile/plans-usage?action=addon'}


def _now():
    return datetime.now(DAY_TZ)


def _key(account, offer=None):
    if offer:
        deadline = _deadline(offer.get('end_time'), limited=False)
        amount = offer.get('credit_num')
        if deadline != END or amount != CREDIT:
            signature = f'{deadline.isoformat() if deadline else "unknown"}:{amount}'
            campaign_id = hashlib.sha256(signature.encode()).hexdigest()[:24]
            return 'benefits:meter:' + campaign_id + ':' + identity(account)
    return 'compensation:' + CAMPAIGN + ':' + identity(account)


def cached(db, account, *, key=None, limited=True):
    state = _read(db, key or _key(account))
    if state.get('claimed'):
        return {**state, 'status': 'already', 'reason': '本期权益已领取', 'credit': 0, 'ok': True}
    if limited and _now() >= END:
        return {**state, 'status': 'expired', 'reason': '已超过领取截止时间', 'credit': 0, 'ok': True}
    return state or {'status': 'unknown', 'reason': '尚未查询', 'credit': 0}


def _deadline(value, *, limited=True):
    if not isinstance(value, str) or not value:
        return END if limited else None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        deadline = parsed.replace(tzinfo=DAY_TZ) if parsed.tzinfo is None else parsed.astimezone(DAY_TZ)
        return min(END, deadline) if limited else deadline
    except ValueError:
        return END if limited else None


def _status(result, *, discover=False):
    if not result.get('ok'):
        return {'status': 'error', 'ok': False, 'reason': '补偿状态查询失败，请稍后重试'}
    data = result.get('data') or {}
    common = {key: data[key] for key in ('active', 'claimed', 'credit_num', 'validity_period', 'end_time') if key in data}
    if discover:
        amount = data.get('credit_num')
        deadline = _deadline(data.get('end_time'), limited=False)
        if data.get('active') is False:
            return {**common, 'status':'ineligible', 'ok':True, 'reason':'当前暂无可领取权益'}
        if type(amount) is not int or amount <= 0 or deadline is None:
            return {**common, 'status':'unknown', 'ok':False, 'reason':'权益金额或截止时间不明确，未发起领取'}
        if data.get('claimed') is True:
            return {**common, 'status':'already', 'ok':True, 'reason':'本期权益已领取'}
        if _now() >= deadline:
            return {**common, 'status':'expired', 'ok':True, 'reason':'本期权益已截止'}
        if data.get('active') is True and data.get('claimed') is False:
            return {**common, 'status':'available', 'ok':True, 'reason':f'发现可领取权益 {amount} 积分'}
        return {**common, 'status':'unknown', 'ok':False, 'reason':'权益状态不明确，未发起领取'}
    if data.get('claimed') is True:
        return {**common, 'status': 'already', 'ok': True, 'reason': '该账号已领取补偿'}
    if _now() >= _deadline(data.get('end_time')):
        return {**common, 'status': 'expired', 'ok': True, 'reason': '补偿活动已截止'}
    if data.get('active') is False:
        return {**common, 'status': 'ineligible', 'ok': True, 'reason': '活动未开启或该账号暂无领取资格'}
    if data.get('active') is True and data.get('claimed') is False and data.get('credit_num') == CREDIT:
        return {**common, 'status': 'available', 'ok': True, 'reason': '可领取 500 积分补偿'}
    return {**common, 'status': 'unknown', 'ok': False, 'reason': '活动信息不完整或与本期补偿不符，未发起领取'}


def _refresh_resources(account, session, result):
    try:
        from admin.routers.accounts import _sync_credit_snapshot
        balance = session.fetch_balance()
        account.balance_total = int(balance.get('total') or 0)
        account.balance_remain = int(balance.get('remain') or 0)
        result['balance_after'] = account.balance_remain
        account.last_sync_at = datetime.utcnow()
        summary = _sync_credit_snapshot(account, session.fetch_credit_details())
        collection = summary.get('collection') or {}
        result['resources_refreshed'] = collection.get('complete', True) is True
    except Exception:
        result['resources_refreshed'] = False
    if not result['resources_refreshed']:
        result['reason'] += '；余额或到期快照尚未完整刷新，下次自动重试'


def process_account(db, account, *, claim=True, source='manual', discover=False):
    """Check before claiming; serialize same-UID manual, batch and scheduled work."""
    previous = {} if discover else cached(db, account)
    if previous.get('status') == 'expired' or (previous.get('claimed') and previous.get('resources_refreshed')):
        return previous
    with maintenance.account_slot(account, db) as blocked:
        if blocked:
            return {'status': 'deferred', 'ok': False, 'credit': 0,
                    'reason': maintenance.skip_reason_text(blocked, '已顺延'), 'skip_reason': blocked}
        key = _key(account)
        lease = _Lease(db, key + ':lease')
        if not lease.acquire():
            return {'status': 'deferred', 'ok': False, 'credit': 0, 'reason': '该账号权益正在处理中，请稍后查看'}
        try:
            previous = {} if discover else cached(db, account)
            if previous.get('status') == 'expired' or (previous.get('claimed') and previous.get('resources_refreshed')):
                return previous
            original = account.auth_json
            with AccountSession(original) as session:
                try:
                    # A failed resource refresh retries reads only, even after expiry.
                    result = dict(previous) if previous.get('claimed') else _status(session.compensation_status(), discover=discover)
                    if discover:
                        # Always probe upstream first: yesterday's claimed offer may
                        # have been replaced by a new reward, amount or deadline.
                        key = _key(account, result)
                        previous = cached(db, account, key=key, limited=False)
                        if previous.get('claimed'):
                            result = dict(previous)
                            if previous.get('resources_refreshed'):
                                return result
                    result['credit'] = 0
                    result['checked_at'] = _now().isoformat(timespec='seconds')
                    # Preserve unknown attempts across refreshes/restarts. A successful
                    # status read alone must not remove the mutation's retry delay.
                    attempted = float(previous.get('attempt_at') or 0)
                    if attempted:
                        result['attempt_at'] = attempted
                    if claim and result['status'] == 'available':
                        amount = result['credit_num'] if discover else CREDIT
                        if _now() >= _deadline(result.get('end_time'), limited=not discover):
                            result.update(status='expired', ok=True, reason='补偿活动已截止')
                        elif attempted and time.time() - attempted < RECHECK_SECONDS:
                            result.update(status='pending', ok=False,
                                          reason='上次领取结果待确认，10 分钟内只复查状态，不重复领取')
                        else:
                            result.update(status='pending', ok=False, attempt_at=time.time(),
                                          reason='领取请求已提交，等待确认')
                            _write(db, key, result)  # Persist before the non-idempotent call.
                            try:
                                response = session.claim_compensation()
                            except Exception:
                                response = {'ok': False}
                            if response.get('ok'):
                                result.update(status='claimed', claimed=True, ok=True, credit=amount,
                                              reason=f'已领取 {amount} 积分权益', claimed_at=_now().isoformat(timespec='seconds'))
                            else:
                                # Lost responses can follow a successful credit grant.
                                # Re-read server state before ever attempting another POST.
                                try:
                                    checked = _status(session.compensation_status(), discover=discover)
                                except Exception:
                                    checked = {}
                                if checked.get('claimed') is True and (not discover or _key(account, checked) == key):
                                    result.update(status='claimed', claimed=True, ok=True, credit=amount,
                                                  reason='已通过官方状态确认领取成功', claimed_at=_now().isoformat(timespec='seconds'))
                                else:
                                    result['reason'] = '领取未确认，稍后复查官方状态；不会立即重复提交'
                    if result.get('claimed'):
                        # Save success before refreshing resources. A failed balance
                        # request must never cause a second compensation grant attempt.
                        _write(db, key, result, commit=False)
                        reward_record = None
                        if int(result.get('credit') or 0) > 0:
                            result['credit_event_key'] = 'benefit:' + hashlib.sha256(key.encode()).hexdigest()
                            reward_record = record_result(db, account.id, result, source=source, action='权益获取' if discover else TITLE)
                        else:
                            db.commit()
                        _refresh_resources(account, session, result)
                        if reward_record and result.get('balance_after') is not None:
                            from admin.credit_journal import attach_balance
                            attach_balance(db, [reward_record], result['balance_after'])
                    _write(db, key, result)
                    return result
                finally:
                    # Do not overwrite a new login completed during the upstream call.
                    updated = session.updated_json()
                    db.query(Account).filter(Account.id == account.id, Account.auth_json == original).update(
                        {Account.auth_json: updated}, synchronize_session=False)
                    db.commit()
        except Exception:
            db.rollback()
            # Never expose upstream bodies or credential-bearing exception text.
            return {'status': 'error', 'ok': False, 'credit': 0,
                    'reason': '权益请求失败，请稍后重试；已提交的领取会先核对状态'}
        finally:
            try:
                lease.release()
            except Exception:
                db.rollback()  # Lease expires on its own if the database is unavailable.


def run_batch(db, ids=None, *, job=None, source='schedule', discover=False):
    from admin.scheduler import _account_label
    query = db.query(Account).filter(Account.status == 'active')
    if ids is not None:
        query = query.filter(Account.id.in_(ids))
    accounts = query.order_by(Account.id).all()
    detail = []
    seen = set()
    for account in accounts:
        account_id, label = account.id, _account_label(account)
        if job:
            job.beat(label)
        account_identity = identity(account)
        if account_identity in seen:
            result = {'ok': True, 'status': 'duplicate', 'credit': 0, 'reason': '同 UID 账号本轮已处理'}
        else:
            seen.add(account_identity)
            result = process_account(db, account, source=source, discover=discover)
        item = {**result, 'account_id': account_id, 'account': label}
        detail.append(item)
        if job:
            job.add_item(item)
    claimed = sum(item['status'] == 'claimed' for item in detail)
    already = sum(item['status'] == 'already' for item in detail)
    credit = sum(item.get('credit', 0) for item in detail)
    deferred = sum(item['status'] in ('deferred', 'pending') for item in detail)
    failed = sum(item['status'] in ('error', 'unknown') for item in detail)
    skipped = sum(item['status'] in ('ineligible', 'expired', 'duplicate') for item in detail)
    return {'task': 'claim_benefits' if discover else TASK, 'ok': failed == 0, 'total': len(detail), 'claimed': claimed,
            'already': already, 'credit': credit, 'deferred': deferred, 'failed': failed, 'skipped': skipped,
            'detail': detail, 'summary': f'共 {len(detail)} 个账号：领取成功 {claimed} 个，失败 {failed} 个，共计领取 {credit} 积分；已领取 {already} 个，顺延/待确认 {deferred} 个，其他跳过 {skipped} 个'}
