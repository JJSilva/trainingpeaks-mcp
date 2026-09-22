"""CORE body temperature extraction from a workout's device FIT file.

The CORE sensor reaches TrainingPeaks two ways: as the native FIT record field
``core_temperature`` (written by newer Garmin firmware), and as Connect IQ
developer fields written by the CORE data field app. The CIQ app has renamed
those fields between versions, so they are matched on substrings rather than
exact names, and every developer field name seen is reported back for debugging.
"""

import gzip
import io
import logging
from asyncio import to_thread
from dataclasses import dataclass
from datetime import datetime
from math import ceil
from typing import Any

from pydantic import ValidationError

from tp_mcp.client import TPClient
from tp_mcp.tools._validation import WorkoutIdInput, format_validation_error

logger = logging.getLogger("tp-mcp")

# FIT record message field 139: native core body temperature, degrees C.
_RECORD_MESG_NAME = "record"
_CORE_TEMPERATURE_FIELD_NUM = 139
_DEV_FIELD_TYPE = "devfield"

# Substrings the CORE Connect IQ data field has used across versions. Matched
# case-insensitively; exact names are deliberately not hard-coded.
_DEV_FIELD_SUBSTRINGS = ("core", "skin", "hsi", "heat_strain", "strain", "quality")

# Plausibility bands. Samples outside these are sensor dropouts or unit
# mismatches (e.g. Fahrenheit), and are counted in discarded_samples.
_CORE_TEMP_RANGE = (30.0, 45.0)
_SKIN_TEMP_RANGE = (10.0, 50.0)
_HSI_RANGE = (0.0, 10.0)

_HSI_THRESHOLDS = (2, 4, 6, 8)

_SERIES_INTERVAL_S = 60
_SERIES_MAX_POINTS = 300
# A pause or dropout should not count as time spent at a heat strain level, so
# the interval a sample represents is capped.
_MAX_SAMPLE_GAP_S = 60.0

_NO_CORE_DATA_NOTE = (
    "No core temperature data in this file (neither the native FIT core_temperature field nor a"
    " CORE developer field). The CORE Connect IQ data field was probably not active during the"
    " recording. Check developer_fields_found to see what the device did write."
)


@dataclass
class _Sample:
    """One record message, with whichever CORE channels it carried."""

    t_offset: float
    native_core: float | None = None
    dev_core: float | None = None
    skin: float | None = None
    hsi: float | None = None


@dataclass
class _ParsedFit:
    """Everything tp_get_core_data needs out of one FIT file."""

    dev_field_names: list[str]
    samples: list[_Sample]
    duration_seconds: float | None


def _classify_dev_field(name: str) -> str | None:
    """Map a developer field name to a CORE channel, or None if unrecognised.

    Order matters: ``core_data_quality`` contains "core" but is a quality score,
    not a temperature, so quality is matched first.
    """
    lower = name.lower()
    if not any(token in lower for token in _DEV_FIELD_SUBSTRINGS):
        return None
    if "quality" in lower:
        return "quality"
    if "skin" in lower:
        return "skin"
    if "hsi" in lower or "strain" in lower:
        return "hsi"
    if "core" in lower:
        return "core"
    return None


