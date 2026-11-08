"""
uk/identity.py — who is making this request.

The API layer resolves identity BEFORE the graph runs and puts only the
verified NHS number in state (requester_id). The model never sees a token
and can never set or change identity.

    NHS_LOGIN_MODE=demo   X-Demo-User: <nhs number> (or Bearer demo:<nhs>).
                          For local demos only; refuses to start when
                          CAREROUTE_REQUIRE_SHARED_STATE=1 unless explicitly
                          allowed (see config.validate).
    NHS_LOGIN_MODE=oidc   Authorization: Bearer <NHS login ID token>.
                          Verified against NHS login's JWKS: signature (RS512
                          or RS256), issuer, audience (your client id), expiry,
                          and identity proofing level P9 — the level NHS login
                          requires for access to health records and booking.

The browser completes the OIDC authorisation-code flow with NHS login
(private_key_jwt client authentication) and sends the ID token here.
"""

from __future__ import annotations

import time

import httpx

from careroute.uk.nhs_number import is_valid, normalise
from careroute.uk.records import NhsRecords


class IdentityError(Exception):
    pass


class DemoIdentity:
    mode = "demo"

    def __init__(self, records: NhsRecords):
        self.records = records

    async def resolve(self, headers) -> str | None:
        raw = headers.get("x-demo-user") or ""
        auth = headers.get("authorization") or ""
        if not raw and auth.lower().startswith("bearer demo:"):
            raw = auth.split(":", 1)[1]
        if not raw:
            return None
        n = normalise(raw)
        if not is_valid(n) or self.records.get(n) is None:
            raise IdentityError("Unknown demo user.")
        return n


class NhsLoginIdentity:
    """Verifies NHS login ID tokens. Keys are fetched once and cached; an
    unknown key id triggers one refresh (key rotation)."""
    mode = "oidc"
    ALGORITHMS = ["RS512", "RS256"]

    def __init__(self, issuer: str, client_id: str, *, client: httpx.AsyncClient | None = None,
                 jwks_url: str | None = None, required_proofing: str = "P9",
                 jwks_ttl_s: int = 3600):
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.jwks_url = jwks_url or f"{self.issuer}/.well-known/jwks.json"
        self.required_proofing = required_proofing
        self._http = client or httpx.AsyncClient()
        self._keys: dict[str, dict] = {}
        self._fetched = 0.0
        self.jwks_ttl_s = jwks_ttl_s

    async def _jwks(self, force: bool = False) -> dict[str, dict]:
        if force or not self._keys or time.time() - self._fetched > self.jwks_ttl_s:
            r = await self._http.get(self.jwks_url, timeout=5)
            r.raise_for_status()
            self._keys = {k["kid"]: k for k in r.json().get("keys", []) if "kid" in k}
            self._fetched = time.time()
        return self._keys

    async def resolve(self, headers) -> str | None:
        import jwt                                   # PyJWT[crypto]

        auth = headers.get("authorization") or ""
        if not auth.lower().startswith("bearer "):
            return None
        token = auth.split(" ", 1)[1].strip()
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            keys = await self._jwks()
            if kid not in keys:
                keys = await self._jwks(force=True)
            if kid not in keys:
                raise IdentityError("Token signed with an unknown key.")
            key = jwt.PyJWK(keys[kid]).key
            claims = jwt.decode(token, key, algorithms=self.ALGORITHMS,
                                audience=self.client_id, issuer=self.issuer,
                                options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        except jwt.PyJWTError as e:
            raise IdentityError(f"Invalid NHS login token: {e}") from e
        if claims.get("identity_proofing_level") != self.required_proofing:
            raise IdentityError("NHS login identity must be verified to P9 to use this service.")
        n = normalise(claims.get("nhs_number"))
        if not is_valid(n):
            raise IdentityError("NHS login token has no valid NHS number.")
        return n
