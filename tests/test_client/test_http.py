"""Tests for HTTP client, including throttling and athlete ID caching."""

import time
from unittest.mock import AsyncMock

import httpx
import pytest

from tp_mcp.client.http import MIN_REQUEST_INTERVAL, APIResponse, TPClient


class TestThrottling:
    """Tests for request throttling."""

    @pytest.mark.asyncio
    async def test_throttle_enforces_minimum_interval(self):
        """Throttle should enforce minimum interval between requests."""
        client = TPClient()

        # First call should not block
        start = time.monotonic()
        await client._throttle()
        first_duration = time.monotonic() - start
        assert first_duration < 0.05  # Should be nearly instant

        # Immediate second call should be delayed
        start = time.monotonic()
        await client._throttle()
        second_duration = time.monotonic() - start
        assert second_duration >= MIN_REQUEST_INTERVAL * 0.9  # Allow 10% tolerance

    @pytest.mark.asyncio
    async def test_throttle_no_delay_when_spaced(self):
        """Throttle should not delay when requests are naturally spaced."""
        client = TPClient()

        await client._throttle()

        # Wait longer than the interval
        import asyncio

        await asyncio.sleep(MIN_REQUEST_INTERVAL + 0.05)

        # Next call should not block
        start = time.monotonic()
        await client._throttle()
        duration = time.monotonic() - start
        assert duration < 0.05  # Should be nearly instant

    @pytest.mark.asyncio
    async def test_throttle_multiple_rapid_calls(self):
        """Multiple rapid calls should each be throttled."""
        client = TPClient()

        start = time.monotonic()

        # Make 4 rapid throttle calls
        for _ in range(4):
            await client._throttle()

        total_duration = time.monotonic() - start

        # Should take at least 3 * MIN_REQUEST_INTERVAL (first is instant, next 3 are throttled)
        expected_min = MIN_REQUEST_INTERVAL * 3 * 0.9  # 10% tolerance
        assert total_duration >= expected_min

    @pytest.mark.asyncio
    async def test_client_init_sets_last_request_time(self):
        """Client should initialize last request time to 0."""
        client = TPClient()
        assert client._last_request_time == 0.0


async def _default_athlete_id() -> int | None:
    """Read the athlete id cached for the default (single-user) subject."""
    from tp_mcp.client.http import _DEFAULT_SUBJECT

    cache = TPClient._identity_caches.get(_DEFAULT_SUBJECT)
    return cache.athlete_id if cache else None


class TestEnsureAthleteId:
    """Tests for athlete ID caching via ensure_athlete_id."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        """Reset per-subject caches between tests."""
        TPClient._identity_caches.clear()
        yield
        TPClient._identity_caches.clear()

    @pytest.mark.asyncio
    async def test_returns_cached_value(self):
        """Should return the per-subject cached athlete ID without an API call."""
        from tp_mcp.client.http import _DEFAULT_SUBJECT, _IdentityCache

        cache = _IdentityCache()
        cache.athlete_id = 999
        TPClient._identity_caches[_DEFAULT_SUBJECT] = cache

        client = TPClient()
        client.get = AsyncMock()  # should not be called

        result = await client.ensure_athlete_id()

        assert result == 999
        client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetches_from_api_and_caches(self):
        """Should fetch athlete ID from API and cache it per subject."""
        client = TPClient()
        client.get = AsyncMock(return_value=APIResponse(success=True, data={"user": {"personId": 42}}))

        result = await client.ensure_athlete_id()

        assert result == 42
        assert await _default_athlete_id() == 42
        assert client.athlete_id == 42

    @pytest.mark.asyncio
    async def test_falls_back_to_athletes_array(self):
        """Should use athletes[0].athleteId when personId is missing."""
        client = TPClient()
        client.get = AsyncMock(
            return_value=APIResponse(
                success=True,
                data={"user": {"athletes": [{"athleteId": 77}]}},
            )
        )

        result = await client.ensure_athlete_id()

        assert result == 77
        assert await _default_athlete_id() == 77

    @pytest.mark.asyncio
    async def test_returns_none_on_api_failure(self):
        """Should return None when API call fails (no caching)."""
        client = TPClient()
        client.get = AsyncMock(return_value=APIResponse(success=False, message="Auth failed"))

        result = await client.ensure_athlete_id()

        assert result is None
        assert await _default_athlete_id() is None

    @pytest.mark.asyncio
    async def test_cache_persists_across_instances(self):
        """Per-subject cache should persist across TPClient instances."""
        client1 = TPClient()
        client1.get = AsyncMock(return_value=APIResponse(success=True, data={"user": {"personId": 123}}))
        await client1.ensure_athlete_id()

        # Second instance should use the cached value without an API call
        client2 = TPClient()
        client2.get = AsyncMock()

        result = await client2.ensure_athlete_id()

        assert result == 123
        client2.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_instance_athlete_id_if_set(self):
        """Should return instance-level athlete_id if already set."""
        client = TPClient()
        client.athlete_id = 555
        client.get = AsyncMock()

        result = await client.ensure_athlete_id()

        assert result == 555
        client.get.assert_not_called()


class TestPerSubjectCache:
    """Tests for per-subject cache isolation (multi-user safety)."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        TPClient._identity_caches.clear()
        yield
        TPClient._identity_caches.clear()

    @pytest.mark.asyncio
    async def test_same_subject_shares_cache(self):
        """Two instances resolve the same identity cache for the same subject."""
        c1 = await TPClient._get_identity_cache()
        c2 = await TPClient._get_identity_cache()
        assert c1 is c2

    @pytest.mark.asyncio
    async def test_different_subjects_isolated(self):
        """Different subjects must get distinct token/athlete caches (no leakage)."""
        from tp_mcp.client.context import current_subject

        tok_a = current_subject.set("subject-A")
        cache_a = await TPClient._get_identity_cache()
        cache_a.athlete_id = 111
        cache_a.token_cache.access_token = "token-A"
        current_subject.reset(tok_a)

        tok_b = current_subject.set("subject-B")
        cache_b = await TPClient._get_identity_cache()
        current_subject.reset(tok_b)

        assert cache_a is not cache_b
        assert cache_b.athlete_id is None
        assert cache_b.token_cache.access_token is None

    @pytest.mark.asyncio
    async def test_invalidate_subject(self):
        """invalidate_subject drops that subject's cache."""
        from tp_mcp.client.context import current_subject

        tok = current_subject.set("subject-C")
        cache = await TPClient._get_identity_cache()
        cache.athlete_id = 7
        TPClient.invalidate_subject("subject-C")
        fresh = await TPClient._get_identity_cache()
        current_subject.reset(tok)
        assert fresh.athlete_id is None


class TestHandleResponse:
    """Tests for HTTP response handling."""

    def test_204_is_success(self):
        """204 No Content responses should be treated as successful writes."""
        client = TPClient()

        response = httpx.Response(status_code=204)

        result = client._handle_response(response)

        assert result.success is True
        assert result.data is None
