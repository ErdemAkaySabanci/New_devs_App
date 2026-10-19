import json
import redis.asyncio as redis
from typing import Dict, Any, Optional
import os

# Initialize Redis client (typically configured centrally).
redis_client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))


def revenue_cache_key(property_id: str, tenant_id: str, month: Optional[int] = None, year: Optional[int] = None) -> str:
    # property ids are only unique per tenant (e.g. prop-001 exists for two tenants),
    # so the tenant must be part of the key or tenants read each other's cached revenue.
    period = f"{year:04d}-{month:02d}" if month and year else "all"
    return f"revenue:{tenant_id}:{property_id}:{period}"


async def get_revenue_summary(
    property_id: str, tenant_id: str, month: Optional[int] = None, year: Optional[int] = None
) -> Dict[str, Any]:
    """
    Fetches revenue summary, utilizing caching to improve performance.
    """
    cache_key = revenue_cache_key(property_id, tenant_id, month, year)

    # Try to get from cache
    cached = await redis_client.get(cache_key)
    if cached:
        return json.loads(cached)

    # Revenue calculation is delegated to the reservation service.
    from app.services import reservations

    # Calculate revenue (errors propagate, so failures are never cached)
    result = await reservations.calculate_revenue(property_id, tenant_id, month=month, year=year)

    # Cache the result for 5 minutes
    await redis_client.setex(cache_key, 300, json.dumps(result))

    return result
