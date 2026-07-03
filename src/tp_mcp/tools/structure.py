"""Workout structure builder, validator, and IF/TSS computation.

Converts a simplified step-based structure format into the wire format
expected by the TrainingPeaks API, including cumulative begin/end times
and polyline generation.
"""

import json
import logging
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from tp_mcp.tools._validation import format_validation_error

logger = logging.getLogger("tp-mcp")

# Valid intensity classes for workout steps
INTENSITY_CLASSES = {"warmUp", "active", "rest", "coolDown", "other"}

# Valid primary intensity metrics
INTENSITY_METRICS = {"percentOfFtp", "percentOfThresholdHr", "percentOfThresholdPace"}

# Valid per-workout length units for the simplified format.
LENGTH_UNITS = {"yard", "meter"}

# Exact yard -> metre conversion. The TP API rejects "yard"/"yards" length
# units (HTTP 400); distance MUST be stored in metres. 25yd -> 22.86m etc.
YARDS_TO_METERS = 0.9144

# Nominal cumulative-counter increment for a rest (duration) step inside a
# distance-based structure. Observed from coach-built pool workouts: work
# steps advance the begin/end counter by their metre length, rests by 10.
REST_COUNTER_INCREMENT = 10.0


class SimpleStep(BaseModel):
    """A single workout step in the simplified input format.

    A step is either time-based (``duration_seconds``) or distance-based
    (``distance_yards`` or ``distance_meters``). Exactly one of the three
    length fields must be provided. Distance is only meaningful for pool
    swims; rests inside a pool set stay time-based (``duration_seconds``).
    """

    name: str = Field(min_length=1, max_length=100)
    type: str = Field(default="step")
    duration_seconds: int | None = Field(default=None, gt=0, le=86400)
    distance_yards: float | None = Field(default=None, gt=0, le=100000)
    distance_meters: float | None = Field(default=None, gt=0, le=100000)
    intensity_min: float = Field(ge=0, le=300)
    intensity_max: float = Field(ge=0, le=300)
    intensityClass: str = Field(default="active")  # noqa: N815
    cadence_min: float | None = Field(default=None, ge=0, le=300)
    cadence_max: float | None = Field(default=None, ge=0, le=300)

    @field_validator("intensityClass")
    @classmethod
    def check_intensity_class(cls, v: str) -> str:
        if v not in INTENSITY_CLASSES:
            valid = ", ".join(sorted(INTENSITY_CLASSES))
            raise ValueError(f"Invalid intensityClass '{v}'. Valid: {valid}")
        return v

    @model_validator(mode="after")
    def check_intensity_range(self) -> "SimpleStep":
        provided = [
            v
            for v in (self.duration_seconds, self.distance_yards, self.distance_meters)
            if v is not None
        ]
        if len(provided) == 0:
            raise ValueError(
                "Step must provide one of duration_seconds, distance_yards, "
                "or distance_meters",
            )
        if len(provided) > 1:
            raise ValueError(
                "Step must provide only one of duration_seconds, distance_yards, "
                "or distance_meters",
            )
        if self.intensity_min > self.intensity_max:
            raise ValueError("intensity_min must be <= intensity_max")
        if (
            self.cadence_min is not None
            and self.cadence_max is not None
            and self.cadence_min > self.cadence_max
        ):
            raise ValueError("cadence_min must be <= cadence_max")
        return self

    @property
    def is_distance(self) -> bool:
        """True if this step is distance-based rather than time-based."""
        return self.distance_yards is not None or self.distance_meters is not None

    @property
    def meters(self) -> float | None:
        """Distance in metres, converting yards if needed. None for rests."""
        if self.distance_yards is not None:
            return self.distance_yards * YARDS_TO_METERS
        return self.distance_meters


class SimpleRepetitionBlock(BaseModel):
    """A repetition block containing multiple steps repeated N times."""

    type: str = Field(default="repetition")
    name: str = Field(default="Repeat")
    reps: int = Field(gt=0, le=100)
    steps: list[SimpleStep] = Field(min_length=1)


