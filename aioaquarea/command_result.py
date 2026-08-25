from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiohttp


@dataclass(frozen=True, slots=True)
class PanasonicCommandResult:
    """Non-sensitive acknowledgement metadata from a Panasonic write."""

    http_status: int | None
    response_code: str | int | None = None
    request_id: str | None = None

    @classmethod
    async def from_response(
        cls, response: aiohttp.ClientResponse
    ) -> PanasonicCommandResult:
        payload: object = None
        if getattr(response, "content_type", None) == "application/json":
            payload = await response.json()
        data = payload if isinstance(payload, dict) else {}
        response_code = next(
            (data[key] for key in ("code", "resultCode") if isinstance(data.get(key), (str, int))),
            None,
        )
        request_id = next(
            (
                str(data[key])
                for key in ("requestId", "requestID", "request_id")
                if isinstance(data.get(key), (str, int))
            ),
            None,
        )
        status = getattr(response, "status", None)
        return cls(
            http_status=status if isinstance(status, int) and not isinstance(status, bool) else None,
            response_code=response_code,
            request_id=request_id,
        )

    def audit_fields(self) -> dict[str, str | int]:
        """Return only metadata that is safe to persist in action history."""
        return {
            key: value
            for key, value in {
                "panasonic_http_status": self.http_status,
                "panasonic_response_code": self.response_code,
                "panasonic_request_id": self.request_id,
            }.items()
            if value is not None
        }
