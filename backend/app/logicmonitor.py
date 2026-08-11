"""Read-only LogicMonitor API client with LMv1 request signing.

Wraps httpx with a pooled, keep-alive client per portal and signs every request using
the LMv1 HMAC-SHA256 scheme. The client is deliberately read-only: any non-GET method
is rejected locally before it can leave the process, TLS certificates are always
verified, and 429/Retry-After responses are honoured with backoff. This is the single
choke point through which the whole application talks to LogicMonitor.
"""
import asyncio, base64, hashlib, hmac, time
from urllib.parse import urlencode
import httpx
from .config import get_settings

_clients: dict[str, httpx.AsyncClient] = {}
_clients_lock = asyncio.Lock()


class _RateLimiter:
    """Token bucket shared by every LogicMonitor request in this process.

    LogicMonitor publishes a tenant-wide budget (measured on this portal:
    x-rate-limit-limit 700 per x-rate-limit-window 60s). A full YTD backfill is ~27k
    requests and, at CONCURRENCY=16 with ~450 ms round-trips, would otherwise offer
    ~2,100 req/min — roughly 3x the budget. Reacting to the resulting 429s technically
    works (see the retry path in get()), but it burns a budget that live monitoring and
    other LogicMonitor consumers in the tenant share. This keeps us under the limit by
    construction instead.
    """

    def __init__(self, per_minute: int):
        self._rate = per_minute / 60.0            # tokens per second
        self._capacity = max(1.0, per_minute / 6.0)  # allow a short burst (~10s worth)
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            await asyncio.sleep(wait)


_limiters: dict[int, _RateLimiter] = {}


def _limiter(per_minute: int) -> _RateLimiter | None:
    if per_minute <= 0:
        return None  # explicitly disabled
    lim = _limiters.get(per_minute)
    if lim is None:
        lim = _limiters[per_minute] = _RateLimiter(per_minute)
    return lim


async def _shared_client(base_url: str) -> httpx.AsyncClient:
    """One pooled, keep-alive client per portal so we don't pay a TLS handshake per request."""
    client = _clients.get(base_url)
    if client is None:
        async with _clients_lock:
            client = _clients.get(base_url)
            if client is None:
                client = httpx.AsyncClient(base_url=base_url, verify=True, timeout=httpx.Timeout(30.0, connect=15.0),
                                           limits=httpx.Limits(max_connections=24, max_keepalive_connections=24, keepalive_expiry=60.0))
                _clients[base_url] = client
    return client


def _retry_after(response: httpx.Response, default: float = 5.0) -> float:
    for header in ("Retry-After", "X-Rate-Limit-Window"):
        value = response.headers.get(header)
        if value:
            try:
                return min(60.0, max(1.0, float(value)))
            except ValueError:
                pass
    return default


