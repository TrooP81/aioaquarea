import functools
import logging
from typing import TYPE_CHECKING  # Re-add TYPE_CHECKING

from .errors import ApiError, AuthenticationError

if TYPE_CHECKING:  # Re-add TYPE_CHECKING block
    from .core import AquareaClient

_LOGGER = logging.getLogger(__name__)


def auth_required(fn):
    """Require an authenticated client before entering an API operation."""

    @functools.wraps(fn)
    async def _wrap(
        client: "AquareaClient", *args, **kwargs
    ):  # Use string literal for type hint
        if client.is_logged is False:
            client.logger.warning(f"{client}: User is not logged or session is too old")
            await client.login()

        try:
            response = await fn(client, *args, **kwargs)
        except (
            AuthenticationError,
            ApiError,
        ) as exception:  # Catch both AuthenticationError and ApiError
            client.logger.warning(
                f"{client}: API Error: {getattr(exception, 'error_code', 'N/A')} - {getattr(exception, 'error_message', str(exception))}."
            )

            # API response authentication failures are recovered centrally by
            # AquareaAPIClient. Retrying here would multiply full login attempts
            # and resend the same logical Panasonic operation more than once.
            raise

        return response

    return _wrap