class SimpleWorkoutStructure(BaseModel):
    """Top-level simplified structure input from the LLM."""

    primaryIntensityMetric: str = Field(default="percentOfFtp")  # noqa: N815
    # Authoring/display unit hint for distance workouts. Distance is always
    # stored in metres on the wire; this only controls the display unit
    # (``visualizationDistanceUnit``). None -> inferred from the steps.
    length_unit: str | None = None
    steps: list[SimpleStep | SimpleRepetitionBlock] = Field(min_length=1)

    @field_validator("primaryIntensityMetric")
    @classmethod
    def check_metric(cls, v: str) -> str:
        if v not in INTENSITY_METRICS:
            valid = ", ".join(sorted(INTENSITY_METRICS))
            raise ValueError(f"Invalid primaryIntensityMetric '{v}'. Valid: {valid}")
        return v

    @field_validator("length_unit")
    @classmethod
    def check_length_unit(cls, v: str | None) -> str | None:
        if v is not None and v not in LENGTH_UNITS:
            valid = ", ".join(sorted(LENGTH_UNITS))
            raise ValueError(f"Invalid length_unit '{v}'. Valid: {valid}")
        return v


def _build_step_wire(step: SimpleStep) -> dict[str, Any]:
    """Convert a SimpleStep to wire format.

    Distance steps emit ``{"unit": "meter"}`` (never "yard"/"yards", which the
    TP API rejects); time-based steps and rests emit ``{"unit": "second"}``.
    """
    targets: list[dict[str, Any]] = [
        {"minValue": step.intensity_min, "maxValue": step.intensity_max},
    ]
    if step.cadence_min is not None and step.cadence_max is not None:
        targets.append(
            {
                "minValue": step.cadence_min,
                "maxValue": step.cadence_max,
                "unit": "roundOrStridePerMinute",
            }
        )

    if step.is_distance:
        length = {"value": round(step.meters or 0.0, 2), "unit": "meter"}
    else:
        length = {"value": step.duration_seconds, "unit": "second"}

    return {
        "name": step.name,
        "type": "step",
        "length": length,
        "targets": targets,
        "intensityClass": step.intensityClass,
        "openDuration": False,
    }


def has_distance_steps(structure: SimpleWorkoutStructure) -> bool:
    """True if any step (including inside repetition blocks) is distance-based."""
    for block in structure.steps:
        if isinstance(block, SimpleRepetitionBlock):
            if any(s.is_distance for s in block.steps):
                return True
        elif block.is_distance:
            return True
    return False


def _display_unit(structure: SimpleWorkoutStructure) -> str:
    """Resolve the display unit for a distance workout.

    Uses the explicit ``length_unit`` hint if set, otherwise infers "yard"
    when any step was authored in yards, defaulting to "meter".
    """
    if structure.length_unit is not None:
        return structure.length_unit
    for block in structure.steps:
        inner = block.steps if isinstance(block, SimpleRepetitionBlock) else [block]
        if any(s.distance_yards is not None for s in inner):
            return "yard"
    return "meter"


def _step_counter_increment(step: SimpleStep, distance_mode: bool) -> float:
    """Cumulative begin/end increment contributed by a single step.

    Duration mode: seconds. Distance mode: metres for work steps, a nominal
    10 for rests (which stay time-based).
    """
    if distance_mode:
        return (step.meters or 0.0) if step.is_distance else REST_COUNTER_INCREMENT
    return float(step.duration_seconds or 0)


def _block_counter_increment(
    block: SimpleStep | SimpleRepetitionBlock, distance_mode: bool,
) -> float:
    """Cumulative begin/end increment contributed by a block."""
    if isinstance(block, SimpleRepetitionBlock):
        inner = sum(_step_counter_increment(s, distance_mode) for s in block.steps)
        return inner * block.reps
    return _step_counter_increment(block, distance_mode)


def _compute_block_duration(block: SimpleStep | SimpleRepetitionBlock) -> int:
    """Compute total duration of a block in seconds (time-based steps only)."""
    if isinstance(block, SimpleRepetitionBlock):
        inner_duration = sum(s.duration_seconds or 0 for s in block.steps)
        return inner_duration * block.reps
    return block.duration_seconds or 0


def _polyline_bar(
    t_start: float, t_end: float, intensity: float, polyline: list[list[float]],
) -> None:
    """Append a rectangular bar to the polyline (TP native format).

    Each segment is drawn as: drop to 0 → rise to intensity → hold → drop to 0.
    """
    polyline.append([round(t_start, 4), 0])
    polyline.append([round(t_start, 4), round(intensity, 4)])
    polyline.append([round(t_end, 4), round(intensity, 4)])
    polyline.append([round(t_end, 4), 0])


