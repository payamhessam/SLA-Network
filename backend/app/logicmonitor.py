import base64, hashlib, hmac, time
from urllib.parse import urlencode
import httpx
from .config import get_settings


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
        async with httpx.AsyncClient(base_url=self.settings.lm_portal_url, verify=True, timeout=30) as client:
            response = await client.get(resource, params=params, headers=self._headers(resource))
            response.raise_for_status()
            payload = response.json()
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

    async def properties(self, device_id: int):
        payload = await self.get(f"/santaba/rest/device/devices/{device_id}/properties", {"size": 1000})
        return {x.get("name"): x.get("value") for x in self.body(payload).get("items", []) if x.get("name")}

    async def applied_datasources(self, device_id: int):
        payload = await self.get(f"/santaba/rest/device/devices/{device_id}/devicedatasources", {"size": 1000})
        return self.body(payload).get("items", [])

    async def instances(self, device_id: int, hds_id: int):
        payload = await self.get(f"/santaba/rest/device/devices/{device_id}/devicedatasources/{hds_id}/instances", {"size": 1000})
        return self.body(payload).get("items", [])

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
