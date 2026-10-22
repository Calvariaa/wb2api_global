"""Daily discovery through the official free-benefit endpoints we support."""
from datetime import datetime, timedelta

from admin import compensation
from admin.models import Schedule

TASK = 'claim_benefits'


def ensure_schedule(db):
    current = db.query(Schedule).filter(Schedule.task == TASK).order_by(Schedule.id).first()
    legacy = db.query(Schedule).filter(Schedule.task == compensation.TASK).order_by(Schedule.id).all()
    if current is None:
        current = legacy.pop(0) if legacy else Schedule(enabled=1)
        current.name = '每日检查并领取权益'
        current.task = TASK
        current.interval_minutes = 1440
        current.next_run_at = datetime.utcnow() + timedelta(seconds=45)
        current.stop_after = None
        db.add(current)
    # Retire the old one-campaign schedules, including duplicates from upgrades.
    for schedule in legacy:
        db.delete(schedule)
    db.commit()
    return current


def run_scheduled(db, schedule=None):
    # The task survives a campaign's expiry. The discovery endpoint supplies
    # each offer's current amount, deadline and per-account claim status.
    return compensation.run_batch(db, source='schedule', discover=True)