class LogicMonitorClient:
    """Strictly read-only LMv1 client. Any non-GET request is rejected locally."""
    def __init__(self):
        self.settings = get_settings()

    def _headers(self, resource: str):
        epoch = str(int(time.time() * 1000))
        signing_resource = resource.removeprefix("/santaba/rest")
        signature = base64.b64encode(hmac.new(self.settings.access_key.encode(), f"GET{epoch}{signing_resource}".encode(), hashlib.sha256).hexdigest().encode()).decode()
        return {"Authorization": f"LMv1 {self.settings.access_id}:{signature}:{epoch}", "Accept": "application/json", "X-Version": "3"}

    async def get(self, resource: str, params: dict | None = None):
        if not resource.startswith("/santaba/rest/"): raise ValueError("Unsupported LogicMonitor resource")
        client = await _shared_client(self.settings.lm_portal_url)
        limiter = _limiter(getattr(self.settings, "lm_rate_limit_per_minute", 500))
        attempts = 0
        while True:
            attempts += 1
            if limiter is not None:
                await limiter.acquire()  # stay inside the tenant-wide LM budget by construction
            try:
                response = await client.get(resource, params=params, headers=self._headers(resource))
            except (httpx.TimeoutException, httpx.TransportError):
                if attempts <= 4:
                    await asyncio.sleep(min(2 ** attempts, 15))
                    continue
                raise
            if response.status_code == 429 and attempts <= 6:
                await asyncio.sleep(_retry_after(response))
                continue
            if response.status_code in (500, 502, 503, 504) and attempts <= 4:
                await asyncio.sleep(min(2 ** attempts, 15))
                continue
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and payload.get("status") == 429 and attempts <= 6:
                await asyncio.sleep(_retry_after(response))
                continue
            if isinstance(payload, dict) and payload.get("status") not in (None, 200):
                raise httpx.HTTPStatusError("LogicMonitor rejected the read-only request", request=response.request, response=response)
            return payload

    async def devices(self):
        if not (self.settings.lm_portal_url and self.settings.access_id and self.settings.access_key): return []
        items, offset = [], 0
        while True:
            data = await self.get("/santaba/rest/device/devices", {"size": 1000, "offset": offset})
            body = data.get("data") if isinstance(data.get("data"), dict) else data
            batch = body.get("items", [])
            items.extend(batch)
            if len(batch) < 1000: return items
            offset += len(batch)

    @staticmethod
    def body(payload):
        return payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload

    async def find_device(self, hostname: str, management_ip: str | None):
        queries = []
        if management_ip: queries.append(f'name:"{management_ip}"')
        queries.extend([f'displayName:"{hostname}"', f'name:"{hostname}"'])
        for query in queries:
            payload = await self.get("/santaba/rest/device/devices", {"size": 20, "filter": query})
            items = self.body(payload).get("items", [])
            if len(items) == 1: return items[0], query.split(":", 1)[0]
            if len(items) > 1: return None, "Ambiguous"
        return None, "Not Found"

    async def _paged_items(self, resource: str, extra_params: dict | None = None) -> list:
        """Fetch every item across all pages (same pagination contract as devices()) so a
        device with more than one page of properties/datasources/instances is never silently
        truncated to just the first 1000."""
        items, offset = [], 0
        while True:
            params = {"size": 1000, "offset": offset, **(extra_params or {})}
            payload = await self.get(resource, params)
            batch = self.body(payload).get("items", [])
            items.extend(batch)
            if len(batch) < 1000:
                return items
            offset += len(batch)

    async def properties(self, device_id: int):
        items = await self._paged_items(f"/santaba/rest/device/devices/{device_id}/properties")
        return {x.get("name"): x.get("value") for x in items if x.get("name")}

    async def applied_datasources(self, device_id: int):
        return await self._paged_items(f"/santaba/rest/device/devices/{device_id}/devicedatasources")

    async def instances(self, device_id: int, hds_id: int):
        return await self._paged_items(f"/santaba/rest/device/devices/{device_id}/devicedatasources/{hds_id}/instances")

    async def instance_data(self, device_id: int, hds_id: int, instance_id: int, start: int, end: int):
        payload = await self.get(f"/santaba/rest/device/devices/{device_id}/devicedatasources/{hds_id}/instances/{instance_id}/data", {"start": start, "end": end})
        return self.body(payload)


def match_device(local, candidates):
    ip = (local.management_ip or "").lower(); host = local.hostname.lower(); short = host.split(".")[0]
    rules = [
        ("Management IP", lambda d: ip and ip in {str(d.get("name", "")).lower(), str(d.get("displayName", "")).lower()}),
        ("Display name", lambda d: str(d.get("displayName", "")).lower() == host),
        ("Hostname", lambda d: str(d.get("name", "")).lower() == host),
        ("Short hostname", lambda d: str(d.get("name", "")).lower().split(".")[0] == short),
    ]
    for method, predicate in rules:
        found = [d for d in candidates if predicate(d)]
        if len(found) == 1: return found[0], method
        if len(found) > 1: return None, "Ambiguous"
    return None, "Not Found"
