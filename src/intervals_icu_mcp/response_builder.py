"""Response builder utilities for structured JSON output.

This module provides utilities for building consistent, structured JSON responses
across all MCP tools. All tools return JSON with a standard structure:

{
    "data": {...},           # Main data payload
    "analysis": {...},       # Optional insights and computed metrics
    "metadata": {...}        # Tool-supplied metadata (e.g. includes, scales)
}

Heavy read tools accept a ``fields`` list of dot paths (see ``FieldsParam``);
``build_response`` then keeps only those keys of ``data``. Lists are traversed
per item, ``*`` matches any dict key (e.g. ``events_by_date.*.name``), and
``id`` / ``activity_id`` are always kept.

By default `metadata` only contains fields that individual tools attach. Set
`INTERVALS_ICU_DEBUG_METADATA=true` (or `1`/`yes`) in the environment to also
inject `fetched_at` and `query_type` for debugging.
"""

import functools
import json
import os
from datetime import datetime
from typing import Annotated, Any, TypeAlias, cast

from pydantic import WithJsonSchema

# Advertised as a plain string array (no null branch) to keep the per-tool
# schema cost low; full rules live in docs/tools.md.
FieldsParam = Annotated[
    list[str] | None,
    WithJsonSchema(
        {
            "type": "array",
            "items": {"type": "string"},
            "description": "Only return these data keys (dot paths, e.g. 'activities.pace')",
        }
    ),
]

# Kept in every projected dict so filtered items stay referenceable.
_ALWAYS_KEEP = frozenset({"id", "activity_id"})

# A node in the field tree: a dict of child nodes, or None for "keep whole value".
_FieldTree: TypeAlias = "dict[str, _FieldTree | None]"


@functools.lru_cache(maxsize=1)
def _debug_metadata_enabled() -> bool:
    """Whether to include `fetched_at` / `query_type` in responses.

    Read once at startup and cached. Truthy values: ``true``, ``1``, ``yes``
    (case-insensitive).
    """
    return os.getenv("INTERVALS_ICU_DEBUG_METADATA", "").strip().lower() in {
        "true",
        "1",
        "yes",
    }


