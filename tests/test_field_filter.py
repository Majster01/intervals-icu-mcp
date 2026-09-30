"""Tests for the `fields` response filter and native-field passthrough."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from httpx import Response

from intervals_icu_mcp.response_builder import ResponseBuilder, item_paths, merge_native
from intervals_icu_mcp.tools.activities import get_activity_details, get_recent_activities
from intervals_icu_mcp.tools.activity_analysis import get_activity_intervals

# Heavy read tools that expose `fields`. Keep in sync with the docs/tools.md list.
FIELDS_TOOLS = {
    "icu_get_recent_activities",
    "icu_get_activities_by_date",
    "icu_get_activity_details",
    "icu_search_activities",
    "icu_search_activities_full",
    "icu_get_activities_around",
    "icu_get_activity_streams",
    "icu_get_activity_intervals",
    "icu_get_best_efforts",
    "icu_search_intervals",
    "icu_get_calendar_events",
    "icu_get_upcoming_workouts",
    "icu_get_event",
    "icu_get_fitness_chart",
    "icu_get_wellness_data",
    "icu_get_power_curves",
    "icu_get_hr_curves",
    "icu_get_pace_curves",
    "icu_get_workout_library",
    "icu_get_workouts_in_folder",
}

DETAIL = {
    "id": "a1",
    "name": "Run",
    "distance_meters": 10000,
    "power": {"average": 200, "max": 400},
    "training": {"training_load": 80, "intensity_factor": 0.8},
}


def _build(data, fields, **kwargs):
    return json.loads(ResponseBuilder.build_response(data=data, fields=fields, **kwargs))


def _ctx(config):
    ctx = MagicMock()
    ctx.get_state = AsyncMock(return_value=config)
    return ctx


class TestProjection:
    def test_none_and_empty_leave_data_unchanged(self):
        assert _build(DETAIL, None)["data"] == DETAIL
        assert _build(DETAIL, [])["data"] == DETAIL
        assert _build(DETAIL, ["", " "])["data"] == DETAIL

    def test_nested_path_and_id_kept(self):
        out = _build(DETAIL, ["distance_meters", "training.training_load"])
        assert out["data"] == {
            "id": "a1",
            "distance_meters": 10000,
            "training": {"training_load": 80},
        }
        assert "unknown_fields" not in out["metadata"]

    def test_leaf_keeps_whole_subtree(self):
        out = _build(DETAIL, ["power", "power.max"])
        assert out["data"]["power"] == {"average": 200, "max": 400}

    def test_lists_traversed_per_item(self):
        data = {
            "activities": [
                {"id": "a", "distance_meters": 1, "training_load": 10, "name": "x"},
                {"id": "b", "distance_meters": 2, "training_load": 20, "name": "y"},
            ],
            "count": 2,
        }
        out = _build(data, ["activities.training_load"])
        assert out["data"] == {
            "activities": [{"id": "a", "training_load": 10}, {"id": "b", "training_load": 20}]
        }

    def test_wildcard_matches_dict_keys(self):
        data = {
            "events_by_date": {
                "2026-10-01": [{"id": 1, "name": "Z2", "description": "long"}],
                "2026-10-02": [{"id": 2, "name": "VO2", "description": "long"}],
            }
        }
        out = _build(data, ["events_by_date.*.name"])
        assert out["data"]["events_by_date"] == {
            "2026-10-01": [{"id": 1, "name": "Z2"}],
            "2026-10-02": [{"id": 2, "name": "VO2"}],
        }

    def test_unknown_fields_reported(self):
        out = _build(DETAIL, ["distance_meters", "nope", "power.nope"])
        assert out["metadata"]["unknown_fields"] == ["nope", "power.nope"]
        assert "power.average" in out["metadata"]["available_fields"]
        assert out["data"] == {"id": "a1", "distance_meters": 10000, "power": {}}

    def test_empty_list_not_flagged_unknown(self):
        out = _build({"activities": [], "count": 0}, ["activities.pace"])
        assert "unknown_fields" not in out["metadata"]

    def test_analysis_and_metadata_untouched(self):
        out = _build(DETAIL, ["distance_meters"], analysis={"note": "x"}, metadata={"m": 1})
        assert out["analysis"] == {"note": "x"}
        assert out["metadata"] == {"m": 1}


class TestNativeHelpers:
    def test_item_paths_strips_prefix(self):
        assert item_paths(["activities.pace", "count", " activities.a.b"], "activities") == [
            "pace",
            "a.b",
        ]
        assert item_paths(None, "activities") == []

    def test_merge_native_adds_only_named_absent_keys(self):
        curated = {"id": "a1", "training": {"training_load": 80}}
        raw = {"id": "raw", "training": "raw", "pace": 3.1, "hr_load": 70}
        out = merge_native(curated, raw, ["pace", "training.x", "missing"])
        assert out == {"id": "a1", "training": {"training_load": 80}, "pace": 3.1}


RAW_ACTIVITY = {
    "id": "a1",
    "start_date_local": "2026-09-28T07:00:00",
    "name": "Tempo run",
    "type": "Run",
    "distance": 10000.0,
    "moving_time": 2700,
    "icu_training_load": 85,
    "icu_intensity": 88.0,
    "average_heartrate": 158,
    "pace": 3.7,
    "icu_hr_zones": [140, 155, 165, 175, 185],
    "icu_hr_zone_times": [300, 900, 1200, 300, 0],
    "hr_load": 82,
}


class TestActivityTools:
    async def test_details_native_fields_only_when_named(self, mock_config, respx_mock):
        respx_mock.get("/activity/a1").mock(return_value=Response(200, json=RAW_ACTIVITY))

        default = json.loads(await get_activity_details(activity_id="a1", ctx=_ctx(mock_config)))
        assert "icu_hr_zone_times" not in default["data"]
        assert "pace" not in default["data"]

        filtered = json.loads(
            await get_activity_details(
                activity_id="a1",
                fields=[
                    "distance_meters",
                    "moving_time_seconds",
                    "pace",
                    "icu_hr_zone_times",
                    "training.training_load",
                ],
                ctx=_ctx(mock_config),
            )
        )
        assert filtered["data"] == {
            "id": "a1",
            "distance_meters": 10000.0,
            "moving_time_seconds": 2700,
            "pace": 3.7,
            "icu_hr_zone_times": [300, 900, 1200, 300, 0],
            "training": {"training_load": 85},
        }

    async def test_recent_activities_per_item_native(self, mock_config, respx_mock):
        respx_mock.get("/athlete/i123456/activities").mock(
            return_value=Response(200, json=[RAW_ACTIVITY, {**RAW_ACTIVITY, "id": "a2"}])
        )
        out = json.loads(
            await get_recent_activities(
                fields=["activities.training_load", "activities.icu_hr_zone_times"],
                ctx=_ctx(mock_config),
            )
        )
        assert out["data"]["activities"] == [
            {"id": "a1", "training_load": 85, "icu_hr_zone_times": [300, 900, 1200, 300, 0]},
            {"id": "a2", "training_load": 85, "icu_hr_zone_times": [300, 900, 1200, 300, 0]},
        ]

    async def test_intervals_native_passthrough(self, mock_config, respx_mock):
        respx_mock.get("/activity/a1/intervals").mock(
            return_value=Response(
                200,
                json={
                    "id": "a1",
                    "icu_intervals": [
                        {"id": 1, "type": "WORK", "moving_time": 600, "zone": 4},
                        {"id": 2, "type": "RECOVERY", "moving_time": 120, "zone": 1},
                    ],
                },
            )
        )
        out = json.loads(
            await get_activity_intervals(
                activity_id="a1", fields=["intervals.zone"], ctx=_ctx(mock_config)
            )
        )
        assert out["data"] == {
            "activity_id": "a1",
            "intervals": [{"id": 1, "zone": 4}, {"id": 2, "zone": 1}],
        }


def test_fields_param_registered_on_heavy_tools_only():
    from intervals_icu_mcp.server import mcp

    tools = asyncio.run(mcp.list_tools())
    with_fields = {t.name for t in tools if "fields" in t.parameters.get("properties", {})}
    assert with_fields == FIELDS_TOOLS
