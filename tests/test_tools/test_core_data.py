"""Tests for tp_get_core_data and the base64 mode of tp_download_workout_file.

FIT fixtures are encoded inline so the tests exercise the real fitdecode path,
including Connect IQ developer fields, rather than mocking the parser out.
"""

import base64
import gzip
import struct
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from tp_mcp.client.http import APIResponse, ErrorCode, RawResponse
from tp_mcp.tools.core_data import _classify_dev_field, tp_get_core_data
from tp_mcp.tools.workout_files import MAX_INLINE_FILE_BYTES, tp_download_workout_file

# ---------------------------------------------------------------------------
# Minimal FIT encoder
# ---------------------------------------------------------------------------

_CRC_TABLE = (
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
)

# FIT timestamps are seconds since 1989-12-31T00:00:00Z.
FIT_EPOCH = int(datetime(1989, 12, 31, tzinfo=timezone.utc).timestamp())
# Offset into a real year, so fitdecode decodes timestamps as datetimes rather
# than leaving them as raw "system time" ints.
_BASE_TS = FIT_EPOCH + 1_100_000_000

UINT8, UINT16, UINT32, STRING, BYTE, FLOAT32 = 0x02, 0x84, 0x86, 0x07, 0x0D, 0x88
_STRUCT_FMT = {UINT8: "<B", UINT16: "<H", UINT32: "<I", FLOAT32: "<f"}

CORE_TEMP_INVALID = 0xFFFF


def _crc(data: bytes) -> int:
    crc = 0
    for byte in data:
        for nibble in (byte & 0x0F, (byte >> 4) & 0x0F):
            tmp = _CRC_TABLE[crc & 0xF]
            crc = (crc >> 4) & 0x0FFF
            crc = crc ^ tmp ^ _CRC_TABLE[nibble]
    return crc


def _def_msg(local, global_num, fields, dev_fields=()):
    header = 0x40 | local | (0x20 if dev_fields else 0)
    out = bytes([header, 0, 0]) + struct.pack("<H", global_num) + bytes([len(fields)])
    for num, size, base in fields:
        out += bytes([num, size, base])
    if dev_fields:
        out += bytes([len(dev_fields)])
        for num, size, dev_idx in dev_fields:
            out += bytes([num, size, dev_idx])
    return out


def _pack(base, size, value):
    if base == STRING:
        return (value.encode() + b"\x00").ljust(size, b"\x00")[:size]
    if base == BYTE:
        return bytes(value).ljust(size, b"\x00")[:size]
    return struct.pack(_STRUCT_FMT[base], value)


def _data_msg(local, values):
    return bytes([local]) + b"".join(_pack(base, size, value) for base, size, value in values)


def build_fit(records, dev_fields=()):
    """Encode a FIT file with record messages.

    records: dicts with "ts" (epoch seconds), optional "core" (native
    core_temperature in C), and one entry per developer field name.
    dev_fields: (field_def_num, name, units) tuples, written as float32.
    """
    body = _def_msg(0, 0, [(0, 1, UINT8)]) + _data_msg(0, [(UINT8, 1, 4)])

    body += _def_msg(1, 207, [(1, 16, BYTE), (3, 1, UINT8)])
    body += _data_msg(1, [(BYTE, 16, bytes(range(16))), (UINT8, 1, 0)])

    if dev_fields:
        body += _def_msg(2, 206, [(0, 1, UINT8), (1, 1, UINT8), (2, 1, UINT8), (3, 32, STRING), (8, 16, STRING)])
        for num, name, units in dev_fields:
            body += _data_msg(
                2,
                [(UINT8, 1, 0), (UINT8, 1, num), (UINT8, 1, FLOAT32), (STRING, 32, name), (STRING, 16, units)],
            )

    body += _def_msg(
        3,
        20,
        [(253, 4, UINT32), (139, 2, UINT16)],
        [(num, 4, 0) for num, _, _ in dev_fields],
    )
    for rec in records:
        core = rec.get("core")
        values = [
            (UINT32, 4, int(rec["ts"] - FIT_EPOCH)),
            (UINT16, 2, CORE_TEMP_INVALID if core is None else int(round(core * 100))),
        ]
        values += [(FLOAT32, 4, float(rec[name])) for _, name, _ in dev_fields]
        body += _data_msg(3, values)

    header = struct.pack("<BBHI4s", 12, 0x20, 2100, len(body), b".FIT")
    return header + body + struct.pack("<H", _crc(header + body))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CORE_DEV_FIELDS = (
    (0, "core_temperature", "C"),
    (1, "skin_temperature", "C"),
    (2, "Heat Strain Index", ""),
    (3, "core_data_quality", ""),
)


