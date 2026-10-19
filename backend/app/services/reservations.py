from datetime import datetime
import logging
import pytz
from decimal import Decimal
from typing import Dict, Any, Optional, Tuple

from sqlalchemy import text

from app.core.database_pool import db_pool

logger = logging.getLogger(__name__)


class PropertyNotFound(Exception):
    """The property does not exist for this tenant (or belongs to another tenant)."""


def month_bounds(year: int, month: int, tz_name: str) -> Tuple[datetime, datetime]:
    """
    [start, end) of a calendar month in the property's local timezone.
    A check-in at 2024-02-29 23:30 UTC is 2024-03-01 00:30 in Paris, so it belongs to March there.
    """
    tz = pytz.timezone(tz_name)
    start = tz.localize(datetime(year, month, 1))
    end = tz.localize(datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1))
    return start, end


async def _get_session():
    if not db_pool.session_factory:
        await db_pool.initialize()
    if not db_pool.session_factory:
        raise Exception("Database pool not available")
    return db_pool.get_session()


async def _property_timezone(session, property_id: str, tenant_id: str) -> str:
    row = (await session.execute(
        text("SELECT timezone FROM properties WHERE id = :property_id AND tenant_id = :tenant_id"),
        {"property_id": property_id, "tenant_id": tenant_id},
    )).fetchone()
    if not row:
        raise PropertyNotFound(property_id)
    return row.timezone


async def calculate_revenue(
    property_id: str, tenant_id: str, month: Optional[int] = None, year: Optional[int] = None
) -> Dict[str, Any]:
    """
    Revenue for one property of one tenant: all-time, or for a calendar month in the property's timezone.
    Database errors propagate; we never answer with made-up financial figures.
    """
    async with await _get_session() as session:
        tz_name = await _property_timezone(session, property_id, tenant_id)

        params = {"property_id": property_id, "tenant_id": tenant_id}
        period_filter = ""
        if month is not None and year is not None:
            params["start_date"], params["end_date"] = month_bounds(year, month, tz_name)
            period_filter = "AND check_in_date >= :start_date AND check_in_date < :end_date"

        row = (await session.execute(
            text(f"""
                SELECT COALESCE(SUM(total_amount), 0) AS total_revenue, COUNT(*) AS reservation_count
                FROM reservations
                WHERE property_id = :property_id AND tenant_id = :tenant_id
                {period_filter}
            """),
            params,
        )).fetchone()

    return {
        "property_id": property_id,
        "tenant_id": tenant_id,
        "total": str(Decimal(str(row.total_revenue))),
        "currency": "USD",
        "count": row.reservation_count,
    }


async def calculate_monthly_revenue(property_id: str, tenant_id: str, month: int, year: int) -> Decimal:
    """Calculates revenue for a specific month in the property's local timezone."""
    result = await calculate_revenue(property_id, tenant_id, month=month, year=year)
    return Decimal(result["total"])


async def calculate_total_revenue(property_id: str, tenant_id: str) -> Dict[str, Any]:
    """Aggregates all-time revenue from database."""
    return await calculate_revenue(property_id, tenant_id)