def build_wire_structure(structure: SimpleWorkoutStructure) -> dict[str, Any]:
    """Convert simplified structure to TP API wire format.

    Duration-only structures keep the original time-based layout (begin/end in
    seconds, a rendered polyline, ``primaryLengthMetric="duration"``). When any
    step carries a distance, the structure switches to a distance layout:
    distances are stored in metres, rests stay in seconds, begin/end advance by
    metre length (work) / a nominal 10 (rest), the polyline is empty, and
    ``primaryLengthMetric="distance"`` with a top-level
    ``visualizationDistanceUnit`` set to the authored unit.

    Args:
        structure: The simplified workout structure.

    Returns:
        Dict matching the TP API structure format.
    """
    distance_mode = has_distance_steps(structure)

    wire_blocks: list[dict[str, Any]] = []
    cumulative = 0.0

    for block in structure.steps:
        increment = _block_counter_increment(block, distance_mode)
        begin = cumulative
        end = cumulative + increment

        if isinstance(block, SimpleRepetitionBlock):
            inner_steps = [_build_step_wire(s) for s in block.steps]
            wire_block: dict[str, Any] = {
                "type": "repetition",
                "length": {"value": block.reps, "unit": "repetition"},
                "steps": inner_steps,
                "begin": _coerce_counter(begin, distance_mode),
                "end": _coerce_counter(end, distance_mode),
            }
            wire_blocks.append(wire_block)

        else:
            # Single step — TP uses repetition wrapper with value=1
            wire_step = _build_step_wire(block)
            wire_block = {
                "type": "step",
                "length": {"value": 1, "unit": "repetition"},
                "steps": [wire_step],
                "begin": _coerce_counter(begin, distance_mode),
                "end": _coerce_counter(end, distance_mode),
            }
            wire_blocks.append(wire_block)

        cumulative = end

    result: dict[str, Any] = {
        "structure": wire_blocks,
        # Distance workouts accept an empty polyline; only duration workouts
        # render the zero-drop bar polyline.
        "polyline": [] if distance_mode else _build_duration_polyline(structure),
        "primaryLengthMetric": "distance" if distance_mode else "duration",
        "primaryIntensityMetric": structure.primaryIntensityMetric,
        "primaryIntensityTargetOrRange": "range",
    }
    if distance_mode:
        result["visualizationDistanceUnit"] = _display_unit(structure)
    return result


def _coerce_counter(value: float, distance_mode: bool) -> float | int:
    """Coerce a begin/end counter value.

    Duration mode keeps integer seconds (preserving the original wire format);
    distance mode keeps metres rounded to 2 decimals.
    """
    return round(value, 2) if distance_mode else int(value)


def _build_duration_polyline(structure: SimpleWorkoutStructure) -> list[list[float]]:
    """Build the zero-drop bar polyline for a time-based structure."""
    total_duration = sum(_compute_block_duration(b) for b in structure.steps)
    polyline: list[list[float]] = []
    poly_cumulative = 0

    for block in structure.steps:
        if isinstance(block, SimpleRepetitionBlock):
            for _rep in range(block.reps):
                for s in block.steps:
                    t_start = poly_cumulative / total_duration if total_duration > 0 else 0
                    poly_cumulative += s.duration_seconds or 0
                    t_end = poly_cumulative / total_duration if total_duration > 0 else 0
                    intensity = s.intensity_max / 100.0
                    _polyline_bar(t_start, t_end, intensity, polyline)
        else:
            t_start = poly_cumulative / total_duration if total_duration > 0 else 0
            poly_cumulative += block.duration_seconds or 0
            t_end = poly_cumulative / total_duration if total_duration > 0 else 0
            intensity = block.intensity_max / 100.0
            _polyline_bar(t_start, t_end, intensity, polyline)

    return polyline


