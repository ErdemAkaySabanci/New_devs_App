import logging
from decimal import Decimal, ROUND_HALF_UP
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Dict, Any, Optional
from app.services.cache import get_revenue_summary
from app.services.reservations import PropertyNotFound
from app.core.auth import authenticate_request as get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()


def to_money(amount: Any) -> Decimal:
    # Round once, half-up, on the Decimal total (amounts are stored with 3 decimals).
    return Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@router.get("/dashboard/summary")
async def get_dashboard_summary(
    property_id: str,
    month: Optional[int] = Query(None, ge=1, le=12),
    year: Optional[int] = Query(None, ge=2000, le=2100),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:

    # Never fall back to a shared tenant: that would mix data across clients.
    tenant_id = getattr(current_user, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=403, detail="No tenant associated with this user")
    if (month is None) != (year is None):
        raise HTTPException(status_code=422, detail="month and year must be provided together")

    try:
        revenue_data = await get_revenue_summary(property_id, tenant_id, month, year)
    except PropertyNotFound:
        # Same answer whether the property doesn't exist or belongs to another tenant.
        raise HTTPException(status_code=404, detail="Property not found")
    except Exception as e:
        logger.error(f"Revenue lookup failed for {property_id} (tenant: {tenant_id}): {e}")
        raise HTTPException(status_code=503, detail="Revenue data temporarily unavailable")

    return {
        "property_id": revenue_data['property_id'],
        "total_revenue": float(to_money(revenue_data['total'])),
        "currency": revenue_data['currency'],
        "reservations_count": revenue_data['count']
    }
