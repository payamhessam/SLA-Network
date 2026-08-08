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
import os
import re
import subprocess

# OpenSSH options re-enabling the legacy algorithms these old Cisco devices require, forcing
# password/keyboard-interactive auth, and disabling host-key prompts/storage. Read-only.
_SSH_OPTS = [
    "-tt",
    "-o", "KexAlgorithms=+diffie-hellman-group14-sha1",
    "-o", "HostKeyAlgorithms=+ssh-rsa",
    "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
    "-o", "MACs=+hmac-sha1,hmac-sha1-96",
    "-o", "Ciphers=+aes128-cbc,aes192-cbc,aes256-cbc,3des-cbc",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "GlobalKnownHostsFile=/dev/null",
    "-o", "PreferredAuthentications=keyboard-interactive,password",
    "-o", "PubkeyAuthentication=no",
    "-o", "NumberOfPasswordPrompts=1",
    "-o", "ConnectTimeout=15",
    "-o", "LogLevel=ERROR",
]
# Total wall-clock budget for a session, generous enough to wait for a push-MFA approval.
_SSH_TIMEOUT = 150

# Fixed read-only command set: (command, ntc-templates key). Unsupported commands on a given
# platform simply return an "% Invalid input" banner we detect and skip (a router has no
# VLANs, a switch has no BGP, etc.).
_COMMANDS = [
    ("show version", "version"),
    ("show inventory", "inventory"),
    ("show vlan brief", "vlans"),
    ("show interfaces status", "if_status"),
    ("show interfaces counters errors", "if_errors"),
    ("show cdp neighbors detail", "cdp"),
    ("show lldp neighbors detail", "lldp"),
    ("show spanning-tree summary", "stp"),
    ("show ip ospf neighbor", "ospf"),
    ("show ip bgp summary", "bgp"),
    ("show ip eigrp neighbors", "eigrp"),
    ("show ip route", "routes"),
    ("show power inline", "poe"),
    ("show environment all", "env"),
    ("show logging", "logging"),
    ("show running-config", "config"),
]

# Cisco severity levels (the N in %FACILITY-N-MNEMONIC).
_SEV = {0: "Emergency", 1: "Alert", 2: "Critical", 3: "Error", 4: "Warning", 5: "Notice", 6: "Info", 7: "Debug"}

# Read-only Cisco knowledge base: syslog mnemonic -> (plain-English cause, recommended action).
# Matched by substring against the mnemonic so families (e.g. any *UPDOWN) resolve.
_SYSLOG_KB = {
    "UPDOWN": ("A link or line protocol changed state (interface up/down).",
               "Check the cable/SFP and the neighbour on that port; if it flaps repeatedly, replace the transceiver or patch lead, or shut the port if unused."),
    "CPUHOG": ("A process held the CPU too long — control-plane overload.",
               "Identify the process with 'show processes cpu sorted'; review ACLs/SNMP/broadcast load and plan a code or capacity upgrade."),
    "HIGHCPU": ("Sustained high CPU utilisation.",
                "Use 'show processes cpu sorted' to find the offender; check for loops, excessive logging, or SNMP polling."),
    "MALLOCFAIL": ("The device failed to allocate memory — memory exhaustion.",
                   "Free memory (remove unused features), schedule a maintenance reload, and evaluate a memory/software upgrade."),
    "MEMORY": ("Low free memory.",
               "Check 'show memory statistics'; reload during a maintenance window if it keeps degrading; consider a software upgrade."),
    "THERMAL": ("A temperature sensor crossed a threshold — thermal risk.",
                "Inspect airflow, fans and ambient/rack temperature immediately; clear obstructions; replace a failed fan tray."),
    "FAN": ("A fan failed or is degraded.",
            "Replace the fan tray; verify airflow and ambient temperature until resolved."),
    "PbENVMON": ("Environmental monitor alarm (temperature/fan/power).",
                 "Check 'show environment all'; address the specific failing sensor (cooling or power)."),
    "PSU": ("A power supply fault or redundancy loss.",
            "Verify both feeds and PSUs; replace the failed unit to restore N+1 redundancy."),
    "PWR": ("A power event was logged.",
            "Check 'show power' / 'show environment power'; confirm both supplies are online."),
    "ILPOWER": ("A PoE (inline power) event on a port.",
                "Check the powered device and 'show power inline'; a controller/AP may have lost power — verify the budget and cabling."),
    "STACKMGR": ("A stack member changed state (join/leave/reload).",
                 "Confirm all members are up with 'show switch'; check stack cables and member power if a member dropped."),
    "SPANTREE": ("A spanning-tree event — possible loop, BPDU guard, or topology change.",
                 "Investigate immediately for a bridging loop; check for unauthorised switches and confirm root bridge placement."),
    "PSECURE_VIOLATION": ("A port-security violation — an unexpected MAC appeared.",
                          "Identify the offending MAC/port; if legitimate, adjust the allowed list, otherwise treat as a security event."),
    "ADJCHG": ("An OSPF adjacency changed state.",
               "Check the neighbour and the interconnecting link/interface for flaps or MTU/auth mismatches."),
    "ADJCHANGE": ("A BGP peer changed state.",
                  "Verify the peer reachability, session config, and any upstream link issues."),
    "NBRCHANGE": ("An EIGRP neighbour changed state.",
                  "Check the link to the neighbour for flaps, MTU or authentication mismatches."),
    "DUPADDR": ("A duplicate IP address was detected.",
                "Locate and remove the conflicting host; verify DHCP scopes and static assignments."),
    "CONFIG": ("The running configuration was changed.",
               "Review who/what changed it ('show archive log config all' / AAA logs); confirm it was authorised."),
    "LOGIN_FAILED": ("Failed login attempt(s).",
                     "If repeated, investigate for brute force; confirm source and lock down management access."),
}

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
    "Alerts / recommendations": ["show logging"],
    "Routing (BGP/EIGRP/static)": ["show ip route", "show ip bgp summary", "show ip eigrp neighbors"],
}