def _timestamp_seconds(value: Any) -> float | None:
    """Normalise a FIT timestamp to epoch seconds.

    fitdecode yields a datetime for real UTC timestamps, but leaves the raw int
    for the "system time" range a device writes before it has a clock fix.
    """
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _as_float(value: Any) -> float | None:
    """Coerce a FIT field value to float, or None when it is not numeric."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _parse_fit(fit_bytes: bytes) -> _ParsedFit:
    """Pull CORE channels out of every record message. Runs off the event loop."""
    import fitdecode

    dev_field_names: list[str] = []
    seen_dev_names: set[str] = set()
    samples: list[_Sample] = []
    first_ts: float | None = None
    last_ts: float | None = None

    with fitdecode.FitReader(io.BytesIO(fit_bytes)) as fit:
        for frame in fit:
            if frame.frame_type != fitdecode.FIT_FRAME_DATA or frame.name != _RECORD_MESG_NAME:
                continue

            timestamp: float | None = None
            sample = _Sample(t_offset=0.0)
            for field in frame.fields:
                if field.field_type == _DEV_FIELD_TYPE:
                    name = str(field.name)
                    if name not in seen_dev_names:
                        seen_dev_names.add(name)
                        dev_field_names.append(name)
                    channel = _classify_dev_field(name)
                    if channel == "core":
                        sample.dev_core = _as_float(field.value)
                    elif channel == "skin":
                        sample.skin = _as_float(field.value)
                    elif channel == "hsi":
                        sample.hsi = _as_float(field.value)
                elif field.name == "timestamp":
                    timestamp = _timestamp_seconds(field.value)
                elif field.def_num == _CORE_TEMPERATURE_FIELD_NUM:
                    sample.native_core = _as_float(field.value)

            if timestamp is None:
                continue
            if first_ts is None:
                first_ts = timestamp
            last_ts = timestamp
            sample.t_offset = timestamp - first_ts
            samples.append(sample)

    duration = last_ts - first_ts if first_ts is not None and last_ts is not None else None
    return _ParsedFit(dev_field_names=dev_field_names, samples=samples, duration_seconds=duration)


def _in_range(value: float, bounds: tuple[float, float]) -> bool:
    """Is value inside the inclusive plausibility band?"""
    return bounds[0] <= value <= bounds[1]


def _temp_stats(points: list[tuple[float, float]]) -> dict[str, Any] | None:
    """Summarise a temperature channel, or None when it has no valid samples."""
    if not points:
        return None
    values = [v for _, v in points]
    return {
        "avg": round(sum(values) / len(values), 2),
        "max": round(max(values), 2),
        "min": round(min(values), 2),
        "unit": "C",
        "samples": len(values),
    }


def _sample_durations(points: list[tuple[float, float]]) -> list[float]:
    """Seconds each sample represents, capped so gaps do not inflate totals."""
    if not points:
        return []
    if len(points) == 1:
        return [1.0]
    durations = []
    for i in range(len(points) - 1):
        durations.append(min(max(points[i + 1][0] - points[i][0], 0.0), _MAX_SAMPLE_GAP_S))
    # The final sample gets the same span as the one before it.
    durations.append(durations[-1])
    return durations


def _hsi_stats(points: list[tuple[float, float]]) -> dict[str, Any] | None:
    """Summarise heat strain index, including time spent at or above thresholds."""
    if not points:
        return None
    values = [v for _, v in points]
    durations = _sample_durations(points)
    time_at_or_above = {
        str(threshold): round(
            sum(dt for (_, value), dt in zip(points, durations, strict=True) if value >= threshold), 1
        )
        for threshold in _HSI_THRESHOLDS
    }
    return {
        "avg": round(sum(values) / len(values), 2),
        "max": round(max(values), 2),
        "samples": len(values),
        "time_at_or_above": time_at_or_above,
    }


def _build_series(
    core: list[tuple[float, float]],
    skin: list[tuple[float, float]],
    hsi: list[tuple[float, float]],
) -> list[dict[str, Any]] | None:
    """Bucket the valid samples into ~60 s means, capped at 300 points."""
    all_offsets = [t for t, _ in core] + [t for t, _ in skin] + [t for t, _ in hsi]
    if not all_offsets:
        return None

    # Widen the step past 60 s when needed so the bucket count stays inside the
    # cap; a span of S at step T yields floor(S / T) + 1 buckets.
    span = max(all_offsets) - min(all_offsets)
    step = max(_SERIES_INTERVAL_S, ceil(span / (_SERIES_MAX_POINTS - 1))) if span > 0 else _SERIES_INTERVAL_S

    buckets: dict[int, dict[str, list[float]]] = {}
    for channel, points in (("core_temp", core), ("skin_temp", skin), ("hsi", hsi)):
        for offset, value in points:
            bucket = buckets.setdefault(int(offset // step), {})
            bucket.setdefault(channel, []).append(value)

    series = []
    for index in sorted(buckets)[:_SERIES_MAX_POINTS]:
        bucket = buckets[index]
        point: dict[str, Any] = {"t_offset_seconds": index * step}
        for channel in ("core_temp", "skin_temp", "hsi"):
            values = bucket.get(channel)
            point[channel] = round(sum(values) / len(values), 2) if values else None
        series.append(point)
    return series


def _summarize(parsed: _ParsedFit, workout_id: str, file_name: str | None) -> dict[str, Any]:
    """Turn parsed records into the tool's response payload."""
    native_core = [(s.t_offset, s.native_core) for s in parsed.samples if s.native_core is not None]
    dev_core = [(s.t_offset, s.dev_core) for s in parsed.samples if s.dev_core is not None]

    # Prefer the native field, but fall back to the CIQ one when the native
    # samples are all implausible (e.g. a device logging Fahrenheit).
    valid_native = [p for p in native_core if _in_range(p[1], _CORE_TEMP_RANGE)]
    if valid_native or not dev_core:
        core_source, raw_core, core = "native", native_core, valid_native
    else:
        core_source = "developer"
        raw_core = dev_core
        core = [p for p in dev_core if _in_range(p[1], _CORE_TEMP_RANGE)]

    raw_skin = [(s.t_offset, s.skin) for s in parsed.samples if s.skin is not None]
    raw_hsi = [(s.t_offset, s.hsi) for s in parsed.samples if s.hsi is not None]
    skin = [p for p in raw_skin if _in_range(p[1], _SKIN_TEMP_RANGE)]
    hsi = [p for p in raw_hsi if _in_range(p[1], _HSI_RANGE)]
    discarded = (len(raw_core) - len(core)) + (len(raw_skin) - len(skin)) + (len(raw_hsi) - len(hsi))

    core_stats = _temp_stats(core)
    if core_stats is not None:
        core_stats["source"] = core_source

    result: dict[str, Any] = {
        "workout_id": workout_id,
        "file_name": file_name,
        "developer_fields_found": parsed.dev_field_names,
        "core_temp": core_stats,
        "skin_temp": _temp_stats(skin),
        "hsi": _hsi_stats(hsi),
        "duration_seconds": round(parsed.duration_seconds, 1) if parsed.duration_seconds is not None else None,
        "discarded_samples": discarded,
    }

    series = _build_series(core, skin, hsi)
    if series is not None:
        result["series"] = series
    if core_stats is None:
        result["note"] = _NO_CORE_DATA_NOTE
    return result


