"""
Rate limiter instance — shared across all route modules.

Import `limiter` here and apply via @limiter.limit("N/period") decorator.
The limiter is keyed on the client's remote IP address.

Usage in a route:
    from app.limiter import limiter
    from fastapi import Request

    @router.post("/auth/login")
    @limiter.limit("10/minute")
    def login(request: Request, body: LoginRequest, ...):
        ...

Wire-up in main.py:
    from app.limiter import limiter
    from slowapi.errors import RateLimitExceeded
    from slowapi import _rate_limit_exceeded_handler

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
