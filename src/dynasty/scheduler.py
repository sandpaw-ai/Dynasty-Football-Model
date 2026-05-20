"""APScheduler-based job runner.

Run with: `python -m dynasty.cli run-scheduler`

Default schedule:
    - Sleeper player map:  Mondays 05:00 (weekly)
    - Daily sources:       every day 05:15
    - Weekly sources:      Mondays 06:00
"""
from __future__ import annotations
import logging
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .sync import sync_source, sync_sleeper_players
from .sources import REGISTRY

log = logging.getLogger(__name__)


def build_scheduler() -> BlockingScheduler:
    sched = BlockingScheduler()

    # Sleeper player map — weekly, ahead of other sources so IDs are fresh
    sched.add_job(
        sync_sleeper_players,
        CronTrigger(day_of_week="mon", hour=5, minute=0),
        id="sync_sleeper_players",
        replace_existing=True,
    )

    for slug, cls in REGISTRY.items():
        if slug == "sleeper_players":
            continue
        freq = cls.update_frequency
        if freq == "daily":
            trigger = CronTrigger(hour=5, minute=15)
        elif freq == "weekly":
            trigger = CronTrigger(day_of_week="mon", hour=6, minute=0)
        else:
            continue  # event-driven sources are not scheduled
        sched.add_job(
            sync_source, trigger,
            args=[slug], id=f"sync_{slug}", replace_existing=True,
        )

    return sched


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = build_scheduler()
    log.info("Scheduler starting. Jobs:")
    for j in s.get_jobs():
        log.info("  %s: %s", j.id, j.trigger)
    s.start()
