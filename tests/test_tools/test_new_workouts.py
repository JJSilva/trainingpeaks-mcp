"""Tests for new workout tools: create with structure, update, delete, copy, comments."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from tp_mcp.client.http import APIResponse, ErrorCode
from tp_mcp.tools.workouts import (
    tp_add_workout_comment,
    tp_copy_workout,
    tp_create_workout,
    tp_delete_workout,
    tp_get_workout_comments,
    tp_get_workout_note,
    tp_set_workout_note,
    tp_update_workout,
)


class TestCreateWorkoutWithStructure:
    """Tests for tp_create_workout with structure support."""

    @pytest.mark.asyncio
    async def test_create_with_structure_auto_computes_duration(self):
        """Structure should auto-compute duration and TSS."""
        structure = {
            "primaryIntensityMetric": "percentOfFtp",
            "steps": [
                {
                    "name": "WU",
                    "duration_seconds": 600,
                    "intensity_min": 40,
                    "intensity_max": 55,
                    "intensityClass": "warmUp",
                },
                {
                    "name": "Main",
                    "duration_seconds": 1200,
                    "intensity_min": 85,
                    "intensity_max": 95,
                    "intensityClass": "active",
                },
                {
                    "name": "CD",
                    "duration_seconds": 600,
                    "intensity_min": 40,
                    "intensity_max": 55,
                    "intensityClass": "coolDown",
                },
            ],
        }
        create_response = APIResponse(
            success=True,
            data={"workoutId": 7001, "title": "Structured", "workoutDay": "2026-03-01T00:00:00"},
        )

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.post = AsyncMock(return_value=create_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_create_workout(
                date_str="2026-03-01", sport="Bike", title="Structured",
                structure=structure,
            )

        assert result["success"] is True
        payload = mock_instance.post.call_args[1]["json"]
        # Duration auto-computed from structure: 2400s = 40min = 0.667 hours
        assert "totalTimePlanned" in payload
        assert abs(payload["totalTimePlanned"] - 40.0 / 60.0) < 0.01
        # TSS and IF auto-computed with correct semantic mapping (issue #41)
        assert payload["tssPlanned"] == pytest.approx(39.6, abs=0.1)
        assert payload["ifPlanned"] == pytest.approx(0.771, abs=0.001)
        # Guard against IF/TSS swap: IF must be < 1, TSS must be >> 1
        assert payload["ifPlanned"] < 1
        assert payload["tssPlanned"] > 1
        # Structure serialised to JSON string
        assert isinstance(payload["structure"], str)
        parsed = json.loads(payload["structure"])
        assert "structure" in parsed
        assert "polyline" in parsed

    @pytest.mark.asyncio
    async def test_create_with_explicit_duration_overrides_structure(self):
        """Explicit duration should override structure-computed duration."""
        structure = {
            "steps": [
                {
                    "name": "WU",
                    "duration_seconds": 600,
                    "intensity_min": 50,
                    "intensity_max": 60,
                    "intensityClass": "warmUp",
                },
            ],
        }
        create_response = APIResponse(
            success=True,
            data={"workoutId": 7002, "title": "Override", "workoutDay": "2026-03-01T00:00:00"},
        )

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.post = AsyncMock(return_value=create_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_create_workout(
                date_str="2026-03-01", sport="Bike", title="Override",
                duration_minutes=90,  # Override the 10min structure
                structure=structure,
            )

        assert result["success"] is True
        payload = mock_instance.post.call_args[1]["json"]
        assert payload["totalTimePlanned"] == 90 / 60.0  # 1.5 hours

    @pytest.mark.asyncio
    async def test_create_with_tags(self):
        """Tags should be passed in payload."""
        create_response = APIResponse(
            success=True, data={"workoutId": 7003, "title": "Tagged", "workoutDay": "2026-03-01T00:00:00"},
        )

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.post = AsyncMock(return_value=create_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_create_workout(
                date_str="2026-03-01", sport="Run", title="Tagged",
                duration_minutes=60, tags="intervals,hard",
            )

        assert result["success"] is True
        payload = mock_instance.post.call_args[1]["json"]
        assert payload["userTags"] == "intervals,hard"
        assert "tags" not in payload

    @pytest.mark.asyncio
    async def test_create_with_feeling_and_rpe(self):
        """Feeling and RPE should be validated and passed."""
        create_response = APIResponse(
            success=True, data={"workoutId": 7004, "title": "Rated", "workoutDay": "2026-03-01T00:00:00"},
        )

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.post = AsyncMock(return_value=create_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_create_workout(
                date_str="2026-03-01", sport="Run", title="Rated",
                duration_minutes=45, feeling=7, rpe=6,
            )

        assert result["success"] is True
        payload = mock_instance.post.call_args[1]["json"]
        assert payload["feeling"] == 7
        assert payload["rpe"] == 6

    @pytest.mark.asyncio
    async def test_create_feeling_out_of_bounds(self):
        """Feeling > 10 should be rejected."""
        result = await tp_create_workout(
            date_str="2026-03-01", sport="Run", title="Bad",
            duration_minutes=30, feeling=11,
        )
        assert result["isError"] is True
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_create_allows_zero_rpe(self):
        """RPE 0 should be accepted as the low end of the /10 scale."""
        create_response = APIResponse(
            success=True, data={"workoutId": 7006, "title": "Recovery", "workoutDay": "2026-03-01T00:00:00"},
        )

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.post = AsyncMock(return_value=create_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_create_workout(
                date_str="2026-03-01", sport="Run", title="Recovery",
                duration_minutes=30, rpe=0,
            )

        assert result["success"] is True
        payload = mock_instance.post.call_args[1]["json"]
        assert payload["rpe"] == 0

    @pytest.mark.asyncio
    async def test_create_rpe_out_of_bounds(self):
        """RPE < 0 should be rejected; 0 is a valid low-end /10 value."""
        result = await tp_create_workout(
            date_str="2026-03-01", sport="Run", title="Bad",
            duration_minutes=30, rpe=-1,
        )
        assert result["isError"] is True
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_create_without_duration_or_structure(self):
        """Should fail if no duration, simplified structure, or raw structure provided."""
        result = await tp_create_workout(
            date_str="2026-03-01", sport="Run", title="No Duration",
        )
        assert result["isError"] is True
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_create_with_structured_workout_serialises(self):
        """Raw structured_workout should be serialized unchanged for POST."""
        structured_workout = {
            "structure": [],
            "polyline": [],
            "primaryLengthMetric": "duration",
            "primaryIntensityMetric": "percentOfFtp",
            "primaryIntensityTargetOrRange": "range",
        }
        create_response = APIResponse(
            success=True,
            data={"workoutId": 7005, "title": "Structured Raw", "workoutDay": "2026-03-01T00:00:00"},
        )

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.post = AsyncMock(return_value=create_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_create_workout(
                date_str="2026-03-01",
                sport="Bike",
                title="Structured Raw",
                structured_workout=structured_workout,
            )

        assert result["success"] is True
        payload = mock_instance.post.call_args[1]["json"]
        assert json.loads(payload["structure"]) == structured_workout

    @pytest.mark.asyncio
    async def test_create_rejects_both_structure_inputs(self):
        """Simplified structure and raw structured_workout cannot be combined."""
        result = await tp_create_workout(
            date_str="2026-03-01",
            sport="Bike",
            title="Conflict",
            structure={"steps": [{"name": "WU", "duration_seconds": 60, "intensity_min": 50, "intensity_max": 60}]},
            structured_workout={"structure": []},
        )
        assert result["isError"] is True
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Provide only one of structure or structured_workout" in result["message"]

    @pytest.mark.asyncio
    async def test_create_rejects_invalid_structured_workout(self):
        """Raw structured_workout should be validated before POST."""
        result = await tp_create_workout(
            date_str="2026-03-01",
            sport="Bike",
            title="Bad Structured Raw",
            structured_workout={"structure": []},
        )
        assert result["isError"] is True
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "structured_workout" in result["message"]


class TestCreatePoolSwimWithDistance:
    """Tests for distance-based (pool) swim creation via simplified structure."""

    @pytest.mark.asyncio
    async def test_full_pool_set_creates_without_error(self):
        """200 WU / 20x25 / 14x50 / 400 CD in yards round-trips to a meter wire."""
        structure = {
            "length_unit": "yard",
            "primaryIntensityMetric": "percentOfThresholdPace",
            "steps": [
                {"name": "Warm Up", "distance_yards": 200, "intensity_min": 0,
                 "intensity_max": 0, "intensityClass": "warmUp"},
                {"type": "repetition", "reps": 20, "steps": [
                    {"name": "25 free", "distance_yards": 25, "intensity_min": 0, "intensity_max": 0},
                    {"name": "Rest", "duration_seconds": 15, "intensityClass": "rest",
                     "intensity_min": 0, "intensity_max": 0},
                ]},
                {"type": "repetition", "reps": 14, "steps": [
                    {"name": "50 free", "distance_yards": 50, "intensity_min": 0, "intensity_max": 0},
                    {"name": "Rest", "duration_seconds": 20, "intensityClass": "rest",
                     "intensity_min": 0, "intensity_max": 0},
                ]},
                {"name": "Cool Down", "distance_yards": 400, "intensity_min": 0,
                 "intensity_max": 0, "intensityClass": "coolDown"},
            ],
        }
        create_response = APIResponse(
            success=True,
            data={"workoutId": 8001, "title": "Pool Set", "workoutDay": "2026-03-01T00:00:00"},
        )

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.post = AsyncMock(return_value=create_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_create_workout(
                date_str="2026-03-01", sport="Swim", title="Pool Set",
                structure=structure,
            )

        assert result["success"] is True
        payload = mock_instance.post.call_args[1]["json"]
        wire = json.loads(payload["structure"])
        assert wire["primaryLengthMetric"] == "distance"
        assert wire["primaryIntensityTargetOrRange"] == "range"
        assert wire["visualizationDistanceUnit"] == "yard"
        # Never store yards; all lengths must be meter or second.
        for block in wire["structure"]:
            for s in block["steps"]:
                assert s["length"]["unit"] in ("meter", "second")
        assert "yard" not in json.dumps(wire["structure"]).lower()
        # No auto duration/TSS/IF derived for distance workouts.
        assert "totalTimePlanned" not in payload
        assert "tssPlanned" not in payload
        assert "ifPlanned" not in payload

    @pytest.mark.asyncio
    async def test_meter_pool_set_creates(self):
        """Metre-authored swim stores metres and displays in metres."""
        structure = {
            "steps": [
                {"name": "100m", "distance_meters": 100, "intensity_min": 0, "intensity_max": 0},
            ],
        }
        create_response = APIResponse(
            success=True, data={"workoutId": 8002, "title": "Meters", "workoutDay": "2026-03-01T00:00:00"},
        )

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.post = AsyncMock(return_value=create_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_create_workout(
                date_str="2026-03-01", sport="Swim", title="Meters", structure=structure,
            )

        assert result["success"] is True
        wire = json.loads(mock_instance.post.call_args[1]["json"]["structure"])
        assert wire["visualizationDistanceUnit"] == "meter"
        assert wire["structure"][0]["steps"][0]["length"] == {"value": 100.0, "unit": "meter"}


class TestStructuredWorkoutPassthroughSanitization:
    """Tests for native structured_workout sanitization (rule #4)."""

    @pytest.mark.asyncio
    async def test_injects_primary_intensity_target_or_range(self):
        """Missing primaryIntensityTargetOrRange is auto-injected as 'range'."""
        structured_workout = {
            "structure": [],
            "polyline": [],
            "primaryLengthMetric": "distance",
            "primaryIntensityMetric": "percentOfThresholdPace",
        }
        create_response = APIResponse(
            success=True, data={"workoutId": 8003, "title": "Raw", "workoutDay": "2026-03-01T00:00:00"},
        )

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.post = AsyncMock(return_value=create_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_create_workout(
                date_str="2026-03-01", sport="Swim", title="Raw",
                structured_workout=structured_workout,
            )

        assert result["success"] is True
        wire = json.loads(mock_instance.post.call_args[1]["json"]["structure"])
        assert wire["primaryIntensityTargetOrRange"] == "range"

    @pytest.mark.asyncio
    async def test_converts_yard_units_and_sets_display_unit(self):
        """Yard length units in a raw payload are converted to metres."""
        structured_workout = {
            "structure": [
                {"type": "step", "length": {"value": 1, "unit": "repetition"},
                 "begin": 0, "end": 22.86, "steps": [
                     {"name": "25 free", "type": "step",
                      "length": {"value": 25, "unit": "yard"},
                      "targets": [{"minValue": 0, "maxValue": 0}],
                      "intensityClass": "active", "openDuration": False},
                 ]},
            ],
            "polyline": [],
            "primaryLengthMetric": "distance",
            "primaryIntensityMetric": "percentOfThresholdPace",
        }
        create_response = APIResponse(
            success=True, data={"workoutId": 8004, "title": "Raw Yards", "workoutDay": "2026-03-01T00:00:00"},
        )

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.post = AsyncMock(return_value=create_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_create_workout(
                date_str="2026-03-01", sport="Swim", title="Raw Yards",
                structured_workout=structured_workout,
            )

        assert result["success"] is True
        wire = json.loads(mock_instance.post.call_args[1]["json"]["structure"])
        step_length = wire["structure"][0]["steps"][0]["length"]
        assert step_length["unit"] == "meter"
        assert step_length["value"] == pytest.approx(22.86)
        assert wire["visualizationDistanceUnit"] == "yard"
        assert "yard" not in json.dumps(wire["structure"]).lower()

    @pytest.mark.asyncio
    async def test_does_not_override_existing_visualization_unit(self):
        """An explicit visualizationDistanceUnit is preserved."""
        structured_workout = {
            "structure": [
                {"type": "step", "length": {"value": 1, "unit": "repetition"},
                 "steps": [{"name": "x", "length": {"value": 25, "unit": "yards"}}]},
            ],
            "polyline": [],
            "primaryLengthMetric": "distance",
            "primaryIntensityMetric": "percentOfThresholdPace",
            "primaryIntensityTargetOrRange": "range",
            "visualizationDistanceUnit": "meter",
        }
        create_response = APIResponse(
            success=True, data={"workoutId": 8005, "title": "Keep Unit", "workoutDay": "2026-03-01T00:00:00"},
        )

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.post = AsyncMock(return_value=create_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_create_workout(
                date_str="2026-03-01", sport="Swim", title="Keep Unit",
                structured_workout=structured_workout,
            )

        assert result["success"] is True
        wire = json.loads(mock_instance.post.call_args[1]["json"]["structure"])
        assert wire["visualizationDistanceUnit"] == "meter"
        assert wire["structure"][0]["steps"][0]["length"]["unit"] == "meter"

    @pytest.mark.asyncio
    async def test_does_not_mutate_caller_dict(self):
        """Sanitization must not mutate the caller's structured_workout."""
        structured_workout = {
            "structure": [{"type": "step", "steps": [{"length": {"value": 25, "unit": "yard"}}]}],
            "polyline": [],
            "primaryLengthMetric": "distance",
            "primaryIntensityMetric": "percentOfThresholdPace",
        }
        original = json.loads(json.dumps(structured_workout))
        create_response = APIResponse(
            success=True, data={"workoutId": 8006, "title": "Immutable", "workoutDay": "2026-03-01T00:00:00"},
        )

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.post = AsyncMock(return_value=create_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            await tp_create_workout(
                date_str="2026-03-01", sport="Swim", title="Immutable",
                structured_workout=structured_workout,
            )

        assert structured_workout == original


class TestUpdateWorkout:
    """Tests for tp_update_workout."""

    @pytest.mark.asyncio
    async def test_update_merges_with_existing(self):
        """Update should GET existing, merge updates, then PUT."""
        existing = {
            "workoutId": 1001,
            "title": "Original",
            "workoutDay": "2026-03-01T00:00:00",
            "workoutTypeFamilyId": 3,
            "workoutTypeValueId": 3,
        }
        get_response = APIResponse(success=True, data=existing)
        put_response = APIResponse(success=True, data=None)

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=get_response)
            mock_instance.put = AsyncMock(return_value=put_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_update_workout(workout_id="1001", title="Updated Title")

        assert result["success"] is True
        put_payload = mock_instance.put.call_args[1]["json"]
        assert put_payload["title"] == "Updated Title"
        # Original fields preserved
        assert put_payload["workoutTypeFamilyId"] == 3

    @pytest.mark.asyncio
    async def test_update_nutrition_replaces_prior_section(self):
        """Updating nutrition should append under the header and replace any prior nutrition block."""
        existing = {
            "workoutId": 1001,
            "title": "Original",
            "workoutDay": "2026-03-01T00:00:00",
            "workoutTypeFamilyId": 3,
            "workoutTypeValueId": 3,
            "description": "Tempo run.\n\n\n-----Nutrition-----\nOld plan",
        }
        get_response = APIResponse(success=True, data=existing)
        put_response = APIResponse(success=True, data=None)

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=get_response)
            mock_instance.put = AsyncMock(return_value=put_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_update_workout(workout_id="1001", nutrition="New plan")

        assert result["success"] is True
        put_payload = mock_instance.put.call_args[1]["json"]
        assert put_payload["description"] == "Tempo run.\n\n\n-----Nutrition-----\nNew plan"

    @pytest.mark.asyncio
    async def test_update_keeps_hidden(self):
        """The TrainingPeaks isHidden field should be preserved if not explicitly updated."""
        existing = {
            "workoutId": 1001,
            "title": "Original",
            "workoutDay": "2026-03-01T00:00:00",
            "workoutTypeFamilyId": 3,
            "workoutTypeValueId": 3,
            "isHidden": False,
        }
        get_response = APIResponse(success=True, data=existing)
        put_response = APIResponse(success=True, data=None)

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=get_response)
            mock_instance.put = AsyncMock(return_value=put_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_update_workout(workout_id="1001", sport="Bike")

        assert result["success"] is True
        put_payload = mock_instance.put.call_args[1]["json"]
        assert put_payload["workoutTypeFamilyId"] == 2
        assert put_payload["workoutTypeValueId"] == 2
        assert put_payload["isHidden"] is False

    @pytest.mark.asyncio
    async def test_update_hidden(self):
        """is_hidden should update the TrainingPeaks isHidden field."""
        existing = {
            "workoutId": 1001,
            "title": "Original",
            "workoutDay": "2026-03-01T00:00:00",
            "workoutTypeFamilyId": 3,
            "workoutTypeValueId": 3,
            "isHidden": False,
        }
        get_response = APIResponse(success=True, data=existing)
        put_response = APIResponse(success=True, data=None)

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=get_response)
            mock_instance.put = AsyncMock(return_value=put_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_update_workout(workout_id="1001", is_hidden=True)

        assert result["success"] is True
        put_payload = mock_instance.put.call_args[1]["json"]
        assert put_payload["isHidden"] is True

    @pytest.mark.asyncio
    async def test_update_sport_changes_ids(self):
        """Changing sport should update family and type IDs."""
        existing = {
            "workoutId": 1001,
            "workoutTypeFamilyId": 3,
            "workoutTypeValueId": 3,
        }
        get_response = APIResponse(success=True, data=existing)
        put_response = APIResponse(success=True, data=None)

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=get_response)
            mock_instance.put = AsyncMock(return_value=put_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_update_workout(workout_id="1001", sport="Bike")

        assert result["success"] is True
        put_payload = mock_instance.put.call_args[1]["json"]
        assert put_payload["workoutTypeFamilyId"] == 2
        assert put_payload["workoutTypeValueId"] == 2

    @pytest.mark.asyncio
    async def test_update_allows_zero_rpe(self):
        """RPE 0 should be accepted when updating workouts."""
        existing = {"workoutId": 1001, "title": "Recovery"}
        get_response = APIResponse(success=True, data=existing)
        put_response = APIResponse(success=True, data=None)

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=get_response)
            mock_instance.put = AsyncMock(return_value=put_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_update_workout(workout_id="1001", rpe=0)

        assert result["success"] is True
        put_payload = mock_instance.put.call_args[1]["json"]
        assert put_payload["rpe"] == 0

    @pytest.mark.asyncio
    async def test_update_date_only_sets_midnight(self):
        """Date-only updates should keep the existing midnight behavior."""
        existing = {"workoutId": 1001, "workoutDay": "2026-03-01T00:00:00"}
        get_response = APIResponse(success=True, data=existing)
        put_response = APIResponse(success=True, data=None)

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=get_response)
            mock_instance.put = AsyncMock(return_value=put_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_update_workout(workout_id="1001", date="2026-04-14")

        assert result["success"] is True
        put_payload = mock_instance.put.call_args[1]["json"]
        assert put_payload["workoutDay"] == "2026-04-14T00:00:00"

    @pytest.mark.asyncio
    async def test_update_date_only_preserves_existing_planned_start_time(self):
        """Date-only updates should move an existing planned start to the new day."""
        existing = {
            "workoutId": 1001,
            "workoutDay": "2026-03-01T00:00:00",
            "startTimePlanned": "2026-03-01T16:45:00",
        }
        get_response = APIResponse(success=True, data=existing)
        put_response = APIResponse(success=True, data=None)

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=get_response)
            mock_instance.put = AsyncMock(return_value=put_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_update_workout(workout_id="1001", date="2026-04-14")

        assert result["success"] is True
        put_payload = mock_instance.put.call_args[1]["json"]
        assert put_payload["workoutDay"] == "2026-04-14T00:00:00"
        assert put_payload["startTimePlanned"] == "2026-04-14T16:45:00"

    @pytest.mark.asyncio
    async def test_update_datetime_preserves_time(self):
        """Datetime updates should forward the scheduled time to TrainingPeaks."""
        existing = {
            "workoutId": 1001,
            "workoutDay": "2026-03-01T00:00:00",
            "startTimePlanned": "2026-03-01T09:30:00",
        }
        get_response = APIResponse(success=True, data=existing)
        put_response = APIResponse(success=True, data=None)

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=get_response)
            mock_instance.put = AsyncMock(return_value=put_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_update_workout(workout_id="1001", date="2026-04-14T16:45:00")

        assert result["success"] is True
        put_payload = mock_instance.put.call_args[1]["json"]
        assert put_payload["workoutDay"] == "2026-04-14T00:00:00"
        assert put_payload["startTimePlanned"] == "2026-04-14T16:45:00"

    @pytest.mark.asyncio
    async def test_update_with_structure_serialises(self):
        """Simplified structure should be converted to TP wire format for PUT."""
        existing = {"workoutId": 1001}
        get_response = APIResponse(success=True, data=existing)
        put_response = APIResponse(success=True, data=None)
        structure = {
            "primaryIntensityMetric": "percentOfFtp",
            "steps": [
                {
                    "name": "WU",
                    "duration_seconds": 600,
                    "intensity_min": 40,
                    "intensity_max": 55,
                    "intensityClass": "warmUp",
                },
                {
                    "name": "Main",
                    "duration_seconds": 1200,
                    "intensity_min": 85,
                    "intensity_max": 95,
                    "intensityClass": "active",
                },
                {
                    "name": "CD",
                    "duration_seconds": 600,
                    "intensity_min": 40,
                    "intensity_max": 55,
                    "intensityClass": "coolDown",
                },
            ],
        }

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=get_response)
            mock_instance.put = AsyncMock(return_value=put_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_update_workout(
                workout_id="1001",
                structure=structure,
            )

        assert result["success"] is True
        put_payload = mock_instance.put.call_args[1]["json"]
        assert isinstance(put_payload["structure"], str)
        parsed = json.loads(put_payload["structure"])
        assert "structure" in parsed
        assert "polyline" in parsed
        assert abs(put_payload["totalTimePlanned"] - 40.0 / 60.0) < 0.01
        # Exact IF/TSS values with swap guard (issue #41)
        assert put_payload["tssPlanned"] == pytest.approx(39.6, abs=0.1)
        assert put_payload["ifPlanned"] == pytest.approx(0.771, abs=0.001)
        assert put_payload["ifPlanned"] < 1
        assert put_payload["tssPlanned"] > 1

    @pytest.mark.asyncio
    async def test_update_with_structure_explicit_duration_and_tss_override(self):
        """Explicit duration and TSS should win over derived structure values."""
        existing = {
            "workoutId": 1001,
            "ifPlanned": 0.91,
        }
        get_response = APIResponse(success=True, data=existing)
        put_response = APIResponse(success=True, data=None)
        structure = {
            "steps": [
                {
                    "name": "WU",
                    "duration_seconds": 600,
                    "intensity_min": 50,
                    "intensity_max": 60,
                    "intensityClass": "warmUp",
                },
            ],
        }

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=get_response)
            mock_instance.put = AsyncMock(return_value=put_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_update_workout(
                workout_id="1001",
                duration_minutes=90,
                tss_planned=77,
                structure=structure,
            )

        assert result["success"] is True
        put_payload = mock_instance.put.call_args[1]["json"]
        assert put_payload["totalTimePlanned"] == 90 / 60.0
        assert put_payload["tssPlanned"] == 77
        assert "ifPlanned" not in put_payload

    @pytest.mark.asyncio
    async def test_update_with_invalid_structure_returns_validation_error(self):
        """Invalid simplified structure should fail before PUT."""
        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock()
            mock_instance.put = AsyncMock()
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_update_workout(
                workout_id="1001",
                structure={"structure": [{"type": "step"}]},
            )

        assert result["isError"] is True
        assert result["error_code"] == "VALIDATION_ERROR"
        mock_instance.get.assert_not_called()
        mock_instance.put.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_with_structured_workout_serialises(self):
        """Raw structured_workout should be serialized unchanged for PUT."""
        existing = {"workoutId": 1001}
        get_response = APIResponse(success=True, data=existing)
        put_response = APIResponse(success=True, data=None)
        structured_workout = {
            "structure": [],
            "polyline": [],
            "primaryLengthMetric": "duration",
            "primaryIntensityMetric": "percentOfFtp",
            "primaryIntensityTargetOrRange": "range",
        }

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=get_response)
            mock_instance.put = AsyncMock(return_value=put_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_update_workout(
                workout_id="1001",
                structured_workout=structured_workout,
            )

        assert result["success"] is True
        put_payload = mock_instance.put.call_args[1]["json"]
        assert json.loads(put_payload["structure"]) == structured_workout

    @pytest.mark.asyncio
    async def test_update_rejects_both_structure_inputs(self):
        """Simplified structure and raw structured_workout cannot be combined."""
        result = await tp_update_workout(
            workout_id="1001",
            structure={"steps": [{"name": "WU", "duration_seconds": 60, "intensity_min": 50, "intensity_max": 60}]},
            structured_workout={"structure": []},
        )
        assert result["isError"] is True
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "Provide only one of structure or structured_workout" in result["message"]

    @pytest.mark.asyncio
    async def test_update_rejects_invalid_structured_workout(self):
        """Raw structured_workout should be validated before PUT."""
        result = await tp_update_workout(
            workout_id="1001",
            structured_workout={"structure": []},
        )
        assert result["isError"] is True
        assert result["error_code"] == "VALIDATION_ERROR"
        assert "structured_workout" in result["message"]


class TestDeleteWorkout:
    """Tests for tp_delete_workout."""

    @pytest.mark.asyncio
    async def test_delete_success(self):
        delete_response = APIResponse(success=True, data=None)

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.delete = AsyncMock(return_value=delete_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_delete_workout("1001")

        assert result["success"] is True
        mock_instance.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_invalid_id(self):
        result = await tp_delete_workout("abc")
        assert result["isError"] is True
        assert result["error_code"] == "VALIDATION_ERROR"


class TestCopyWorkout:
    """Tests for tp_copy_workout."""

    @pytest.mark.asyncio
    async def test_copy_preserves_structure(self):
        """Copy should preserve structure, description, sport type."""
        source = {
            "workoutId": 1001,
            "title": "Source Workout",
            "workoutTypeFamilyId": 2,
            "workoutTypeValueId": 2,
            "totalTimePlanned": 1.5,
            "tssPlanned": 80,
            "description": "Test desc",
            "coachComments": "Coach says...",
            "structure": '{"structure": []}',
        }
        get_response = APIResponse(success=True, data=source)
        post_response = APIResponse(success=True, data={"workoutId": 2001, "title": "Source Workout"})

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=get_response)
            mock_instance.post = AsyncMock(return_value=post_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_copy_workout("1001", "2026-04-01")

        assert result["success"] is True
        assert result["copied_from"] == 1001
        payload = mock_instance.post.call_args[1]["json"]
        assert payload["workoutDay"] == "2026-04-01T00:00:00"
        assert payload["workoutTypeFamilyId"] == 2
        assert payload["totalTimePlanned"] == 1.5
        assert payload["tssPlanned"] == 80
        assert payload["description"] == "Test desc"
        assert payload["coachComments"] == "Coach says..."
        assert "structure" in payload

    @pytest.mark.asyncio
    async def test_copy_does_not_copy_actual_data(self):
        """Copy should not include actual/completed data."""
        source = {
            "workoutId": 1001,
            "title": "Done",
            "workoutTypeFamilyId": 3,
            "workoutTypeValueId": 3,
            "totalTimePlanned": 1.0,
            "totalTime": 0.95,
            "tssActual": 75,
            "completed": True,
        }
        get_response = APIResponse(success=True, data=source)
        post_response = APIResponse(success=True, data={"workoutId": 2002})

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=get_response)
            mock_instance.post = AsyncMock(return_value=post_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_copy_workout("1001", "2026-04-01")

        assert result["success"] is True
        payload = mock_instance.post.call_args[1]["json"]
        assert "totalTime" not in payload
        assert "tssActual" not in payload
        assert "completed" not in payload

    @pytest.mark.asyncio
    async def test_copy_shifts_start_time_planned_to_target_date(self):
        """Copy should shift startTimePlanned to the target date, preserving time-of-day."""
        source = {
            "workoutId": 1001,
            "title": "Morning Ride",
            "workoutTypeFamilyId": 2,
            "workoutTypeValueId": 2,
            "startTimePlanned": "2026-03-15T07:30:00",
        }
        get_response = APIResponse(success=True, data=source)
        post_response = APIResponse(success=True, data={"workoutId": 2004, "title": "Morning Ride"})

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=get_response)
            mock_instance.post = AsyncMock(return_value=post_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_copy_workout("1001", "2026-04-01")

        assert result["success"] is True
        payload = mock_instance.post.call_args[1]["json"]
        assert payload["startTimePlanned"] == "2026-04-01T07:30:00"

    @pytest.mark.asyncio
    async def test_copy_without_start_time_planned_omits_field(self):
        """Copy should not set startTimePlanned when source has none."""
        source = {
            "workoutId": 1001,
            "title": "No Time Workout",
            "workoutTypeFamilyId": 2,
            "workoutTypeValueId": 2,
        }
        get_response = APIResponse(success=True, data=source)
        post_response = APIResponse(success=True, data={"workoutId": 2005, "title": "No Time Workout"})

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=get_response)
            mock_instance.post = AsyncMock(return_value=post_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_copy_workout("1001", "2026-04-01")

        assert result["success"] is True
        payload = mock_instance.post.call_args[1]["json"]
        assert "startTimePlanned" not in payload

    @pytest.mark.asyncio
    async def test_copy_preserves_raw_start_time_planned_on_parse_failure(self):
        """When startTimePlanned cannot be parsed, raw value is preserved rather than silently dropped."""
        source = {
            "workoutId": 1001,
            "title": "Weird Time Workout",
            "workoutTypeFamilyId": 2,
            "workoutTypeValueId": 2,
            "startTimePlanned": "not-a-datetime",
        }
        get_response = APIResponse(success=True, data=source)
        post_response = APIResponse(success=True, data={"workoutId": 2006, "title": "Weird Time Workout"})

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=get_response)
            mock_instance.post = AsyncMock(return_value=post_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_copy_workout("1001", "2026-04-01")

        assert result["success"] is True
        payload = mock_instance.post.call_args[1]["json"]
        # Raw value preserved, not silently dropped
        assert payload["startTimePlanned"] == "not-a-datetime"

    @pytest.mark.asyncio
    async def test_copy_preserves_utc_offset_in_start_time_planned(self):
        """When startTimePlanned carries a UTC offset, the offset is preserved on the new date.

        The TP API returns naive datetimes in practice, so DST re-localisation is not
        performed. This test documents the intended fixed-offset behaviour.
        """
        source = {
            "workoutId": 1001,
            "title": "Offset Workout",
            "workoutTypeFamilyId": 2,
            "workoutTypeValueId": 2,
            "startTimePlanned": "2026-03-15T07:30:00+02:00",
        }
        get_response = APIResponse(success=True, data=source)
        post_response = APIResponse(success=True, data={"workoutId": 2007, "title": "Offset Workout"})

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=get_response)
            mock_instance.post = AsyncMock(return_value=post_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_copy_workout("1001", "2026-04-01")

        assert result["success"] is True
        payload = mock_instance.post.call_args[1]["json"]
        # Date shifted; fixed offset carried over (TP API uses naive times so no DST risk)
        assert payload["startTimePlanned"] == "2026-04-01T07:30:00+02:00"

    @pytest.mark.asyncio
    async def test_copy_with_title_override(self):
        source = {"workoutId": 1001, "title": "Old", "workoutTypeFamilyId": 3, "workoutTypeValueId": 3}
        get_response = APIResponse(success=True, data=source)
        post_response = APIResponse(success=True, data={"workoutId": 2003, "title": "New Title"})

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=get_response)
            mock_instance.post = AsyncMock(return_value=post_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            await tp_copy_workout("1001", "2026-04-01", title="New Title")

        payload = mock_instance.post.call_args[1]["json"]
        assert payload["title"] == "New Title"


class TestWorkoutComments:
    """Tests for workout comment tools."""

    @pytest.mark.asyncio
    async def test_get_comments_success(self):
        comments_data = [
            {"id": 1, "comment": "Great workout!", "isCoach": True},
            {"id": 2, "comment": "Thanks coach", "isCoach": False},
        ]
        response = APIResponse(success=True, data={"workoutId": 1001, "workoutComments": comments_data})

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_get_workout_comments("1001")

        assert result["count"] == 2
        assert len(result["comments"]) == 2
        mock_instance.get.assert_called_once_with("/fitness/v6/athletes/123/workouts/1001")

    @pytest.mark.asyncio
    async def test_get_comments_empty(self):
        response = APIResponse(success=True, data={"workoutId": 1001, "workoutComments": []})

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_get_workout_comments("1001")

        assert result["count"] == 0
        assert "No comments" in result.get("message", "")

    @pytest.mark.asyncio
    async def test_add_comment_success(self):
        post_response = APIResponse(success=True, data=None)
        comments_data = [{"id": 1, "comment": "Nice ride!", "isCoach": False}]
        get_response = APIResponse(
            success=True, data={"workoutId": 1001, "workoutComments": comments_data}
        )

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.post = AsyncMock(return_value=post_response)
            mock_instance.get = AsyncMock(return_value=get_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_add_workout_comment("1001", "Nice ride!")

        assert result["success"] is True
        assert result["count"] == 1
        assert result["comments"] == comments_data
        payload = mock_instance.post.call_args[1]["json"]
        assert payload["value"] == "Nice ride!"
        mock_instance.get.assert_called_once_with("/fitness/v6/athletes/123/workouts/1001")

    @pytest.mark.asyncio
    async def test_add_comment_get_fails_gracefully(self):
        post_response = APIResponse(success=True, data=None)
        get_response = APIResponse(success=False, error_code=ErrorCode.API_ERROR, message="timeout")

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.post = AsyncMock(return_value=post_response)
            mock_instance.get = AsyncMock(return_value=get_response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_add_workout_comment("1001", "Nice ride!")

        assert result["success"] is True
        assert result["comments"] == []
        assert result["count"] == 0
        assert result["comments_fetch_failed"] is True

    @pytest.mark.asyncio
    async def test_add_empty_comment_rejected(self):
        result = await tp_add_workout_comment("1001", "")
        assert result["isError"] is True
        assert result["error_code"] == "VALIDATION_ERROR"


class TestWorkoutNote:
    """Tests for tp_get_workout_note and tp_set_workout_note."""

    @pytest.mark.asyncio
    async def test_get_note_success(self):
        response = APIResponse(
            success=True,
            data={"note": "Felt strong today", "dateTimeUpdatedUtc": "2026-03-01T10:00:00Z"},
        )

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_get_workout_note("1001")

        assert result["note"] == "Felt strong today"
        assert result["workout_id"] == "1001"
        assert result["updated_at"] == "2026-03-01T10:00:00Z"
        mock_instance.get.assert_called_once_with("/fitness/v6/workouts/1001/privateWorkoutNote")

    @pytest.mark.asyncio
    async def test_get_note_empty(self):
        response = APIResponse(success=True, data={})

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_get_workout_note("1001")

        assert result["note"] == ""
        assert result["updated_at"] is None

    @pytest.mark.asyncio
    async def test_get_note_auth_failure(self):
        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=None)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_get_workout_note("1001")

        assert result["isError"] is True
        assert result["error_code"] == "AUTH_INVALID"

    @pytest.mark.asyncio
    async def test_get_note_api_error(self):
        response = APIResponse(success=False, error_code=ErrorCode.API_ERROR, message="Server error")

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get = AsyncMock(return_value=response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_get_workout_note("1001")

        assert result["isError"] is True
        assert result["error_code"] == "API_ERROR"

    @pytest.mark.asyncio
    async def test_get_note_invalid_id(self):
        result = await tp_get_workout_note("abc")
        assert result["isError"] is True
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_set_note_success(self):
        response = APIResponse(success=True, data=None)

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.put = AsyncMock(return_value=response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_set_workout_note("1001", "Hard session, legs heavy")

        assert result["success"] is True
        assert result["note"] == "Hard session, legs heavy"
        assert result["workout_id"] == "1001"
        mock_instance.put.assert_called_once_with(
            "/fitness/v6/workouts/1001/privateWorkoutNote",
            json={"note": "Hard session, legs heavy"},
        )

    @pytest.mark.asyncio
    async def test_set_note_clear(self):
        response = APIResponse(success=True, data=None)

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.put = AsyncMock(return_value=response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_set_workout_note("1001", "")

        assert result["success"] is True
        assert result["note"] == ""
        payload = mock_instance.put.call_args[1]["json"]
        assert payload["note"] == ""

    @pytest.mark.asyncio
    async def test_set_note_auth_failure(self):
        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=None)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_set_workout_note("1001", "some note")

        assert result["isError"] is True
        assert result["error_code"] == "AUTH_INVALID"

    @pytest.mark.asyncio
    async def test_set_note_api_error(self):
        response = APIResponse(success=False, error_code=ErrorCode.API_ERROR, message="Server error")

        with patch("tp_mcp.tools.workouts.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.put = AsyncMock(return_value=response)
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_set_workout_note("1001", "some note")

        assert result["isError"] is True
        assert result["error_code"] == "API_ERROR"

    @pytest.mark.asyncio
    async def test_set_note_invalid_id(self):
        result = await tp_set_workout_note("abc", "note")
        assert result["isError"] is True
        assert result["error_code"] == "VALIDATION_ERROR"
