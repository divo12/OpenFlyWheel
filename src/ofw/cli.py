"""Dependency-free operator CLI over the scheduler application service."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from ofw.scheduler import (
    JobId,
    LocalScheduler,
    ScheduledJob,
    SchedulerError,
    SchedulerErrorCode,
    read_automation_policy,
)


class CampaignCommand(StrEnum):
    STATUS = "status"
    CANCEL = "cancel"
    RESUME = "resume"


def run_campaign_command(arguments: tuple[str, ...], now: datetime) -> ScheduledJob:
    if len(arguments) != 5 or arguments[0] != "campaign":
        raise SchedulerError(
            SchedulerErrorCode.INVALID_TRANSITION,
            "usage: ofw campaign status|cancel|resume STORE POLICY JOB_ID",
        )
    try:
        command = CampaignCommand(arguments[1])
    except ValueError as error:
        raise SchedulerError(
            SchedulerErrorCode.INVALID_TRANSITION,
            arguments[1],
        ) from error
    store_path = Path(arguments[2])
    policy = read_automation_policy(Path(arguments[3]))
    job_id = JobId(arguments[4])
    scheduler = LocalScheduler(store_path, policy)
    try:
        match command:
            case CampaignCommand.STATUS:
                return scheduler.job(job_id)
            case CampaignCommand.CANCEL:
                return scheduler.cancel(job_id, now)
            case CampaignCommand.RESUME:
                return scheduler.resume(job_id, now)
    finally:
        scheduler.close()


def main() -> int:
    try:
        result = run_campaign_command(tuple(sys.argv[1:]), datetime.now(UTC))
    except SchedulerError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(result.to_json())
    return 0
