"""Canary health check: compare the run failure rate after a switch with the baseline.

Reads ``system.lakeflow.job_run_timeline``. A run can span several rows; the
row that ends the run carries ``result_state``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

FAILED_STATES = ("FAILED", "ERROR", "TIMED_OUT")


def failure_rate_sql(job_ids: list[str]) -> str:
    """Runs and failures for the given jobs in two windows: baseline and since the switch."""
    if not job_ids or not all(str(j).isdigit() for j in job_ids):
        raise ValueError("job_ids must be a non-empty list of numeric job ids")
    ids = ", ".join(f"'{j}'" for j in job_ids)
    failed = ", ".join(f"'{s}'" for s in FAILED_STATES)
    return f"""
SELECT
  CASE WHEN period_end_time >= :switch_time THEN 'canary' ELSE 'baseline' END AS window,
  COUNT(DISTINCT run_id) AS runs,
  COUNT(DISTINCT CASE WHEN result_state IN ({failed}) THEN run_id END) AS failed_runs
FROM system.lakeflow.job_run_timeline
WHERE job_id IN ({ids})
  AND result_state IS NOT NULL
  AND period_end_time >= :switch_time - make_interval(0, 0, 0, :baseline_days)
GROUP BY 1
""".strip()


@dataclass
class HealthResult:
    healthy: bool | None  # None = not enough runs yet
    canary_runs: int
    canary_failed: int
    baseline_runs: int
    baseline_failed: int
    message: str


def judge(canary_runs: int, canary_failed: int, baseline_runs: int, baseline_failed: int,
          min_runs: int = 20, max_increase: float = 0.05) -> HealthResult:
    """Healthy when the canary failure rate is at most baseline + ``max_increase``."""
    if canary_runs < min_runs:
        return HealthResult(None, canary_runs, canary_failed, baseline_runs, baseline_failed,
                            f"only {canary_runs} canary runs so far; {min_runs} are needed")
    canary_rate = canary_failed / canary_runs
    baseline_rate = baseline_failed / baseline_runs if baseline_runs else 0.0
    healthy = canary_rate <= baseline_rate + max_increase
    return HealthResult(healthy, canary_runs, canary_failed, baseline_runs, baseline_failed,
                        f"canary failure rate {canary_rate:.1%}, baseline {baseline_rate:.1%}, "
                        f"allowed increase {max_increase:.0%}")


def check(spark, job_ids: list[str], switch_time: datetime, baseline_days: int = 14,
          min_runs: int = 20, max_increase: float = 0.05) -> HealthResult:
    rows = spark.sql(failure_rate_sql(job_ids),
                     args={"switch_time": switch_time, "baseline_days": baseline_days}).collect()
    stats = {r["window"]: (r["runs"], r["failed_runs"]) for r in rows}
    canary, baseline = stats.get("canary", (0, 0)), stats.get("baseline", (0, 0))
    return judge(canary[0], canary[1], baseline[0], baseline[1], min_runs, max_increase)
