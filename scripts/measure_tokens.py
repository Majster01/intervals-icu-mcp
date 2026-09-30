"""Measure how much the `fields` filter shrinks tool responses.

Runs selected tools against synthetic (respx-mocked) API payloads with and
without representative `fields`, and prints each response's size. Also reports
the extra tool-list schema cost of the `fields` parameter. The intervals.icu
API is never touched.

Token counts default to a chars/3.5 estimate. With ``--exact`` and
ANTHROPIC_API_KEY set, they come from the Anthropic token-counting endpoint.

Usage:
    uv run python scripts/measure_tokens.py
    ANTHROPIC_API_KEY=... uv run python scripts/measure_tokens.py --exact

NOT part of `make test`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import respx
from httpx import Response

from intervals_icu_mcp.auth import ICUConfig
from intervals_icu_mcp.server import mcp
from intervals_icu_mcp.tools.activities import get_activity_details, get_recent_activities
from intervals_icu_mcp.tools.activity_analysis import get_activity_intervals, get_hr_histogram

BASE_URL = "https://intervals.icu/api/v1"
ATHLETE = "i1"
MODEL = "claude-haiku-4-5-20251001"

Counter = Callable[[str], int]


def _activity(i: int) -> dict[str, Any]:
    """A run with the fields the curated responses read, plus native extras."""
    return {
        "id": f"i{1000 + i}",
        "start_date_local": f"2026-09-{(i % 28) + 1:02d}T07:00:00",
        "name": "Morning tempo run",
        "description": "4x8min at threshold, felt strong on the last rep",
        "type": "Run",
        "distance": 12345.6,
        "moving_time": 3725,
        "elapsed_time": 3900,
        "total_elevation_gain": 124.0,
        "average_speed": 3.314,
        "max_speed": 5.12,
        "average_heartrate": 156,
        "max_heartrate": 181,
        "average_cadence": 86.4,
        "max_cadence": 96.0,
        "icu_average_watts": 265,
        "normalized_power": 278,
        "icu_weighted_avg_watts": 276,
        "max_watts": 512,
        "variability_index": 1.049,
        "efficiency_factor": 1.782,
        "icu_training_load": 92,
        "icu_intensity": 87.6,
        "tss": 91.8,
        "hrss": 88.2,
        "trimp": 142.4,
        "calories": 912,
        "feel": 2,
        "icu_rpe": 7,
        "device_name": "Garmin Forerunner 965",
        "pace": 3.314,
        "gap": 3.402,
        "icu_hr_zones": [138, 152, 162, 171, 180, 186, 200],
        "icu_hr_zone_times": [412, 988, 1402, 721, 202, 0, 0],
        "hr_load": 88,
        "pace_load": 90,
    }


def _intervals() -> dict[str, Any]:
    laps: list[dict[str, Any]] = []
    t = 0
    for i in range(10):
        work = i % 2 == 1
        dur = 480 if work else 180
        laps.append(
            {
                "id": i + 1,
                "type": "WORK" if work else "RECOVERY",
                "start": t,
                "end": t + dur,
                "duration": dur,
                "distance": 1650.0 if work else 480.0,
                "average_watts": 298 if work else 180,
                "normalized_power": 301 if work else 184,
                "average_heartrate": 168 if work else 142,
                "max_heartrate": 176 if work else 158,
                "average_cadence": 88.2,
                "average_speed": 3.44 if work else 2.67,
                "zone": 4 if work else 1,
            }
        )
        t += dur
    return {"id": "i1000", "icu_intervals": laps}


def _hr_histogram() -> list[dict[str, Any]]:
    # 5-bpm buckets from 100 to 190 bpm
    return [{"min": b, "max": b + 5, "secs": 60 + (b % 7) * 30} for b in range(100, 190, 5)]


def _ctx() -> Any:
    ctx = MagicMock()
    ctx.get_state = AsyncMock(
        return_value=ICUConfig(intervals_icu_api_key="k", intervals_icu_athlete_id=ATHLETE)
    )
    return ctx


def _estimate(text: str) -> int:
    return round(len(text) / 3.5)


def _exact_counter() -> Counter:
    import anthropic

    client = anthropic.Anthropic()

    def count(text: str) -> int:
        result = client.messages.count_tokens(
            model=MODEL, messages=[{"role": "user", "content": text}]
        )
        return result.input_tokens

    return count


async def _run(call: Callable[[], Awaitable[str]]) -> str:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        router.get("/activity/i1000").mock(return_value=Response(200, json=_activity(0)))
        router.get(f"/athlete/{ATHLETE}/activities").mock(
            return_value=Response(200, json=[_activity(i) for i in range(30)])
        )
        router.get("/activity/i1000/intervals").mock(return_value=Response(200, json=_intervals()))
        router.get("/activity/i1000/hr-histogram").mock(
            return_value=Response(200, json=_hr_histogram())
        )
        return await call()


async def _scenarios(count: Counter) -> list[tuple[str, int, int]]:
    detail_fields = [
        "distance_meters",
        "moving_time_seconds",
        "pace",
        "icu_hr_zone_times",
        "icu_hr_zones",
        "training.training_load",
    ]
    details = await _run(lambda: get_activity_details(activity_id="i1000", ctx=_ctx()))
    histogram = await _run(lambda: get_hr_histogram(activity_id="i1000", ctx=_ctx()))
    details_filtered = await _run(
        lambda: get_activity_details(activity_id="i1000", fields=detail_fields, ctx=_ctx())
    )
    recent = await _run(lambda: get_recent_activities(ctx=_ctx()))
    recent_filtered = await _run(
        lambda: get_recent_activities(
            fields=["activities.distance_meters", "activities.training_load"], ctx=_ctx()
        )
    )
    intervals = await _run(lambda: get_activity_intervals(activity_id="i1000", ctx=_ctx()))
    intervals_filtered = await _run(
        lambda: get_activity_intervals(
            activity_id="i1000",
            fields=["intervals.duration_seconds", "intervals.performance.average_heartrate"],
            ctx=_ctx(),
        )
    )
    return [
        ("details -> 6 fields (incl. native HR zones)", count(details), count(details_filtered)),
        (
            "details + HR histogram -> details w/ native zones",
            count(details) + count(histogram),
            count(details_filtered),
        ),
        ("recent activities x30 -> 2 fields", count(recent), count(recent_filtered)),
        ("intervals x10 -> 2 fields", count(intervals), count(intervals_filtered)),
    ]


async def _schema_cost(count: Counter) -> tuple[int, int, int]:
    tools = await mcp.list_tools()
    with_fields = [t.to_mcp_tool().model_dump(exclude_none=True) for t in tools]
    without: list[dict[str, Any]] = json.loads(json.dumps(with_fields))
    n = 0
    for tool in without:
        props = tool.get("inputSchema", {}).get("properties", {})
        if props.pop("fields", None) is not None:
            n += 1
    return n, count(json.dumps(without)), count(json.dumps(with_fields))


async def main() -> int:
    parser = argparse.ArgumentParser(description="Measure response token savings from the fields filter.")
    parser.add_argument("--exact", action="store_true", help="Use Anthropic token counting")
    args = parser.parse_args()

    if args.exact and not os.getenv("ANTHROPIC_API_KEY"):
        print("--exact needs ANTHROPIC_API_KEY", file=sys.stderr)
        return 1
    count = _exact_counter() if args.exact else _estimate
    unit = "tokens" if args.exact else "~tokens (chars/3.5)"

    print(f"Response size, {unit}")
    print(f"{'scenario':<52}{'before':>8}{'after':>8}{'saved':>8}")
    for name, before, after in await _scenarios(count):
        print(f"{name:<52}{before:>8}{after:>8}{1 - after / before:>8.0%}")

    n, without, with_fields = await _schema_cost(count)
    print(f"\nTool-list schema, {unit}: {without} -> {with_fields} ")
    print(f"(+{with_fields - without} for `fields` on {n} tools)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