def _details_response(file_infos):
    return APIResponse(success=True, data={"workoutDeviceFileInfos": file_infos})


def _patched_client(details, raw):
    """Patch TPClient so details/raw file responses are served from fixtures."""
    mock_instance = AsyncMock()
    mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
    mock_instance.get = AsyncMock(return_value=details)
    mock_instance.get_raw = AsyncMock(return_value=raw)
    return mock_instance


def _raw_gzip(fit_bytes, content_type="application/gzip"):
    return RawResponse(success=True, content=gzip.compress(fit_bytes), content_type=content_type)


# ---------------------------------------------------------------------------
# Developer field classification
# ---------------------------------------------------------------------------


class TestClassifyDevField:
    """Developer field names are matched on substrings, not exact names."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("core_temperature", "core"),
            ("CORE Temp", "core"),
            ("core_body_temp", "core"),
            ("skin_temperature", "skin"),
            ("Skin Temp", "skin"),
            ("hsi", "hsi"),
            ("HSI", "hsi"),
            ("heat_strain_index", "hsi"),
            ("Heat Strain", "hsi"),
            # "core_data_quality" contains "core" but is a quality score.
            ("core_data_quality", "quality"),
            ("Core Quality", "quality"),
            ("cadence", None),
            ("power", None),
        ],
    )
    def test_classification(self, name, expected):
        assert _classify_dev_field(name) == expected


# ---------------------------------------------------------------------------
# tp_get_core_data
# ---------------------------------------------------------------------------


class TestGetCoreData:
    """End-to-end extraction from a device FIT file."""

    @pytest.mark.asyncio
    async def test_extracts_core_skin_and_hsi(self):
        records = [
            {
                "ts": _BASE_TS + i,
                "core": 37.0 + i * 0.1,
                "core_temperature": 36.0,  # developer field, ignored in favour of native
                "skin_temperature": 33.0 + i * 0.1,
                "Heat Strain Index": float(i),
                "core_data_quality": 3.0,
            }
            for i in range(10)
        ]
        fit = build_fit(records, CORE_DEV_FIELDS)

        with patch("tp_mcp.tools.core_data.TPClient") as mock_client:
            mock_instance = _patched_client(
                _details_response([{"fileId": 55, "fileName": "ride.fit.gz"}]),
                _raw_gzip(fit),
            )
            mock_client.return_value.__aenter__.return_value = mock_instance
            result = await tp_get_core_data(workout_id="9001")

        assert "isError" not in result
        assert result["workout_id"] == "9001"
        assert result["file_name"] == "ride.fit.gz"
        # Every developer field is reported, including ones we do not use.
        assert set(result["developer_fields_found"]) == {
            "core_temperature", "skin_temperature", "Heat Strain Index", "core_data_quality",
        }
        # Native core_temperature wins over the developer field.
        assert result["core_temp"]["source"] == "native"
        assert result["core_temp"]["min"] == pytest.approx(37.0)
        assert result["core_temp"]["max"] == pytest.approx(37.9)
        assert result["core_temp"]["avg"] == pytest.approx(37.45, abs=0.01)
        assert result["core_temp"]["unit"] == "C"
        assert result["core_temp"]["samples"] == 10
        assert result["skin_temp"]["max"] == pytest.approx(33.9)
        assert result["hsi"]["max"] == pytest.approx(9.0)
        assert result["hsi"]["samples"] == 10
        assert result["duration_seconds"] == pytest.approx(9.0)
        assert result["discarded_samples"] == 0
        assert "note" not in result

    @pytest.mark.asyncio
    async def test_hsi_time_at_or_above_thresholds(self):
        # HSI climbs 0..9 at 1 s per sample, so each threshold t is met for
        # (10 - t) samples, the last of which also carries a 1 s span.
        records = [
            {"ts": _BASE_TS + i, "core": 37.0, "core_temperature": 37.0,
             "skin_temperature": 33.0, "Heat Strain Index": float(i), "core_data_quality": 3.0}
            for i in range(10)
        ]
        fit = build_fit(records, CORE_DEV_FIELDS)

        with patch("tp_mcp.tools.core_data.TPClient") as mock_client:
            mock_instance = _patched_client(
                _details_response([{"fileId": 55, "fileName": "ride.fit.gz"}]),
                _raw_gzip(fit),
            )
            mock_client.return_value.__aenter__.return_value = mock_instance
            result = await tp_get_core_data(workout_id="9001")

        time_at = result["hsi"]["time_at_or_above"]
        assert set(time_at) == {"2", "4", "6", "8"}
        assert time_at["2"] == pytest.approx(8.0)
        assert time_at["4"] == pytest.approx(6.0)
        assert time_at["6"] == pytest.approx(4.0)
        assert time_at["8"] == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_invalid_samples_discarded_but_counted(self):
        records = [
            # Core temp in Fahrenheit and an out-of-scale HSI: both implausible.
            {"ts": _BASE_TS, "core": 99.5, "core_temperature": 37.0,
             "skin_temperature": 33.0, "Heat Strain Index": 55.0, "core_data_quality": 3.0},
            {"ts": _BASE_TS + 1, "core": 37.2, "core_temperature": 37.0,
             "skin_temperature": 33.0, "Heat Strain Index": 4.0, "core_data_quality": 3.0},
        ]
        fit = build_fit(records, CORE_DEV_FIELDS)

        with patch("tp_mcp.tools.core_data.TPClient") as mock_client:
            mock_instance = _patched_client(
                _details_response([{"fileId": 55, "fileName": "ride.fit.gz"}]),
                _raw_gzip(fit),
            )
            mock_client.return_value.__aenter__.return_value = mock_instance
            result = await tp_get_core_data(workout_id="9001")

        assert result["core_temp"]["samples"] == 1
        assert result["core_temp"]["max"] == pytest.approx(37.2)
        assert result["hsi"]["samples"] == 1
        assert result["discarded_samples"] == 2

    @pytest.mark.asyncio
    async def test_falls_back_to_developer_core_temperature(self):
        """A device with no native field still yields core temp from the CIQ field."""
        records = [
            {"ts": _BASE_TS + i, "core": None, "core_temperature": 38.0 + i * 0.1,
             "skin_temperature": 33.0, "Heat Strain Index": 5.0, "core_data_quality": 3.0}
            for i in range(5)
        ]
        fit = build_fit(records, CORE_DEV_FIELDS)

        with patch("tp_mcp.tools.core_data.TPClient") as mock_client:
            mock_instance = _patched_client(
                _details_response([{"fileId": 55, "fileName": "ride.fit.gz"}]),
                _raw_gzip(fit),
            )
            mock_client.return_value.__aenter__.return_value = mock_instance
            result = await tp_get_core_data(workout_id="9001")

        assert result["core_temp"]["source"] == "developer"
        assert result["core_temp"]["min"] == pytest.approx(38.0)
        assert result["core_temp"]["samples"] == 5

    @pytest.mark.asyncio
    async def test_falls_back_when_native_samples_are_implausible(self):
        """A device logging Fahrenheit natively should not mask a good CIQ field."""
        records = [
            {"ts": _BASE_TS + i, "core": 99.5, "core_temperature": 38.2,
             "skin_temperature": 33.0, "Heat Strain Index": 5.0, "core_data_quality": 3.0}
            for i in range(4)
        ]
        fit = build_fit(records, CORE_DEV_FIELDS)

        with patch("tp_mcp.tools.core_data.TPClient") as mock_client:
            mock_instance = _patched_client(
                _details_response([{"fileId": 55, "fileName": "ride.fit.gz"}]),
                _raw_gzip(fit),
            )
            mock_client.return_value.__aenter__.return_value = mock_instance
            result = await tp_get_core_data(workout_id="9001")

        assert result["core_temp"]["source"] == "developer"
        assert result["core_temp"]["avg"] == pytest.approx(38.2, abs=0.01)
        assert result["core_temp"]["samples"] == 4

    @pytest.mark.asyncio
    async def test_no_core_data_returns_note_not_error(self):
        records = [{"ts": _BASE_TS + i, "core": None} for i in range(5)]
        fit = build_fit(records)

        with patch("tp_mcp.tools.core_data.TPClient") as mock_client:
            mock_instance = _patched_client(
                _details_response([{"fileId": 55, "fileName": "ride.fit.gz"}]),
                _raw_gzip(fit),
            )
            mock_client.return_value.__aenter__.return_value = mock_instance
            result = await tp_get_core_data(workout_id="9001")

        assert "isError" not in result
        assert result["core_temp"] is None
        assert result["skin_temp"] is None
        assert result["hsi"] is None
        assert result["developer_fields_found"] == []
        assert "Connect IQ" in result["note"]

    @pytest.mark.asyncio
    async def test_handles_uncompressed_fit_file(self):
        records = [
            {"ts": _BASE_TS + i, "core": 37.5, "core_temperature": 37.5,
             "skin_temperature": 33.0, "Heat Strain Index": 3.0, "core_data_quality": 3.0}
            for i in range(3)
        ]
        fit = build_fit(records, CORE_DEV_FIELDS)

        with patch("tp_mcp.tools.core_data.TPClient") as mock_client:
            mock_instance = _patched_client(
                _details_response([{"fileId": 55, "fileName": "ride.fit"}]),
                RawResponse(success=True, content=fit, content_type="application/octet-stream"),
            )
            mock_client.return_value.__aenter__.return_value = mock_instance
            result = await tp_get_core_data(workout_id="9001")

        assert result["core_temp"]["samples"] == 3

    @pytest.mark.asyncio
    async def test_downsamples_series_to_cap(self):
        # Six hours sampled every 10 s: more than 300 buckets at 60 s resolution,
        # so the step has to widen to stay under the cap.
        records = [
            {"ts": _BASE_TS + i * 10, "core": 37.0 + (i % 10) * 0.05, "core_temperature": 37.0,
             "skin_temperature": 33.0, "Heat Strain Index": 3.0, "core_data_quality": 3.0}
            for i in range(2160)
        ]
        fit = build_fit(records, CORE_DEV_FIELDS)

        with patch("tp_mcp.tools.core_data.TPClient") as mock_client:
            mock_instance = _patched_client(
                _details_response([{"fileId": 55, "fileName": "ride.fit.gz"}]),
                _raw_gzip(fit),
            )
            mock_client.return_value.__aenter__.return_value = mock_instance
            result = await tp_get_core_data(workout_id="9001")

        series = result["series"]
        assert 200 < len(series) <= 300
        assert series[0]["t_offset_seconds"] == 0
        assert set(series[0]) == {"t_offset_seconds", "core_temp", "skin_temp", "hsi"}
        # Offsets are a monotonic grid, widened past 60 s to honour the cap.
        offsets = [p["t_offset_seconds"] for p in series]
        assert offsets == sorted(offsets)
        assert offsets[1] - offsets[0] > 60
        # Bucket means stay inside the range of the underlying samples.
        assert all(37.0 <= p["core_temp"] <= 37.5 for p in series)

    @pytest.mark.asyncio
    async def test_no_device_file_returns_not_found(self):
        with patch("tp_mcp.tools.core_data.TPClient") as mock_client:
            mock_instance = _patched_client(_details_response([]), None)
            mock_client.return_value.__aenter__.return_value = mock_instance
            result = await tp_get_core_data(workout_id="9001")

        assert result["isError"] is True
        assert result["error_code"] == "NOT_FOUND"
        assert "no device file" in result["message"]

    @pytest.mark.asyncio
    async def test_missing_workout_returns_not_found(self):
        with patch("tp_mcp.tools.core_data.TPClient") as mock_client:
            mock_instance = _patched_client(
                APIResponse(success=False, error_code=ErrorCode.NOT_FOUND, message="nope"), None
            )
            mock_client.return_value.__aenter__.return_value = mock_instance
            result = await tp_get_core_data(workout_id="9001")

        assert result["isError"] is True
        assert result["error_code"] == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_invalid_workout_id_rejected(self):
        result = await tp_get_core_data(workout_id="not-a-number")
        assert result["isError"] is True
        assert result["error_code"] == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_non_fit_device_file_errors_clearly(self):
        with patch("tp_mcp.tools.core_data.TPClient") as mock_client:
            mock_instance = _patched_client(
                _details_response([{"fileId": 55, "fileName": "ride.tcx"}]),
                RawResponse(success=True, content=b"<TrainingCenterDatabase/>", content_type="application/xml"),
            )
            mock_client.return_value.__aenter__.return_value = mock_instance
            result = await tp_get_core_data(workout_id="9001")

        assert result["isError"] is True
        assert result["error_code"] == "API_ERROR"
        assert "FIT" in result["message"]


# ---------------------------------------------------------------------------
# tp_download_workout_file return_base64
# ---------------------------------------------------------------------------


class TestDownloadReturnBase64:
    """Inline base64 mode mirrors the file_data_base64 input of the upload tool."""

    @pytest.mark.asyncio
    async def test_returns_base64_without_writing_to_disk(self):
        payload = b"\x1f\x8bfake gzipped fit"

        with patch("tp_mcp.tools.workout_files.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get_raw = AsyncMock(
                return_value=RawResponse(
                    success=True,
                    content=payload,
                    content_type="application/gzip",
                    content_disposition='attachment; filename="ride.fit.gz"',
                )
            )
            mock_client.return_value.__aenter__.return_value = mock_instance

            with patch("tp_mcp.tools.workout_files._save_workout_file") as mock_save:
                result = await tp_download_workout_file(
                    workout_id="9001", file_id="55", return_base64=True
                )
                mock_save.assert_not_called()

        assert "saved_to" not in result
        assert base64.b64decode(result["file_data_base64"]) == payload
        assert result["file_name"] == "ride.fit.gz"
        assert result["content_type"] == "application/gzip"
        assert result["size_bytes"] == len(payload)

    @pytest.mark.asyncio
    async def test_refuses_oversized_file(self):
        oversized = b"x" * (MAX_INLINE_FILE_BYTES + 1)

        with patch("tp_mcp.tools.workout_files.TPClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.ensure_athlete_id = AsyncMock(return_value=123)
            mock_instance.get_raw = AsyncMock(
                return_value=RawResponse(success=True, content=oversized, content_type="application/gzip")
            )
            mock_client.return_value.__aenter__.return_value = mock_instance

            result = await tp_download_workout_file(workout_id="9001", file_id="55", return_base64=True)

        assert result["isError"] is True
        assert result["error_code"] == "VALIDATION_ERROR"
        assert str(MAX_INLINE_FILE_BYTES) in result["message"]

    @pytest.mark.asyncio
    async def test_rejects_output_path_with_return_base64(self):
        result = await tp_download_workout_file(
            workout_id="9001", file_id="55", output_path="/tmp/x.fit", return_base64=True
        )
        assert result["isError"] is True
        assert result["error_code"] == "VALIDATION_ERROR"