def _recommend(mnemonic: str, severity: int) -> tuple[str, str]:
    """Look up a syslog mnemonic in the Cisco knowledge base; fall back to a severity-based
    generic recommendation. Returns (likely cause, recommended action)."""
    up = (mnemonic or "").upper()
    for key, (cause, action) in _SYSLOG_KB.items():
        if key in up:
            return cause, action
    if severity <= 2:
        return "A critical device event was logged.", "Investigate this event now; correlate with interface/power/temperature state and engage on-call if service is affected."
    if severity == 3:
        return "An error condition was logged.", "Review the message context and the affected component; remediate before it escalates."
    return "A notable event was logged.", "Monitor; act if it recurs or correlates with a service impact."

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
    # merge interface error counters (show interfaces counters errors) by interface name
    for r in _rows(parsed.get("if_errors")):
        port = r.get("interface") or r.get("port")
        if not port:
            continue
        entry = if_status.setdefault(port, {})
        entry["FCS/CRC Errors"] = r.get("fcs_errors") or r.get("crc") or r.get("fcs")
        entry["Align Errors"] = r.get("align_errors") or r.get("align")
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

    # Spanning tree — parse the per-VLAN counts from `show spanning-tree summary`
    stp_raw = raw.get("stp", "")
    stp_rows = []
    if stp_raw and not _unsupported(stp_raw):
        for m in re.finditer(r"^\s*(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$", stp_raw, re.M):
            name = m.group(1)
            if name.lower() == "name":
                continue
            stp_rows.append({"Instance": name, "Blocking": m.group(2), "Listening": m.group(3),
                             "Learning": m.group(4), "Forwarding": m.group(5), "Active": m.group(6)})
    if stp_rows:
        tables["Spanning Tree"] = stp_rows

    # Environmental & PoE — PoE per port (show power inline) + sensors (show environment all)
    env_rows = []
    for r in _rows(parsed.get("poe")):
        env_rows.append({"Category": "PoE", "Switch/Module": "-", "Component/Interface": r.get("interface") or r.get("port"),
                         "State": r.get("oper_state") or r.get("oper") or r.get("admin_state"), "Temperature C": "-",
                         "Fan RPM": "-", "Watts": r.get("power") or r.get("watts"), "Details": r.get("device") or r.get("class")})
    for r in _rows(parsed.get("env")):
        env_rows.append({"Category": r.get("type") or r.get("sensor") or "Sensor", "Switch/Module": r.get("module") or "-",
                         "Component/Interface": r.get("sensor") or r.get("descr") or r.get("name") or "-",
                         "State": r.get("state") or r.get("status"), "Temperature C": r.get("temperature") or r.get("temp"),
                         "Fan RPM": r.get("speed") or r.get("fan_speed"), "Watts": r.get("power"), "Details": r.get("reading")})
    if env_rows:
        tables["Environmental and PoE"] = env_rows

    # Alerts + Recommendations — parse the device syslog (show logging), read-only.
    log_raw = raw.get("logging", "")
    alert_rows, recs = [], []
    if log_raw and not _unsupported(log_raw):
        # %FACILITY-SEVERITY-MNEMONIC: message  (optionally prefixed by a timestamp)
        pat = re.compile(r"(?P<ts>[*.]?\w{3}\s+\d+\s+[\d:.]+)?[^%]*%(?P<fac>[A-Z0-9_]+)-(?P<sev>\d)-(?P<mnem>[A-Z0-9_]+):\s*(?P<msg>.+)")
        seen: dict[tuple, dict] = {}
        for line in log_raw.splitlines():
            m = pat.search(line)
            if not m:
                continue
            sev = int(m.group("sev")); fac = m.group("fac"); mnem = m.group("mnem"); msg = m.group("msg").strip()
            alert_rows.append({"Severity": _SEV.get(sev, str(sev)), "Source": fac, "Instance": mnem,
                               "Message": msg[:200], "Age": (m.group("ts") or "").strip(), "Acknowledged": "—"})
            key = (fac, mnem)
            entry = seen.get(key)
            if entry is None:
                cause, action = _recommend(f"{fac} {mnem}", sev)  # match KB on facility + mnemonic
                seen[key] = {"sev": sev, "count": 1, "fac": fac, "mnem": mnem, "cause": cause, "action": action, "sample": msg[:160]}
            else:
                entry["count"] += 1
                entry["sev"] = min(entry["sev"], sev)
        alert_rows = alert_rows[-120:][::-1]  # most-recent first, capped
        # Build prioritised recommendations from the distinct actionable events (sev<=4).
        for e in sorted((v for v in seen.values() if v["sev"] <= 4), key=lambda v: (v["sev"], -v["count"]))[:15]:
            pr = "P1" if e["sev"] <= 2 else ("P2" if e["sev"] == 3 else "P3")
            recs.append({"Priority": pr, "Severity": _SEV.get(e["sev"], str(e["sev"])),
                         "Finding": f"{e['count']}x %{e['fac']}-{e['sev']}-{e['mnem']}", "Likely cause": e["cause"],
                         "Recommended action": e["action"], "Latest message": e["sample"]})
    # Interface-error and environment findings feed the recommendation panel too.
    bad_ifs = [k for k, v in (if_status or {}).items()
               if str(v.get("FCS/CRC Errors") or "0").strip() not in ("0", "", "None") or str(v.get("Align Errors") or "0").strip() not in ("0", "", "None")]
    if bad_ifs:
        recs.append({"Priority": "P2", "Severity": "Error", "Finding": f"{len(bad_ifs)} interface(s) with FCS/CRC or alignment errors",
                     "Likely cause": "Physical-layer problem (bad cable/SFP, duplex mismatch, or EMI).",
                     "Recommended action": f"Inspect {', '.join(bad_ifs[:6])}: reseat/replace the cable or transceiver and confirm duplex/speed autoneg.",
                     "Latest message": ""})
    if alert_rows:
        tables["Alerts"] = alert_rows
    if recs:
        tables["Recommendations (SSH)"] = recs

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


