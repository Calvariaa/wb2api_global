"""使用记录统计：平台全部 Key 的请求数 / Token / 积分 / 耗时聚合，供前端图表使用。

数据源是 usage_logs（由代理网关逐请求写入）。全部聚合在 SQL 侧完成，
不把明细拉到 Python 里再算，避免日志量大时接口变慢。

统计口径：
- 只统计成功请求（error_kind 为空或 'success'）的 token / 积分，
  失败请求若也计进去会虚高消费；
- 请求数则统计全部调用（含失败），用于反映真实负载。
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, case
from sqlalchemy.orm import Session

from admin.db import get_db
from admin.models import Account, ApiKey, UsageLog
from admin.security import require_admin

router = APIRouter(prefix="/api/stats", tags=["stats"])

#: 视为「成功」的 error_kind 取值（空串 = 历史数据未分类，同样按成功计）
_OK_KINDS = ("", "success")


@router.get("/model-health")
def model_health_stats(hours: int = Query(default=24, ge=1, le=168),
                       _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    from admin.health import model_health
    return model_health(db, hours)


def _window(days: int) -> datetime:
    """返回统计窗口起点（UTC，与 usage_logs.created_at 存的口径一致）。"""
    return datetime.utcnow() - timedelta(days=max(1, days))


def _time_filters(days: int, start: Optional[datetime], end: Optional[datetime]):
    """Explicit bounds override the rolling window, using the logs' UTC clock."""
    def utc(value):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value and value.tzinfo else value
    start, end = utc(start), utc(end)
    if start and end and start > end:
        raise HTTPException(400, "开始日期不能晚于结束日期")
    filters = []
    if start is not None:
        filters.append(UsageLog.created_at >= start)
    elif end is None:
        filters.append(UsageLog.created_at >= _window(days))
    if end is not None:
        filters.append(UsageLog.created_at <= end)
    return filters


def _ok_filter(q):
    return q.filter(UsageLog.error_kind.in_(_OK_KINDS))


