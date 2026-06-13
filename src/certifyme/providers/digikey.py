"""DigiKey Product Information API (v4) datasheet provider.

Auth uses the OAuth2 *client credentials* flow, which only needs the app's
Client ID and Client Secret (no user login). We then call the keyword-search
endpoint and pull ``DatasheetUrl`` off the best matching product.

Only the standard library is used so the tool installs with zero dependencies.

Credentials are read from the environment:

    DIGIKEY_CLIENT_ID       (required)
    DIGIKEY_CLIENT_SECRET   (required)
    DIGIKEY_SANDBOX=1       (optional; use the sandbox host)
    DIGIKEY_LOCALE_SITE     (optional; default "US")
    DIGIKEY_LOCALE_LANGUAGE (optional; default "en")
    DIGIKEY_LOCALE_CURRENCY (optional; default "USD")
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from .base import CachingProvider

PROD_HOST = "https://api.digikey.com"
SANDBOX_HOST = "https://sandbox-api.digikey.com"


class DigiKeyError(RuntimeError):
    pass


def _build_ssl_context() -> ssl.SSLContext:
    """Build an SSL context with a CA bundle that actually exists.

    KiCad bundles a Python whose default cert path points at a non-existent
    python.org location, so ``urlopen`` fails every HTTPS call with a misleading
    "self signed certificate in certificate chain" error. We pick the first CA
    bundle we can find so verification works inside KiCad's interpreter too.

    Order: explicit env override -> certifi -> the system bundle -> Python's
    own defaults. Set ``DIGIKEY_INSECURE=1`` to skip verification entirely
    (last resort; not recommended).
    """
    if os.environ.get("DIGIKEY_INSECURE", "").lower() in ("1", "true", "yes"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    for var in ("DIGIKEY_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        path = os.environ.get(var)
        if path and os.path.exists(path):
            return ssl.create_default_context(cafile=path)

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass

    for path in ("/etc/ssl/cert.pem", "/opt/homebrew/etc/ca-certificates/cert.pem"):
        if os.path.exists(path):
            return ssl.create_default_context(cafile=path)

    return ssl.create_default_context()


class DigiKeyProvider(CachingProvider):
    name = "digikey"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        sandbox: bool = False,
        locale_site: str = "US",
        locale_language: str = "en",
        locale_currency: str = "USD",
        timeout: float = 20.0,
    ):
        super().__init__()
        if not client_id or not client_secret:
            raise DigiKeyError(
                "DigiKey credentials missing. Set DIGIKEY_CLIENT_ID and "
                "DIGIKEY_CLIENT_SECRET (see .env.example)."
            )
        self.client_id = client_id
        self.client_secret = client_secret
        self.host = SANDBOX_HOST if sandbox else PROD_HOST
        self.locale_site = locale_site
        self.locale_language = locale_language
        self.locale_currency = locale_currency
        self.timeout = timeout
        self._ssl = _build_ssl_context()
        self._token: str | None = None
        self._token_expiry: float = 0.0

    @classmethod
    def from_env(cls, **overrides) -> "DigiKeyProvider":
        env = os.environ
        sandbox = env.get("DIGIKEY_SANDBOX", "").lower() in ("1", "true", "yes")
        kwargs = dict(
            client_id=env.get("DIGIKEY_CLIENT_ID", ""),
            client_secret=env.get("DIGIKEY_CLIENT_SECRET", ""),
            sandbox=sandbox,
            locale_site=env.get("DIGIKEY_LOCALE_SITE", "US"),
            locale_language=env.get("DIGIKEY_LOCALE_LANGUAGE", "en"),
            locale_currency=env.get("DIGIKEY_LOCALE_CURRENCY", "USD"),
        )
        kwargs.update(overrides)
        return cls(**kwargs)

    # -- auth ---------------------------------------------------------------

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 30:
            return self._token
        data = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        ).encode()
        req = urllib.request.Request(
            f"{self.host}/v1/oauth2/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        payload = self._send(req)
        token = payload.get("access_token")
        if not token:
            raise DigiKeyError(f"no access_token in token response: {payload}")
        self._token = token
        self._token_expiry = time.time() + float(payload.get("expires_in", 600))
        return token

    # -- search -------------------------------------------------------------

    def _lookup(self, query: str) -> str | None:
        token = self._access_token()
        body = json.dumps({"Keywords": query, "Limit": 5, "Offset": 0}).encode()
        req = urllib.request.Request(
            f"{self.host}/products/v4/search/keyword",
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "X-DIGIKEY-Client-Id": self.client_id,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-DIGIKEY-Locale-Site": self.locale_site,
                "X-DIGIKEY-Locale-Language": self.locale_language,
                "X-DIGIKEY-Locale-Currency": self.locale_currency,
            },
            method="POST",
        )
        try:
            payload = self._send(req)
        except DigiKeyError:
            raise
        return _extract_datasheet(payload)

    # -- transport ----------------------------------------------------------

    def _send(self, req: urllib.request.Request, _retries: int = 2) -> dict:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            if exc.code == 429 and _retries > 0:  # rate limited; back off
                time.sleep(2.0)
                return self._send(req, _retries - 1)
            raise DigiKeyError(f"HTTP {exc.code} from DigiKey: {detail}") from exc
        except urllib.error.URLError as exc:
            if _retries > 0:
                time.sleep(1.0)
                return self._send(req, _retries - 1)
            raise DigiKeyError(f"network error contacting DigiKey: {exc}") from exc


def _extract_datasheet(payload: dict) -> str | None:
    """Pull a datasheet URL out of a v4 keyword-search response, tolerating
    the slightly different shapes the API returns."""
    buckets = []
    for key in ("ExactMatches", "Products"):
        value = payload.get(key)
        if isinstance(value, list):
            buckets.extend(value)
    # Some responses nest under ProductsV4 or similar.
    for product in buckets:
        if not isinstance(product, dict):
            continue
        for field in ("DatasheetUrl", "PrimaryDatasheet", "datasheetUrl"):
            url = product.get(field)
            if url:
                return _normalize(url)
    return None


def _normalize(url: str) -> str:
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    return url
