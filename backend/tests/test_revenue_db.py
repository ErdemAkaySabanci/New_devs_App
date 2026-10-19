"""Integration tests against the seeded Postgres from docker-compose (skipped if the DB is unreachable)."""
import asyncio
from decimal import Decimal

import pytest

from app.core.database_pool import db_pool
from app.services.reservations import PropertyNotFound, calculate_revenue


def run(coro):
    async def wrapper():
        try:
            return await coro
        finally:
            await db_pool.close()  # engine is bound to this event loop
    return asyncio.run(wrapper())


def _db_available():
    async def ping():
        await db_pool.initialize()
        async with db_pool.get_session() as session:
            from sqlalchemy import text
            await session.execute(text("SELECT 1"))
    try:
        run(ping())
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="seeded database not reachable")


def test_total_revenue_uses_real_reservations():
    r = run(calculate_revenue("prop-001", "tenant-a"))
    assert Decimal(r["total"]) == Decimal("2250.000")
    assert r["count"] == 4


def test_shared_property_id_is_isolated_per_tenant():
    r = run(calculate_revenue("prop-001", "tenant-b"))  # Mountain Lodge Beta, no reservations
    assert Decimal(r["total"]) == 0 and r["count"] == 0


@pytest.mark.parametrize("property_id,tenant_id", [("prop-002", "tenant-b"), ("prop-004", "tenant-a")])
def test_other_tenants_property_is_not_found(property_id, tenant_id):
    with pytest.raises(PropertyNotFound):
        run(calculate_revenue(property_id, tenant_id))


def test_march_uses_paris_local_boundaries():
    march = run(calculate_revenue("prop-001", "tenant-a", month=3, year=2024))
    feb = run(calculate_revenue("prop-001", "tenant-a", month=2, year=2024))
    assert Decimal(march["total"]) == Decimal("2250.000") and march["count"] == 4
    assert Decimal(feb["total"]) == 0 and feb["count"] == 0


def test_march_new_york_property():
    r = run(calculate_revenue("prop-004", "tenant-b", month=3, year=2024))
    assert Decimal(r["total"]) == Decimal("1776.50") and r["count"] == 4
