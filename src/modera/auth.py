from secrets import compare_digest
from typing import Annotated

from fastapi import Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from modera.errors import APIError

bearer = HTTPBearer(
    auto_error=False, scheme_name="ApiKey", description="Shared service API key"
)


async def authenticate(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> None:
    if (
        len(request.headers.getlist("authorization")) != 1
        or credentials is None
        or not compare_digest(
            credentials.credentials.encode("utf-8"),
            request.app.state.api_key.get_secret_value().encode("utf-8"),
        )
    ):
        raise APIError(
            401,
            "authentication_required",
            "A valid bearer API key is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