async def tp_get_core_data(workout_id: str) -> dict[str, Any]:
    """Extract CORE body temperature data from a workout's device FIT file.

    Downloads the first device file attached to the workout, gunzips it if
    needed, and reads core temperature, skin temperature, and heat strain index
    from the FIT record messages.

    Args:
        workout_id: The workout ID.

    Returns:
        Dict with core_temp/skin_temp/hsi summaries and an optional downsampled
        series, or error. core_temp is null with an explanatory note when the
        file carries no core temperature at all.
    """
    try:
        validated = WorkoutIdInput(workout_id=workout_id)
    except (ValidationError, ValueError) as e:
        msg = format_validation_error(e) if isinstance(e, ValidationError) else str(e)
        return {"isError": True, "error_code": "VALIDATION_ERROR", "message": msg}

    async with TPClient() as client:
        athlete_id = await client.ensure_athlete_id()
        if not athlete_id:
            return {
                "isError": True,
                "error_code": "AUTH_INVALID",
                "message": "Could not get athlete ID. Re-authenticate.",
            }

        details_endpoint = f"/fitness/v6/athletes/{athlete_id}/workouts/{validated.workout_id}/details"
        details_response = await client.get(details_endpoint)
        if details_response.is_error:
            if details_response.error_code is not None and details_response.error_code.value == "NOT_FOUND":
                return {
                    "isError": True,
                    "error_code": "NOT_FOUND",
                    "message": f"Workout {validated.workout_id} not found.",
                }
            return {
                "isError": True,
                "error_code": details_response.error_code.value if details_response.error_code else "API_ERROR",
                "message": details_response.message,
            }

        details = details_response.data if isinstance(details_response.data, dict) else {}
        file_infos = details.get("workoutDeviceFileInfos")
        if not isinstance(file_infos, list):
            file_infos = []
        first_file = next((f for f in file_infos if isinstance(f, dict)), None)
        if first_file is None or first_file.get("fileId") is None:
            return {
                "isError": True,
                "error_code": "NOT_FOUND",
                "message": (
                    f"Workout {validated.workout_id} has no device file to read CORE data from."
                    " Only workouts with an uploaded device recording carry one."
                ),
            }

        file_id = str(first_file["fileId"])
        file_name = first_file.get("fileName")
        raw_endpoint = f"/fitness/v6/athletes/{athlete_id}/workouts/{validated.workout_id}/rawfiledata/{file_id}"
        raw_response = await client.get_raw(raw_endpoint)
        if raw_response.is_error:
            if raw_response.error_code is not None and raw_response.error_code.value == "NOT_FOUND":
                return {"isError": True, "error_code": "NOT_FOUND", "message": f"Workout file {file_id} not found."}
            return {
                "isError": True,
                "error_code": raw_response.error_code.value if raw_response.error_code else "API_ERROR",
                "message": raw_response.message,
            }

    content = raw_response.content
    name = str(file_name or "")
    is_gzip = (
        name.lower().endswith(".gz")
        or (raw_response.content_type or "").lower().startswith("application/gzip")
        or content[:2] == b"\x1f\x8b"
    )
    if is_gzip:
        try:
            content = gzip.decompress(content)
        except (OSError, EOFError) as e:
            return {
                "isError": True,
                "error_code": "API_ERROR",
                "message": f"Device file {file_id} looked gzipped but could not be decompressed: {e}",
            }

    try:
        parsed = await to_thread(_parse_fit, content)
    except ImportError:
        return {
            "isError": True,
            "error_code": "API_ERROR",
            "message": "fitdecode is not installed on the server, so FIT files cannot be parsed.",
        }
    except Exception as e:
        logger.exception("Failed to parse FIT file for workout %s", validated.workout_id)
        return {
            "isError": True,
            "error_code": "API_ERROR",
            "message": (
                f"Could not parse device file {file_id} as a FIT file: {e}."
                " CORE data is only available from FIT recordings."
            ),
        }

    return _summarize(parsed, workout_id=str(validated.workout_id), file_name=file_name)
