"""Unit tests for tenant isolation, timezone boundaries and money rounding (no DB/Redis needed)."""
import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import dashboard
from app.core.tenant_resolver import TenantResolver
from app.services import cache, reservations
from app.services.reservations import PropertyNotFound, month_bounds


class FakeRedis:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.store[key] = value


# ---------- tenant isolation: cache ----------

def test_cache_key_contains_tenant_and_period():
    assert cache.revenue_cache_key("prop-001", "tenant-a") == "revenue:tenant-a:prop-001:all"
    assert cache.revenue_cache_key("prop-001", "tenant-b") == "revenue:tenant-b:prop-001:all"
    assert cache.revenue_cache_key("prop-001", "tenant-a", 3, 2024) == "revenue:tenant-a:prop-001:2024-03"


def test_same_property_id_never_shares_cache_between_tenants(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(cache, "redis_client", fake)

    async def fake_calc(property_id, tenant_id, month=None, year=None):
        total = {"tenant-a": "2250.000", "tenant-b": "0"}[tenant_id]
        return {"property_id": property_id, "tenant_id": tenant_id, "total": total, "currency": "USD", "count": 0}

    monkeypatch.setattr(reservations, "calculate_revenue", fake_calc)

    async def scenario():
        results = []
        for _ in range(3):  # repeated refreshes, interleaved tenants
            results.append(await cache.get_revenue_summary("prop-001", "tenant-a"))
            results.append(await cache.get_revenue_summary("prop-001", "tenant-b"))
        return results

    results = asyncio.run(scenario())
    assert all(r["total"] == "2250.000" for r in results[0::2])
    assert all(r["total"] == "0" for r in results[1::2])
    assert {json.loads(v)["tenant_id"] for v in fake.store.values()} == {"tenant-a", "tenant-b"}


def test_failed_calculation_is_not_cached(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(cache, "redis_client", fake)

    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(reservations, "calculate_revenue", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(cache.get_revenue_summary("prop-001", "tenant-a"))
    assert fake.store == {}


# ---------- tenant isolation: auth / API ----------

def test_unknown_user_gets_no_default_tenant():
    assert asyncio.run(TenantResolver.resolve_tenant_id("u1", "stranger@example.com")) is None
    assert asyncio.run(TenantResolver.resolve_tenant_id("u2", "ocean@propertyflow.com")) == "tenant-b"


def _call_summary(user, property_id="prop-001", month=None, year=None):
    return asyncio.run(dashboard.get_dashboard_summary(property_id, month, year, current_user=user))


def test_summary_rejects_user_without_tenant():
    with pytest.raises(HTTPException) as exc:
        _call_summary(SimpleNamespace(tenant_id=None))
    assert exc.value.status_code == 403


def test_summary_hides_other_tenants_property(monkeypatch):
    async def not_found(*args, **kwargs):
        raise PropertyNotFound("prop-002")

    monkeypatch.setattr(dashboard, "get_revenue_summary", not_found)
    with pytest.raises(HTTPException) as exc:
        _call_summary(SimpleNamespace(tenant_id="tenant-b"), "prop-002")
    assert exc.value.status_code == 404


def test_summary_never_returns_mock_data_on_db_error(monkeypatch):
    async def db_down(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(dashboard, "get_revenue_summary", db_down)
    with pytest.raises(HTTPException) as exc:
        _call_summary(SimpleNamespace(tenant_id="tenant-a"))
    assert exc.value.status_code == 503


# ---------- timezone boundaries ----------

def test_march_bounds_paris_and_new_york():
    start, end = month_bounds(2024, 3, "Europe/Paris")
    assert start.astimezone(timezone.utc) == datetime(2024, 2, 29, 23, 0, tzinfo=timezone.utc)
    assert end.astimezone(timezone.utc) == datetime(2024, 3, 31, 22, 0, tzinfo=timezone.utc)  # DST starts Mar 31

    start, end = month_bounds(2024, 3, "America/New_York")
    assert start.astimezone(timezone.utc) == datetime(2024, 3, 1, 5, 0, tzinfo=timezone.utc)
    assert end.astimezone(timezone.utc) == datetime(2024, 4, 1, 4, 0, tzinfo=timezone.utc)  # DST since Mar 10


def test_late_utc_checkin_belongs_to_local_month():
    res_tz_1 = datetime(2024, 2, 29, 23, 30, tzinfo=timezone.utc)  # seed reservation, prop-001 (Paris)
    paris_start, paris_end = month_bounds(2024, 3, "Europe/Paris")
    utc_start, _ = month_bounds(2024, 3, "UTC")
    assert paris_start <= res_tz_1 < paris_end
    assert res_tz_1 < utc_start


def test_december_rolls_over_and_end_is_exclusive():
    start, end = month_bounds(2024, 12, "UTC")
    assert (start.year, start.month) == (2024, 12)
    assert (end.year, end.month, end.day) == (2025, 1, 1)
    jan_first = end  # a check-in exactly at the end bound belongs to the next month
    assert not (start <= jan_first < end)


# ---------- financial precision ----------

def test_three_decimal_amounts_sum_exactly():
    total = Decimal("333.333") + Decimal("333.333") + Decimal("333.334")
    assert dashboard.to_money(total) == Decimal("1000.00")


def test_half_up_rounding_where_float_rounding_fails():
    assert round(2.675, 2) == 2.67  # float representation error
    assert dashboard.to_money("2.675") == Decimal("2.68")
    assert dashboard.to_money("1199.255") == Decimal("1199.26")


def test_summary_returns_cent_rounded_total(monkeypatch):
    async def summary(*args, **kwargs):
        return {"property_id": "prop-001", "tenant_id": "tenant-a", "total": "1000.005", "currency": "USD", "count": 3}

    monkeypatch.setattr(dashboard, "get_revenue_summary", summary)
    body = _call_summary(SimpleNamespace(tenant_id="tenant-a"))
    assert body["total_revenue"] == 1000.01
    assert body["reservations_count"] == 3