def _convert_datetimes(obj: Any) -> Any:  # type: ignore[misc]
    """Recursively convert datetime objects to ISO strings."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {str(k): _convert_datetimes(v) for k, v in obj.items()}  # type: ignore[misc]
    elif isinstance(obj, list):
        return [_convert_datetimes(item) for item in obj]  # type: ignore[misc]
    return obj


def _split_path(path: str) -> list[str]:
    return [seg for seg in path.strip().split(".") if seg]


def _build_field_tree(paths: list[str]) -> _FieldTree:
    """Turn dot paths into a prefix tree. A shorter path wins over a longer one."""
    tree: _FieldTree = {}
    for path in paths:
        segs = _split_path(path)
        if not segs:
            continue
        node: _FieldTree | None = tree
        for seg in segs[:-1]:
            assert node is not None
            if seg in node and node[seg] is None:
                node = None  # an ancestor is already kept whole
                break
            node = node.setdefault(seg, {})
        if node is not None:
            node[segs[-1]] = None
    return tree


def _project(obj: Any, tree: _FieldTree) -> Any:  # type: ignore[misc]
    """Keep only the keys named in ``tree``; lists apply the tree to each item."""
    if isinstance(obj, list):
        return [_project(item, tree) for item in obj]  # type: ignore[misc]
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in cast(dict[str, Any], obj).items():
            if key in tree or "*" in tree:
                sub = tree[key] if key in tree else tree["*"]
                out[key] = value if sub is None else _project(value, sub)
            elif key in _ALWAYS_KEEP:
                out[key] = value
        return out
    return obj


def _path_exists(obj: Any, segs: list[str]) -> bool:  # type: ignore[misc]
    if not segs:
        return True
    if isinstance(obj, list):
        # An empty list can't disprove a path, so don't flag it as unknown.
        items = cast(list[Any], obj)
        return not items or any(_path_exists(item, segs) for item in items)
    if isinstance(obj, dict):
        values = cast(dict[str, Any], obj)
        if segs[0] == "*":
            return not values or any(_path_exists(v, segs[1:]) for v in values.values())
        if segs[0] in values:
            return _path_exists(values[segs[0]], segs[1:])
    return False


def _available_fields(data: dict[str, Any]) -> list[str]:
    """Top-level keys plus one level into dicts / the first list item."""
    paths: list[str] = []
    for key, value in data.items():
        child: Any = value
        if isinstance(child, list) and child:
            child = cast(list[Any], child)[0]
        if isinstance(child, dict) and child:
            paths.extend(f"{key}.{sub}" for sub in cast(dict[str, Any], child))
        else:
            paths.append(key)
    return paths


def item_paths(fields: list[str] | None, prefix: str) -> list[str]:
    """Paths under ``prefix`` with the prefix stripped (``activities.pace`` -> ``pace``)."""
    lead = prefix + "."
    return [p.strip()[len(lead) :] for p in fields or [] if p.strip().startswith(lead)]


def merge_native(
    curated: dict[str, Any], raw: dict[str, Any], paths: list[str] | None
) -> dict[str, Any]:
    """Copy raw API fields named in ``paths`` into ``curated`` when it lacks them.

    Lets ``fields`` reach native Intervals.icu fields (e.g. ``icu_hr_zone_times``)
    that the curated response leaves out. Curated keys are never overwritten.
    """
    for path in paths or []:
        segs = _split_path(path)
        if segs and segs[0] not in curated and segs[0] in raw:
            curated[segs[0]] = raw[segs[0]]
    return curated


class ResponseBuilder:
    """Builder for standardized JSON responses."""

    @staticmethod
    def format_date_with_day(dt: datetime | str | None) -> dict[str, str] | None:
        """Format a date/datetime with explicit day-of-week information.

        Args:
            dt: datetime object or ISO string or None

        Returns:
            Dict with datetime, date, day_of_week, and formatted string, or None if input is None
        """
        if dt is None:
            return None

        # Parse the datetime if it's a string, otherwise use it directly
        if isinstance(dt, str):
            parsed_dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        else:
            parsed_dt = dt

        return {
            "datetime": dt if isinstance(dt, str) else dt.isoformat(),
            "date": parsed_dt.strftime("%Y-%m-%d"),
            "day_of_week": parsed_dt.strftime("%A"),  # e.g., "Monday"
            "formatted": parsed_dt.strftime(
                "%A, %B %d, %Y at %I:%M %p"
            ),  # e.g., "Monday, October 15, 2025 at 02:30 PM"
        }

    @staticmethod
    def build_response(
        data: dict[str, Any],
        analysis: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        query_type: str | None = None,
        fields: list[str] | None = None,
    ) -> str:
        """Build standardized JSON response.

        Args:
            data: Main data payload
            analysis: Optional analysis and insights
            metadata: Optional tool-supplied metadata
            query_type: Optional query type label; only surfaced when
                ``INTERVALS_ICU_DEBUG_METADATA`` is enabled.
            fields: Optional dot paths; when given, ``data`` is projected to
                just those keys. Unmatched paths are reported in metadata.

        Returns:
            JSON string with structure:
            {
                "data": {...},
                "analysis": {...},
                "metadata": {...}
            }

            With ``INTERVALS_ICU_DEBUG_METADATA=true``, metadata is also
            enriched with ``fetched_at`` (ISO timestamp) and, if provided,
            ``query_type``.
        """
        # Convert datetime objects to ISO strings
        converted_data = cast(dict[str, Any], _convert_datetimes(data))
        converted_analysis: dict[str, Any] | None = None
        if analysis:
            converted_analysis = cast(dict[str, Any], _convert_datetimes(analysis))

        meta = metadata or {}
        converted_meta = cast(dict[str, Any], _convert_datetimes(meta))

        paths = [p for p in fields or [] if _split_path(p)]
        if paths:
            unknown = [p for p in paths if not _path_exists(converted_data, _split_path(p))]
            if unknown:
                converted_meta["unknown_fields"] = unknown
                converted_meta["available_fields"] = _available_fields(converted_data)
            converted_data = cast(
                dict[str, Any], _project(converted_data, _build_field_tree(paths))
            )

        response: dict[str, Any] = {"data": converted_data}

        if converted_analysis:
            response["analysis"] = converted_analysis

        if _debug_metadata_enabled():
            converted_meta["fetched_at"] = datetime.now().isoformat()
            if query_type:
                converted_meta["query_type"] = query_type

        response["metadata"] = converted_meta

        return json.dumps(response, separators=(",", ":"))

    @staticmethod
    def build_error_response(
        error_message: str,
        error_type: str = "error",
        suggestions: list[str] | None = None,
    ) -> str:
        """Build standardized error response.

        Args:
            error_message: Human-readable error message
            error_type: Type of error (e.g., "not_found", "rate_limit", "validation")
            suggestions: Optional list of suggestions to resolve the error

        Returns:
            JSON string with error structure
        """
        response: dict[str, dict[str, str | list[str]]] = {
            "error": {
                "message": error_message,
                "type": error_type,
                "timestamp": datetime.now().isoformat(),
            }
        }

        if suggestions:
            response["error"]["suggestions"] = suggestions

        return json.dumps(response, separators=(",", ":"))
