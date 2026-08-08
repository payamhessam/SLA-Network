"""Manual device-fact collection to fill LogicMonitor gaps (admin only).

The monitored account (pa-phessamfar) has MFA, so the application can NOT connect to the
devices itself. Instead the admin opens their own SSH session to the switch/router, runs a
fixed read-only set of Cisco `show` commands, and pastes the whole transcript back. This
module builds the copy-paste command block and parses the pasted output into the
device-detail tables (VLANs, interface duplex/speed, CDP/LLDP neighbours, inventory,
spanning tree, OSPF/BGP/EIGRP/static routing, environment/PoE, running-config).

Everything here is read-only by construction: the command block contains only `show`
commands plus a per-session `terminal length 0`; nothing this module produces can change a
device, and no credential is ever handled by the application.
"""
import re

# Fixed read-only command set: (command, ntc-templates key). Unsupported commands on a given
# platform simply return an "% Invalid input" banner we detect and skip (a router has no
# VLANs, a switch has no BGP, etc.).
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

# The ready-to-copy block the admin runs in their own (MFA-authenticated) SSH session.
SCRIPT = "terminal length 0\n" + "\n".join(c for c, _ in _COMMANDS)
COMMANDS = [c for c, _ in _COMMANDS]

# Which detail tables each command can fill — used to show the admin what a paste will cover.
GAP_COMMANDS = {
    "VLANs": ["show vlan brief"],
    "Interfaces": ["show interfaces status"],
    "CDP-LLDP Neighbors": ["show cdp neighbors detail", "show lldp neighbors detail"],
    "Spanning Tree": ["show spanning-tree"],
    "Inventory": ["show inventory", "show version"],
    "OSPF Neighbors": ["show ip ospf neighbor"],
    "Environmental and PoE": ["show environment all", "show power inline"],
    "Configuration Backups": ["show running-config"],
    "Routing (BGP/EIGRP/static)": ["show ip route", "show ip bgp summary", "show ip eigrp neighbors"],
}

_INVALID = ("% invalid input", "% incomplete", "% ambiguous", "% unrecognized", "invalid input detected")


def _unsupported(raw: str) -> bool:
    low = str(raw or "").lower()
    return any(tok in low for tok in _INVALID)


def _rows(parsed):
    return parsed if isinstance(parsed, list) else []


def _joined(v):
    return ", ".join(v) if isinstance(v, list) else (v or "")


def _split(transcript: str) -> dict:
    """Split a pasted transcript into {key: command-output}. Order-independent: each command
    string is located by its echo, the hits are sorted by position, and each block runs to
    the next command echo. Works whether or not the admin ran the commands in script order."""
    found = []
    for cmd, key in _COMMANDS:
        i = transcript.find(cmd)
        if i >= 0:
            found.append((i, cmd, key))
    found.sort()
    out = {}
    for n, (i, cmd, key) in enumerate(found):
        start = i + len(cmd)
        end = found[n + 1][0] if n + 1 < len(found) else len(transcript)
        block = transcript[start:end]
        block = re.sub(r"\n\S+#\s*$", "", block).strip("\r\n ")  # drop trailing prompt line
        out[key] = block
    return out


def _parse(blocks: dict) -> tuple[dict, dict]:
    """Structure each command block with ntc-templates; keep raw text alongside."""
    parsed, raw = {}, {}
    try:
        from ntc_templates.parse import parse_output
    except Exception:
        parse_output = None
    for key, text in blocks.items():
        raw[key] = text
        if key == "config" or parse_output is None or _unsupported(text) or not text:
            continue
        cmd = next((c for c, k in _COMMANDS if k == key), None)
        try:
            result = parse_output(platform="cisco_ios", command=cmd, data=text)
            if isinstance(result, list):
                parsed[key] = result
        except Exception:
            pass
    return parsed, raw


def _map_tables(raw: dict, parsed: dict) -> dict:
    """Turn the collected command output into the device-detail table shape."""
    tables: dict[str, list] = {}

    vlans = [{"VLAN ID": r.get("vlan_id") or r.get("vlan"), "Name": r.get("vlan_name") or r.get("name"),
              "Status": r.get("status"), "Ports": _joined(r.get("interfaces"))} for r in _rows(parsed.get("vlans"))]
    if vlans:
        tables["VLANs"] = vlans

    if_status = {}
    for r in _rows(parsed.get("if_status")):
        port = r.get("port") or r.get("interface")
        if port:
            if_status[port] = {"Description": r.get("name"), "Status": r.get("status"), "VLAN": r.get("vlan"),
                               "Duplex": r.get("duplex"), "Speed": r.get("speed"), "Type": r.get("type")}
    if if_status:
        tables["_if_status"] = if_status

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

    inv = [{"Component": r.get("name"), "Description": r.get("descr") or r.get("description"),
            "PID": r.get("pid"), "VID": r.get("vid"), "Serial Number": r.get("sn") or r.get("serial")}
           for r in _rows(parsed.get("inventory"))]
    if inv:
        tables["Inventory"] = inv

    ospf = [{"Neighbor": r.get("neighbor_id") or r.get("neighbor"), "State": r.get("state"),
             "Neighbor Events": "—", "Restarts": "—", "Retransmit Queue": r.get("queue") or "—"}
            for r in _rows(parsed.get("ospf"))]
    if ospf:
        tables["OSPF Neighbors"] = ospf

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

    config = raw.get("config")
    if config and not _unsupported(config):
        from datetime import datetime, timezone
        tables["Configuration Backups"] = [{"Running Config": "Pulled directly via SSH (read-only)",
                                            "Running Saved": "—", "Startup Config": "See running-config below",
                                            "Startup Saved": "—", "Collected": datetime.now(timezone.utc).isoformat()}]
    return tables


def parse_transcript(transcript: str) -> dict:
    """Parse an admin-pasted `show`-command transcript into the device-detail tables.
    Returns a status dict; on success `data` holds the normalised tables, the running-config
    text, and the raw per-command outputs. Read-only — this only reads text the admin pasted."""
    if not transcript or not transcript.strip():
        return {"status": "no_data", "message": "No output was pasted."}
    blocks = _split(transcript)
    if not blocks:
        return {"status": "no_data", "message": "Could not find any of the expected show-command output in the pasted text."}
    parsed, raw = _parse(blocks)
    tables = _map_tables(raw, parsed)
    if not tables:
        return {"status": "no_data", "message": "The pasted output was recognised but produced no fillable data — check that terminal length 0 was set and the full output was copied."}
    config = raw.get("config") if not _unsupported(raw.get("config", "")) else None
    return {
        "status": "ok",
        "data": {"tables": tables, "config": config, "raw": raw, "commands": COMMANDS,
                 "recognised": sorted(k for k in blocks if not _unsupported(blocks[k]))},
    }
