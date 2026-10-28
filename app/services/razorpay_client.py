"""Minimal Razorpay REST client.

The official SDK is unnecessary here: two endpoints are used (create an order,
refund a payment), both plain JSON over HTTPS with HTTP Basic auth. Keeping the
surface this small means the payment service can depend on a two-method
protocol, which tests replace with a fake instead of mocking HTTP internals.

Provider failures are deliberately opaque to the client. A 401 from Razorpay
means our keys are wrong, not that the shopper did anything; leaking the
provider's response would expose account details with no benefit.
"""
from __future__ import annotations

from typing import Protocol

import httpx

from ..core.config import Settings
from ..core.exceptions import ServiceUnavailableError
from ..core.logging import get_logger

logger = get_logger(__name__)

RAZORPAY_API_BASE = "https://api.razorpay.com/v1"
REQUEST_TIMEOUT_SECONDS = 15.0


class PaymentGateway(Protocol):
    """The provider operations the payment service is allowed to perform."""

    async def create_order(
        self,
        *,
        amount_paise: int,
        receipt: str,
        notes: dict[str, str],
    ) -> dict: ...

    async def refund(
        self,
        *,
        provider_payment_id: str,
        amount_paise: int,
        notes: dict[str, str],
    ) -> dict: ...


class RazorpayClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def is_configured(self) -> bool:
        return bool(
            self.settings.RAZORPAY_KEY_ID
            and self.settings.RAZORPAY_KEY_SECRET
        )

    def _credentials(self) -> tuple[str, str]:
        if not self.is_configured:
            raise ServiceUnavailableError(
                "Online payments are not configured",
                error_code="payments_not_configured",
            )
        return (
            self.settings.RAZORPAY_KEY_ID.get_secret_value(),  # type: ignore[union-attr]
            self.settings.RAZORPAY_KEY_SECRET.get_secret_value(),  # type: ignore[union-attr]
        )

    async def create_order(
        self,
        *,
        amount_paise: int,
        receipt: str,
        notes: dict[str, str],
    ) -> dict:
        payload = {
            "amount": amount_paise,
            "currency": "INR",
            "receipt": receipt[:40],
            "payment_capture": 1,
            "notes": notes,
        }
        return await self._post("/orders", payload)

    async def refund(
        self,
        *,
        provider_payment_id: str,
        amount_paise: int,
        notes: dict[str, str],
    ) -> dict:
        return await self._post(
            f"/payments/{provider_payment_id}/refund",
            {"amount": amount_paise, "notes": notes},
        )

    async def _post(self, path: str, payload: dict) -> dict:
        key_id, key_secret = self._credentials()
        try:
            async with httpx.AsyncClient(
                base_url=RAZORPAY_API_BASE,
                auth=(key_id, key_secret),
                timeout=REQUEST_TIMEOUT_SECONDS,
            ) as client:
                response = await client.post(path, json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "razorpay.request_rejected",
                path=path,
                status_code=exc.response.status_code,
                provider_error=_safe_provider_error(exc.response),
            )
            raise ServiceUnavailableError(
                "The payment provider rejected the request",
                error_code="payment_provider_error",
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            logger.error("razorpay.request_failed", path=path, exc_info=exc)
            raise ServiceUnavailableError(
                "The payment provider is temporarily unavailable",
                error_code="payment_provider_unavailable",
            ) from exc

        if not isinstance(data, dict) or not data.get("id"):
            logger.error("razorpay.unexpected_response", path=path)
            raise ServiceUnavailableError(
                "The payment provider returned an unexpected response",
                error_code="payment_provider_error",
            )
        return data


def _safe_provider_error(response: httpx.Response) -> str | None:
    """Extract only the provider's machine error code, never the whole body."""
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        code = error.get("code")
        return str(code)[:100] if code else None
    return None


__all__ = ["PaymentGateway", "RazorpayClient", "RAZORPAY_API_BASE"]
