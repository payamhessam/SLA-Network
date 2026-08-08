"""Read-only, on-demand SSH collection to fill LogicMonitor gaps (admin only).

This module SSHes into a Cisco switch/router with an administrator-supplied credential
and runs ONLY a fixed allow-list of `show` commands to pull the facts LogicMonitor does
not expose (VLANs, interface duplex/speed, CDP/LLDP neighbours, inventory, spanning tree,
OSPF/BGP/EIGRP/static routing, environment/PoE, and the running-config). It is strictly
read-only:

  * only the hardcoded `_COMMANDS` `show` list is ever sent — the caller cannot supply
    commands, so there is no injection path and nothing can change the device;
  * `terminal length 0` is a per-session paging setting, not a configuration change;
  * config mode is never entered.

The password is used for the single session and then discarded — it is never persisted
or logged. Connection/auth failures are reported so the UI can say "Network not available".
"""
from datetime import datetime, timezone

# Fixed allow-list: (command, key). Unsupported commands on a given platform simply return
# an "% Invalid input" banner which we detect and skip — a router has no VLANs, etc.
_COMMANDS = [
    ("show version", "version"),
    ("show inventory", "inventory"),
    ("show vlan brief", "vlans"),
    ("show interfaces status", "if_status"),
    ("show cdp neighbors detail", "cdp"),
    ("show lldp neighbors detail", "lldp"),
    ("show spanning-tree", "stp"),
    ("show ip ospf neighbor", "ospf"),
    ("show ip bgp summary", "bgp"),
    ("show ip eigrp neighbors", "eigrp"),
    ("show ip route", "routes"),
    ("show power inline", "poe"),
    ("show environment all", "env"),
    ("show running-config", "config"),
]

_INVALID = ("% invalid input", "% incomplete", "% ambiguous", "% unrecognized", "invalid input detected")


def _unsupported(raw: str) -> bool:
    low = str(raw).lower()
    return any(tok in low for tok in _INVALID)


def _rows(parsed):
    """Normalise a textfsm result to a list of dicts (empty when unparsed/unsupported)."""
    return parsed if isinstance(parsed, list) else []


def _joined(v):
    return ", ".join(v) if isinstance(v, list) else (v or "")