@router.get("/usage")
def usage_stats(
    days: int = 1,
    granularity: str = "hour",  # day | hour
    key_id: Optional[int] = None,
    model: Optional[str] = None,
    account_id: Optional[int] = None,
    use_case: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """使用记录总览：概览卡片 + 模型分布 + 分组分布 + 端点分布 + 趋势。

    默认 **近 1 天 / 按小时**：这是「今天用得怎么样」最常见的诉求，
    默认 14 天会把当天的问题稀释在两周的曲线里，看不出异常。

    支持按 `key_id` / `model` / `account_id` / `use_case` 筛选：
    筛选后**所有**图与卡片都跟着变（同一份 where 条件），
    这样「某个 Key 今天花了多少」是直接可读的，不用自己心算做减法。

    返回结构直接对应前端「使用记录」页面，避免前端多次请求拼装。
    """
    # ── 统一筛选条件 ────────────────────────────────────────────────────
    # 只构造一次，保证概览/分布/趋势三个口径完全一致 ——
    # 分开写很容易出现「卡片数字和图表对不上」的经典问题。
    filters = _time_filters(days, start, end)
    if key_id is not None:
        filters.append(UsageLog.api_key_id == key_id)
    if model:
        filters.append(UsageLog.model == model)
    if account_id is not None:
        filters.append(UsageLog.account_id == account_id)
    if use_case:
        filters.append(UsageLog.use_case == use_case)

    def _f(q):
        return q.filter(*filters)

    # ── 概览 ────────────────────────────────────────────────────────────
    ov = _ok_filter(
        _f(db.query(
            func.count(UsageLog.id),
            func.coalesce(func.sum(UsageLog.credits), 0.0),
            func.coalesce(func.sum(UsageLog.prompt_tokens), 0),
            func.coalesce(func.sum(UsageLog.completion_tokens), 0),
            func.coalesce(func.sum(UsageLog.total_tokens), 0),
            func.coalesce(func.sum(UsageLog.cached_tokens), 0),
            func.avg(UsageLog.latency_ms),
        ))
    ).one()

    total_req, credits, ptok, ctok, ttok, cached, avg_latency = ov
    # 总请求数按全部调用计（含失败），与图表口径区分
    all_req = _f(db.query(func.count(UsageLog.id))).scalar() or 0
    ok_req = int(total_req or 0)
    fail_req = max(0, int(all_req or 0) - ok_req)

    # ── 模型分布 ────────────────────────────────────────────────────────
    by_model = (
        _ok_filter(
            _f(db.query(
                UsageLog.model,
                func.count(UsageLog.id),
                func.coalesce(func.sum(UsageLog.total_tokens), 0),
                func.coalesce(func.sum(UsageLog.credits), 0.0),
            ))
        )
        .group_by(UsageLog.model)
        .order_by(func.coalesce(func.sum(UsageLog.credits), 0.0).desc())
        .all()
    )

    # ── 端点分布 ────────────────────────────────────────────────────────
    # use_case 由网关写入（如 responses / chat / messages），作为"端点"维度
    by_endpoint = (
        _ok_filter(
            _f(db.query(
                UsageLog.use_case,
                func.count(UsageLog.id),
                func.coalesce(func.sum(UsageLog.total_tokens), 0),
                func.coalesce(func.sum(UsageLog.credits), 0.0),
            ))
        )
        .group_by(UsageLog.use_case)
        .order_by(func.coalesce(func.sum(UsageLog.credits), 0.0).desc())
        .all()
    )

    # ── 按 Key 分组分布（对应参考图的"分组使用分布"）────────────────────
    by_key = (
        _ok_filter(
            _f(db.query(
                UsageLog.api_key_id,
                func.count(UsageLog.id),
                func.coalesce(func.sum(UsageLog.total_tokens), 0),
                func.coalesce(func.sum(UsageLog.credits), 0.0),
            ))
        )
        .group_by(UsageLog.api_key_id)
        .order_by(func.coalesce(func.sum(UsageLog.credits), 0.0).desc())
        .all()
    )
    key_names = {k.id: k.name for k in db.query(ApiKey).all()}
    account_names = {a.id: a.name or a.uid or f"账号 #{a.id}"
                     for a in db.query(Account).all()}
    by_account = (_ok_filter(_f(db.query(
        UsageLog.account_id, func.count(UsageLog.id),
        func.coalesce(func.sum(UsageLog.total_tokens), 0),
        func.coalesce(func.sum(UsageLog.credits), 0.0))))
        .group_by(UsageLog.account_id)
        .order_by(func.count(UsageLog.id).desc()).all())
    by_result = (_f(db.query(UsageLog.error_kind, func.count(UsageLog.id)))
                 .group_by(UsageLog.error_kind).order_by(func.count(UsageLog.id).desc()).all())
    account_models = []
    if account_id is not None:
        from admin.routers.accounts import _active_model_limits
        limits = _active_model_limits(db, datetime.utcnow()).get(account_id, [])
        limit_map = {entry['model']: entry for entry in limits}
        ok = UsageLog.error_kind.in_(_OK_KINDS)
        rows = (_f(db.query(UsageLog.model, func.count(UsageLog.id),
                           func.sum(case((ok, 1), else_=0)),
                           func.sum(case((ok, UsageLog.total_tokens), else_=0)),
                           func.sum(case((ok, UsageLog.credits), else_=0))))
                .group_by(UsageLog.model).order_by(func.count(UsageLog.id).desc()).all())
        for mid, count, successes, tokens, used in rows:
            account_models.append({'model': mid, 'requests': count,
                                   'successes': int(successes or 0),
                                   'failures': count - int(successes or 0),
                                   'tokens': int(tokens or 0), 'credits': round(float(used or 0), 4),
                                   'limit': limit_map.pop(mid, None)})
        # Keep a current limit visible even if this date window has no calls.
        for mid, entry in limit_map.items():
            if not model or model == mid:
                account_models.append({'model': mid, 'requests': 0, 'successes': 0,
                                       'failures': 0, 'tokens': 0, 'credits': 0, 'limit': entry})

    # ── 趋势（按天/按小时分桶）──────────────────────────────────────────
    if db.get_bind().dialect.name == "sqlite":
        bucket = func.strftime("%Y-%m-%d %H:00" if granularity == "hour" else "%Y-%m-%d",
                               UsageLog.created_at)
    elif granularity == "hour":
        bucket = func.date_format(UsageLog.created_at, "%Y-%m-%d %H:00")
    else:
        bucket = func.date_format(UsageLog.created_at, "%Y-%m-%d")
    trend_rows = (
        _ok_filter(
            _f(db.query(
                bucket.label("b"),
                func.coalesce(func.sum(UsageLog.prompt_tokens), 0),
                func.coalesce(func.sum(UsageLog.completion_tokens), 0),
                func.coalesce(func.sum(UsageLog.cached_tokens), 0),
                func.coalesce(func.sum(UsageLog.credits), 0.0),
                func.count(UsageLog.id),
            ))
        )
        .group_by("b")
        .order_by("b")
        .all()
    )

    def _dist(rows, label_fn):
        return [
            {
                "label": label_fn(r[0]),
                "requests": int(r[1] or 0),
                "tokens": int(r[2] or 0),
                "credits": round(float(r[3] or 0), 4),
            }
            for r in rows
        ]

    return {
        "range_days": None if start or end else days,
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "granularity": granularity,
        "filters": {
            "key_id": key_id,
            "model": model,
            "account_id": account_id,
            "use_case": use_case,
        },
        "overview": {
            "requests": int(all_req or 0),
            "ok_requests": ok_req,
            "failed_requests": fail_req,
            "credits": round(float(credits or 0), 4),
            "prompt_tokens": int(ptok or 0),
            "completion_tokens": int(ctok or 0),
            "total_tokens": int(ttok or 0),
            "cached_tokens": int(cached or 0),
            "avg_latency_ms": int(avg_latency) if avg_latency else 0,
        },
        "by_model": _dist(by_model, lambda v: v or "未知"),
        "by_endpoint": _dist(by_endpoint, lambda v: v or "chat"),
        "by_key": _dist(by_key, lambda v: key_names.get(v) or f"#{v}"),
        "by_account": [{**item, "account_id": row[0]} for row, item in zip(
            by_account, _dist(by_account, lambda v: account_names.get(v) or
                             (f"已删除账号 #{v}" if v else "未分配账号")))],
        "by_result": [{"label": "成功" if kind in _OK_KINDS else kind or "未分类",
                       "requests": int(count)} for kind, count in by_result],
        "account_models": account_models,
        "usage_scope": "gateway_only",
        "trend": [
            {
                "bucket": r[0],
                "prompt_tokens": int(r[1] or 0),
                "completion_tokens": int(r[2] or 0),
                "cached_tokens": int(r[3] or 0),
                "credits": round(float(r[4] or 0), 4),
                "requests": int(r[5] or 0),
            }
            for r in trend_rows
        ],
    }


@router.get("/usage/options")
def usage_filter_options(
    days: int = 30,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """筛选下拉的可选项：实际用过的 Key / 模型 / 端点。

    只返回**窗口内真实出现过**的值，避免下拉里塞一堆从没用过的模型，
    让人以为「有数据」却筛出空结果。
    """
    filters = _time_filters(days, start, end)

    models = [
        r[0] for r in
        db.query(UsageLog.model)
        .filter(*filters, UsageLog.model.isnot(None))
        .group_by(UsageLog.model)
        .order_by(func.count(UsageLog.id).desc())
        .all() if r[0]
    ]
    cases = [
        r[0] for r in
        db.query(UsageLog.use_case)
        .filter(*filters, UsageLog.use_case.isnot(None))
        .group_by(UsageLog.use_case)
        .order_by(func.count(UsageLog.id).desc())
        .all() if r[0]
    ]
    keys = [
        {"id": k.id, "name": k.name}
        for k in db.query(ApiKey).order_by(ApiKey.id.asc()).all()
    ]
    account_names = {a.id: a.name or a.uid or f"账号 #{a.id}" for a in db.query(Account).all()}
    accounts = [{"id": aid, "name": account_names.get(aid) or f"已删除账号 #{aid}"}
                for (aid,) in db.query(UsageLog.account_id).filter(*filters, UsageLog.account_id.isnot(None))
                .group_by(UsageLog.account_id).order_by(func.count(UsageLog.id).desc()).all()]
    seen = {a['id'] for a in accounts}
    accounts.extend({'id': aid, 'name': name} for aid, name in account_names.items() if aid not in seen)
    return {"models": models, "use_cases": cases, "keys": keys, "accounts": accounts}
