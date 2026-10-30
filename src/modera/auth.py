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
    valid = False
    if len(request.headers.getlist("authorization")) == 1 and credentials is not None:
        submitted = credentials.credentials.encode("utf-8")
        # Compare every configured key: do not short-circuit on the first match.
        for key in request.app.state.api_keys:
            valid |= compare_digest(submitted, key.get_secret_value().encode("utf-8"))
    if not valid:
        raise APIError(
            401,
            "authentication_required",
            "A valid bearer API key is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