def _map_tables(raw: dict, parsed: dict) -> dict:
    """Turn the collected command output into the device-detail table shape."""
    tables: dict[str, list] = {}

    # VLANs
    vlans = [{"VLAN ID": r.get("vlan_id") or r.get("vlan"), "Name": r.get("vlan_name") or r.get("name"),
              "Status": r.get("status"), "Ports": _joined(r.get("interfaces"))} for r in _rows(parsed.get("vlans"))]
    if vlans:
        tables["VLANs"] = vlans

    # Interface status (used to enrich the Interfaces table by name)
    if_status = {}
    for r in _rows(parsed.get("if_status")):
        port = r.get("port") or r.get("interface")
        if port:
            if_status[port] = {"Description": r.get("name"), "Status": r.get("status"), "VLAN": r.get("vlan"),
                               "Duplex": r.get("duplex"), "Speed": r.get("speed"), "Type": r.get("type")}
    if if_status:
        tables["_if_status"] = if_status  # internal, merged server-side into Interfaces

    # CDP / LLDP neighbours
    neigh = []
    for r in _rows(parsed.get("cdp")):
        neigh.append({"Protocol": "CDP", "Local Interface": r.get("local_interface"),
                      "Neighbor": r.get("destination_host") or r.get("neighbor_name"),
                      "Management IP": r.get("management_ip") or r.get("mgmt_ip"),
                      "Platform/Model": r.get("platform"), "Remote Port": r.get("remote_port")})
    for r in _rows(parsed.get("lldp")):
        neigh.append({"Protocol": "LLDP", "Local Interface": r.get("local_interface"),
                      "Neighbor": r.get("neighbor") or r.get("neighbor_name") or r.get("system_name"),
                      "Management IP": r.get("management_ip") or r.get("mgmt_ip"),
                      "Platform/Model": r.get("system_description") or r.get("capabilities"),
                      "Remote Port": r.get("neighbor_interface") or r.get("remote_port")})
    if neigh:
        tables["CDP-LLDP Neighbors"] = neigh

    # Inventory
    inv = [{"Component": r.get("name"), "Description": r.get("descr") or r.get("description"),
            "PID": r.get("pid"), "VID": r.get("vid"), "Serial Number": r.get("sn") or r.get("serial")}
           for r in _rows(parsed.get("inventory"))]
    if inv:
        tables["Inventory"] = inv

    # OSPF neighbours
    ospf = [{"Neighbor": r.get("neighbor_id") or r.get("neighbor"), "State": r.get("state"),
             "Neighbor Events": "—", "Restarts": "—", "Retransmit Queue": r.get("queue") or "—"}
            for r in _rows(parsed.get("ospf"))]
    if ospf:
        tables["OSPF Neighbors"] = ospf

    # Routing / BGP / EIGRP as SSH-only tables (not present in LogicMonitor for this fleet)
    routes = [{"Protocol": r.get("protocol"), "Network": r.get("network"), "Mask": r.get("mask") or r.get("prefix_length"),
               "Next Hop": r.get("nexthop_ip") or r.get("nexthop_if"), "Distance/Metric": f"{r.get('distance','')}/{r.get('metric','')}"}
              for r in _rows(parsed.get("routes"))]
    if routes:
        tables["IP Routes (SSH)"] = routes
    bgp = [{"Neighbor": r.get("bgp_neigh") or r.get("neighbor"), "AS": r.get("neigh_as") or r.get("as"),
            "State/PfxRcd": r.get("state_pfxrcd") or r.get("state"), "Up/Down": r.get("up_down") or r.get("uptime")}
           for r in _rows(parsed.get("bgp"))]
    if bgp:
        tables["BGP Summary (SSH)"] = bgp
    eigrp = [{"Peer": r.get("address") or r.get("peer"), "Interface": r.get("interface"),
              "Hold": r.get("hold"), "Uptime": r.get("uptime"), "SRTT": r.get("srtt"), "Q Cnt": r.get("q_cnt")}
             for r in _rows(parsed.get("eigrp"))]
    if eigrp:
        tables["EIGRP Neighbors (SSH)"] = eigrp

    # Config backups — store the running-config verbatim (read-only) for the tab
    config = raw.get("config")
    if config and not _unsupported(config):
        tables["Configuration Backups"] = [{"Running Config": "Pulled directly via SSH (read-only)",
                                            "Running Saved": "—", "Startup Config": "See running-config below",
                                            "Startup Saved": "—", "Collected": datetime.now(timezone.utc).isoformat()}]

    # Environmental / PoE (kept as raw text panels — templates vary widely by platform)
    return tables


def collect(host: str, username: str, password: str, device_type: str = "cisco_ios") -> dict:
    """Connect read-only and collect. Returns a status dict; on success `data` holds the
    normalised tables, the running-config text, and the raw command outputs."""
    try:
        from netmiko import ConnectHandler
        from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException
    except Exception:
        return {"status": "error", "message": "SSH support (netmiko) is not installed on the server."}

    # IOS/IOS-XE covers the Catalyst switches and the routers in this fleet.
    conn_params = {"device_type": "cisco_ios", "host": host, "username": username, "password": password,
                   "fast_cli": False, "conn_timeout": 12, "banner_timeout": 15, "auth_timeout": 15}
    try:
        conn = ConnectHandler(**conn_params)
    except NetmikoAuthenticationException:
        return {"status": "auth_failed", "message": "SSH authentication failed for this device."}
    except NetmikoTimeoutException:
        return {"status": "unreachable", "message": "Network not available — the device did not respond on SSH."}
    except Exception as exc:
        # ConnectionRefused, no route, DNS, etc. all mean we could not reach it.
        return {"status": "unreachable", "message": f"Network not available ({type(exc).__name__})."}

    raw: dict = {}
    parsed: dict = {}
    try:
        conn.send_command("terminal length 0", expect_string=r"#")  # per-session paging off (not a config change)
        for cmd, key in _COMMANDS:
            try:
                out = conn.send_command(cmd, use_textfsm=(key != "config"), read_timeout=60)
            except Exception:
                continue
            if isinstance(out, list):
                parsed[key] = out
                raw[key] = out
            else:
                raw[key] = out
                if not _unsupported(out):
                    # keep raw text for panels even when no textfsm template matched
                    parsed.setdefault(key, [])
    finally:
        try:
            conn.disconnect()
        except Exception:
            pass

    tables = _map_tables(raw, parsed)
    return {
        "status": "ok",
        "data": {
            "tables": tables,
            "config": raw.get("config") if not _unsupported(raw.get("config", "")) else None,
            "raw": {k: (v if isinstance(v, str) else "") for k, v in raw.items()},
            "commands": [c for c, _ in _COMMANDS],
        },
    }