def compute_if_tss(structure: SimpleWorkoutStructure) -> tuple[float, float, int]:
    """Compute IF and TSS from a workout structure.

    Uses NP-style time-weighted 4th-power average of midpoint intensities.
    IF = (weighted_sum / total_seconds) ^ 0.25 / 100
    TSS = (total_seconds * IF^2 * 100) / 3600

    Args:
        structure: The simplified workout structure.

    Returns:
        Tuple of (IF, TSS, total_duration_seconds). Distance-based structures
        (pool swims) have no time-weighting basis, so this returns zeros.
    """
    if has_distance_steps(structure):
        return 0.0, 0.0, 0

    weighted_sum = 0.0
    total_seconds = 0

    for block in structure.steps:
        if isinstance(block, SimpleRepetitionBlock):
            for _rep in range(block.reps):
                for step in block.steps:
                    seconds = step.duration_seconds or 0
                    midpoint = (step.intensity_min + step.intensity_max) / 2.0
                    weighted_sum += seconds * (midpoint ** 4)
                    total_seconds += seconds
        else:
            seconds = block.duration_seconds or 0
            midpoint = (block.intensity_min + block.intensity_max) / 2.0
            weighted_sum += seconds * (midpoint ** 4)
            total_seconds += seconds

    if total_seconds == 0:
        return 0.0, 0.0, 0

    intensity_factor = (weighted_sum / total_seconds) ** 0.25 / 100.0
    tss = (total_seconds * intensity_factor ** 2 * 100.0) / 3600.0

    return round(intensity_factor, 3), round(tss, 1), total_seconds


def parse_structure_input(structure_input: dict[str, Any] | str) -> SimpleWorkoutStructure:
    """Parse structure input from either a dict or JSON string.

    Args:
        structure_input: Structure as dict or JSON string.

    Returns:
        Parsed SimpleWorkoutStructure.

    Raises:
        ValidationError: If structure is invalid.
        ValueError: If JSON is malformed.
    """
    if isinstance(structure_input, str):
        try:
            data = json.loads(structure_input)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in structure: {e}") from e
    else:
        data = structure_input

    # Parse steps - distinguish between simple steps and repetition blocks
    raw_steps = data.get("steps", [])
    parsed_steps: list[SimpleStep | SimpleRepetitionBlock] = []

    for raw_step in raw_steps:
        if raw_step.get("type") == "repetition":
            parsed_steps.append(SimpleRepetitionBlock.model_validate(raw_step))
        else:
            parsed_steps.append(SimpleStep.model_validate(raw_step))

    return SimpleWorkoutStructure(
        primaryIntensityMetric=data.get("primaryIntensityMetric", "percentOfFtp"),
        length_unit=data.get("length_unit"),
        steps=parsed_steps,
    )


async def tp_validate_structure(structure: str) -> dict[str, Any]:
    """Validate a workout interval structure without creating a workout.

    Args:
        structure: JSON string of the simplified structure format.

    Returns:
        Dict with validation result (block count, total duration, metric) or error.
    """
    try:
        parsed = parse_structure_input(structure)
    except (ValidationError, ValueError) as e:
        msg = format_validation_error(e) if isinstance(e, ValidationError) else str(e)
        return {
            "isError": True,
            "error_code": "VALIDATION_ERROR",
            "message": msg,
        }

    intensity_factor, tss, total_seconds = compute_if_tss(parsed)

    # Count blocks
    block_count = len(parsed.steps)
    step_count = 0
    for block in parsed.steps:
        if isinstance(block, SimpleRepetitionBlock):
            step_count += len(block.steps) * block.reps
        else:
            step_count += 1

    result: dict[str, Any] = {
        "valid": True,
        "block_count": block_count,
        "total_steps": step_count,
        "total_duration_seconds": total_seconds,
        "total_duration_minutes": round(total_seconds / 60, 1),
        "estimated_if": intensity_factor,
        "estimated_tss": tss,
        "intensity_metric": parsed.primaryIntensityMetric,
    }

    if has_distance_steps(parsed):
        # Sum work-step distances (metres) across the whole structure.
        total_meters = 0.0
        for block in parsed.steps:
            inner = block.steps if isinstance(block, SimpleRepetitionBlock) else [block]
            reps = block.reps if isinstance(block, SimpleRepetitionBlock) else 1
            total_meters += reps * sum(s.meters or 0.0 for s in inner)
        result["length_metric"] = "distance"
        result["display_unit"] = _display_unit(parsed)
        result["total_distance_meters"] = round(total_meters, 2)
        result["total_distance_yards"] = round(total_meters / YARDS_TO_METERS, 2)
    else:
        result["length_metric"] = "duration"

    return result
