"""Credential-safe connector failures and bounded retries for explicit reads."""
import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import random

import httpx


class ConnectorFailure(ValueError):
    def __init__(self, category, message, retry_after=None):
        super().__init__(message)
        self.category = category
        self.retry_after = retry_after


def response_failure(service, response):
    status = response.status_code
    if status == 401:
        return ConnectorFailure('authorization', f'{service} authorization expired or was revoked (401). Reconnect the account.')
    if status == 403:
        return ConnectorFailure('permission', f'{service} denied permission (403). Check scopes and resource access.')
    if status == 429:
        return ConnectorFailure('throttled', f'{service} rate limited the request (429). Try again later.', response.headers.get('Retry-After'))
    if 500 <= status <= 599:
        return ConnectorFailure('unavailable', f'{service} is temporarily unavailable ({status}). Try again later.', response.headers.get('Retry-After'))
    return ConnectorFailure('rejected', f'{service} rejected the request ({status}). Check the selected operation and resource.')


def transport_failure(service, error):
    if isinstance(error, BaseExceptionGroup):
        for nested in error.exceptions:
            failure = transport_failure(service, nested)
            if failure:
                return failure
    if isinstance(error, httpx.HTTPStatusError):
        return response_failure(service, error.response)
    if isinstance(error, (TimeoutError, httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return ConnectorFailure('network', f'{service} could not be reached. Check connectivity and try again.')
    if isinstance(error, httpx.DecodingError):
        return ConnectorFailure('malformed', f'{service} returned unreadable data.')
    return None


def _delay(value):
    if value:
        try:
            return max(0, int(value))
        except ValueError:
            try:
                return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    return .2


async def request_connector(service, operation, *, retry_read=False):
    async def attempt():
        try:
            return await operation()
        except Exception as error:
            failure = transport_failure(service, error)
            if not failure:
                raise
            raise failure from None

    if not retry_read:
        return await attempt()
    try:
        async with asyncio.timeout(25):
            try:
                return await attempt()
            except ConnectorFailure as error:
                if error.category not in ('network', 'throttled', 'unavailable'):
                    raise
                delay = _delay(error.retry_after)
                if delay > 2:
                    raise
                await asyncio.sleep(delay + random.uniform(0, .1))
                return await attempt()
    except TimeoutError:
        raise ConnectorFailure('network', f'{service} read timed out. Check connectivity and try again.') from None