def collect(host: str, username: str, password: str) -> dict:
    """Connect over READ-ONLY OpenSSH, run the allow-list, and return parsed tables.

    The session sends the password once and then waits (up to `_SSH_TIMEOUT`) for the
    device's auth backend to complete — this is where a push-MFA (Microsoft Authenticator)
    approval happens out-of-band. The password is passed to sshpass via the SSHPASS env var
    (never on argv, never stored, never logged). Only `show` commands are ever sent."""
    cmds = ["terminal length 0"] + COMMANDS + ["exit"]
    args = ["sshpass", "-e", "ssh", *_SSH_OPTS, f"{username}@{host}"]
    env = dict(os.environ, SSHPASS=password)
    try:
        proc = subprocess.run(args, input="\n".join(cmds) + "\n", capture_output=True,
                              text=True, timeout=_SSH_TIMEOUT, env=env)
    except FileNotFoundError:
        return {"status": "error", "message": "SSH client (openssh-client/sshpass) is not installed on the server."}
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "message": "Timed out waiting for the device — approval not received, or the device is slow."}
    transcript = (proc.stdout or "") + (proc.stderr or "")
    low = transcript.lower()
    if "permission denied" in low or "authentication failed" in low:
        return {"status": "auth_failed", "message": "Authentication failed (wrong password, or the MFA approval was declined/expired)."}
    if any(t in low for t in ("unable to negotiate", "no matching", "connection timed out", "connection refused",
                              "no route to host", "could not resolve", "operation timed out")):
        return {"status": "unreachable", "message": "Network not available — could not reach the device on SSH."}
    result = parse_transcript(transcript)
    if result.get("status") != "ok":
        # Connected but produced no parseable output (e.g. approval never completed).
        result.setdefault("message", "Connected but no command output was returned.")
    return result
