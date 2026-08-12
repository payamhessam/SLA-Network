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
    ("show ip route 0.0.0.0", "route_default"),
    ("show standby brief", "standby"),
    ("show etherchannel summary", "etherchannel"),
    ("show power inline", "poe"),
    ("show environment all", "env"),
    ("show logging", "logging"),
    ("show running-config", "config"),
]

# Cisco severity levels (the N in %FACILITY-N-MNEMONIC).
_SEV = {0: "Emergency", 1: "Alert", 2: "Critical", 3: "Error", 4: "Warning", 5: "Notice", 6: "Info", 7: "Debug"}

# Read-only Cisco knowledge base for Catalyst 9200/9300/9500 (IOS-XE), 2960/3750 (IOS) and
# ISR 4331 (IOS-XE): syslog facility/mnemonic -> (plain-English cause, recommended action).
# Matched as a substring of "FACILITY MNEMONIC"; MOST SPECIFIC KEYS FIRST so a precise entry
# wins over a broad family. Actions are engineer guidance — the app itself only runs `show`.
_SYSLOG_KB = {
    # ---- Interface / physical layer ----
    "DUPLEX_MISMATCH": ("Half/full duplex mismatch between the two ends of a link.",
                        "Set both ends to the same speed/duplex (or 'auto' on both); mismatches cause late collisions and drops."),
    "NATIVE_VLAN_MISMATCH": ("802.1Q trunk native-VLAN mismatch between neighbours.",
                             "Align the native VLAN on both trunk ends; a mismatch risks VLAN leakage and STP issues."),
    "UNQUALIFIED_TRANSCEIVER": ("A non-Cisco / unqualified optic was detected.",
                                "Replace with a Cisco-qualified transceiver, or accept the risk knowingly; unqualified optics can flap or under-perform."),
    "SFP_SECURITY": ("Transceiver security/identification check failed.",
                     "Reseat or replace the SFP with a supported part; clean the fibre connectors."),
    "GBIC_SECURITY": ("Transceiver security/identification check failed.",
                      "Reseat or replace the SFP/GBIC with a supported part."),
    "SFF8472": ("Optical transceiver DOM threshold crossed (Rx/Tx power, temp or bias).",
                "Check 'show interfaces transceiver detail'; clean the fibre, verify distance/optic type, replace a failing optic."),
    "TRANSCEIVER": ("Transceiver event.",
                    "Check 'show interfaces transceiver'; reseat/clean/replace the optic if levels are out of range."),
    "UDLD": ("UDLD detected a unidirectional link and disabled the port.",
             "Inspect the fibre pair/patch (one strand or a dirty connector is the usual cause); replace the cable/optic, then recover the port."),
    "STORM_CONTROL": ("Broadcast/multicast/unknown-unicast storm exceeded the configured threshold.",
                      "Find the traffic source; rule out an L2 loop; tune storm-control levels if the traffic is legitimate."),
    "MACFLAP": ("The same MAC is flapping between two ports — usually an L2 loop or a dual-homed host.",
                "Check the topology/STP on the two ports; remove the loop or fix the dual-homing (EtherChannel/portfast)."),
    "MAC_MOVE": ("A host MAC moved rapidly between ports (flapping).",
                 "Investigate for an L2 loop or a mis-cabled dual-homed device on the two ports involved."),
    "MATM": ("MAC address-table move/flap notification.",
             "Check for a bridging loop or a device connected to two access ports simultaneously."),
    "ERR_DISABLE": ("A port was error-disabled by a protection feature.",
                    "Find the reason with 'show interfaces status err-disabled'; fix the cause (bpduguard/security/udld/link-flap), then recover the port."),
    "ERRDISABLE": ("A port was error-disabled.",
                   "Identify the trigger, remediate it, then re-enable; consider 'errdisable recovery' for transient causes."),
    "LOOPBACK": ("Keepalive loopback detected on an interface.",
                 "Check for a physical loop/mis-patch on that port; remove it."),
    "CANNOT_BUNDLE": ("An EtherChannel member could not bundle (parameter mismatch).",
                      "Make speed/duplex/trunk mode and allowed-VLAN identical across all bundle members."),
    "PORT_SUSPENDED": ("An EtherChannel port was suspended (LACP/PAgP mismatch).",
                       "Verify channel-group mode and matching config on both sides."),
    "UPDOWN": ("A link or line protocol changed state (interface up/down).",
               "Check the cable/SFP and the neighbour on that port; if it flaps, replace the transceiver/patch lead or shut the port if unused."),
    # ---- Spanning tree ----
    "ROOTGUARD_BLOCK": ("Root guard blocked a superior BPDU — an unexpected switch is trying to become root.",
                        "Locate and remove the unauthorised switch on that port; confirm root-bridge placement."),
    "LOOPGUARD_BLOCK": ("Loop guard blocked a port (stopped receiving BPDUs).",
                        "Check for a unidirectional link/fibre fault on that port."),
    "BPDUGUARD": ("BPDU guard error-disabled an access port that received a BPDU.",
                  "A switch/bridge was connected to an access (PortFast) port — remove it; recover the port afterwards."),
    "BRIDGE_ASSURANCE": ("Bridge Assurance blocked a port (no bidirectional BPDUs).",
                         "Verify the peer is up and sending BPDUs; check for a unidirectional link."),
    "PVID": ("VLAN/PVID inconsistency on a trunk.",
             "Align the native/allowed VLANs on both trunk ends."),
    "RECV_1Q_NON_TRUNK": ("Tagged frames received on a non-trunk port.",
                          "Fix the trunk/access mismatch on both ends of the link."),
    "SPANTREE": ("A spanning-tree event — possible loop, guard action, or topology change.",
                 "Investigate for a bridging loop; check for unauthorised switches and confirm root-bridge placement."),
    # ---- EtherChannel ----
    "PAGP": ("PAgP EtherChannel negotiation event.",
             "Confirm matching channel mode and member parameters on both switches."),
    "LACP": ("LACP EtherChannel event.",
             "Confirm matching LACP mode/rate and member speed/duplex on both switches."),
    # ---- FHRP (HSRP / VRRP / GLBP) ----
    "BADAUTH": ("First-hop redundancy (HSRP/VRRP) authentication mismatch.",
                "Align the HSRP/VRRP group authentication on all peers."),
    "HSRP": ("HSRP gateway state change (active/standby transition).",
             "Frequent changes indicate an uplink flap or priority/preempt issue — check the group's interfaces and tracking."),
    "VRRP": ("VRRP master/backup state change.",
             "Check the interconnect and priority/preempt; flaps point to link instability."),
    "GLBP": ("GLBP AVG/AVF state change.",
             "Verify links and group priority/weighting; flaps indicate uplink instability."),
    # ---- L2 security ----
    "DHCP_SNOOPING": ("DHCP snooping dropped traffic (untrusted port or rate limit).",
                      "A rogue DHCP server or a mis-set trust boundary is likely — trust only uplinks/servers; check the offending port."),
    "SW_DAI": ("Dynamic ARP Inspection dropped ARP — possible ARP spoofing or a missing binding.",
               "Verify DHCP-snooping bindings and DAI trust on the port; investigate the source if unexpected."),
    "INVALID_ARP": ("Dynamic ARP Inspection dropped an invalid ARP.",
                    "Check the DHCP-snooping binding table and trust config; treat unexpected sources as a security event."),
    "IP_SOURCE": ("IP Source Guard dropped traffic (IP/MAC not in the binding table).",
                  "Confirm the host uses DHCP or add a static binding; investigate spoofing if unexpected."),
    "DOT1X": ("802.1X authentication failed on a port.",
              "Check the supplicant credentials, RADIUS/ISE reachability, and the port's auth config."),
    "AUTHMGR": ("Session-manager authentication failure (802.1X/MAB/web-auth).",
                "Verify RADIUS/ISE reachability and the endpoint's credentials/policy."),
    "MAB": ("MAC Authentication Bypass failed for an endpoint.",
            "Confirm the MAC is authorised in ISE/RADIUS and the fallback order on the port."),
    "PSECURE_VIOLATION": ("Port-security violation — an unexpected MAC appeared on a locked port.",
                          "Identify the MAC/port; if legitimate adjust the allowed list, otherwise treat as a security event."),
    "DUPADDR": ("A duplicate IP address was detected on the segment.",
                "Locate and remove the conflicting host; verify DHCP scopes and static assignments."),
    # ---- Routing ----
    "STUCKINACTIVE": ("EIGRP route stuck-in-active (SIA) — a neighbour did not answer a query in time.",
                      "Find the slow/flapping neighbour or link; summarise routes and check for query-range/CPU issues."),
    "SIA": ("EIGRP route stuck-in-active — a neighbour did not answer a query in time.",
            "Find the slow/flapping neighbour or link; add route summarisation to bound the EIGRP query domain."),
    "DUAL": ("EIGRP DUAL event.",
             "Check for a flapping route or slow neighbour; summarisation reduces query scope."),
    "MAXPFX": ("BGP maximum-prefix limit reached from a peer.",
               "The peer advertised too many prefixes — verify inbound filters; raise the limit only if the count is legitimate."),
    "MAX_PFX": ("BGP maximum-prefix limit reached from a peer.",
                "Verify inbound prefix filters; the peer may be leaking routes."),
    "NOTIFICATION": ("BGP sent/received a NOTIFICATION and reset the session.",
                     "Check for a policy/config mismatch, hold-timer expiry, or an upstream link problem with that peer."),
    "DUP_RTRID": ("Duplicate OSPF router-id detected.",
                  "Assign a unique router-id per device and clear the process during a change window."),
    "CONFLICTING_LSAID": ("OSPF LSA ID conflict (overlapping network/mask).",
                          "Check for overlapping 'network' statements or subnet/mask mismatches between routers."),
    "ADJCHG": ("An OSPF adjacency changed state.",
               "Check the neighbour and the link for flaps, MTU mismatch, or authentication problems."),
    "ADJCHANGE": ("A BGP peer changed state.",
                  "Verify peer reachability, session config, and upstream link stability."),
    "NBRCHANGE": ("An EIGRP neighbour changed state.",
                  "Check the link for flaps, MTU, or authentication mismatch."),
    "NHRP": ("DMVPN/NHRP event (spoke registration or resolution).",
             "Verify hub reachability and NHS registration; check the tunnel and underlying transport."),
    # ---- Crypto / VPN (ISR 4331) ----
    "IKEV2": ("IKEv2 negotiation event on a VPN tunnel.",
              "Check the peer, PSK/certificate, and proposal (encryption/DH/PRF) match; verify reachability."),
    "ISAKMP": ("IKEv1/ISAKMP negotiation event.",
               "Confirm the peer, PSK, and IKE policy match on both ends."),
    "IKMP": ("IKE key-management event.",
             "Verify peer identity, keys and policy; mismatches reset tunnels."),
    "CRYPTO": ("IPSec/crypto event.",
               "Check 'show crypto ikev2 sa' / 'show crypto ipsec sa'; verify peer, keys, and matching transform/policy."),
    # ---- WAN / modules (ISR 4331) ----
    "CONTROLLER": ("A WAN controller (T1/E1/serial) changed state.",
                   "Test the circuit with the carrier; check framing/line-code and cabling."),
    "NIM": ("An ISR network-interface module (NIM) event.",
            "Reseat/verify the module ('show platform'); replace if it does not come online."),
    "CELLULAR": ("Cellular/LTE modem event.",
                 "Check signal strength, SIM status and carrier ('show cellular'); reposition the antenna if weak."),
    "DIALER": ("Dialer/PPP session event.",
               "Check PPP authentication and the underlying circuit/backup path."),
    # ---- Stack (9300 / 3750) ----
    "SWITCH_REMOVED": ("A stack member left the stack.",
                       "Check the member's power and both stack cables; reseat the ring, confirm 'show switch'."),
    "SWITCH_ADDED": ("A stack member joined the stack.",
                     "Confirm the new member's version/priority and a healthy dual-ring in 'show switch stack-ports'."),
    "STACK_LINK": ("A stack-ring link changed state.",
                   "Reseat/replace the stack cable; a half-open ring removes redundancy."),
    "STACKMGR": ("A stack member changed state (join/leave/reload).",
                 "Confirm all members are up ('show switch'); check stack cables and member power."),
    # ---- Platform / hardware / resources (Catalyst 9000 IOS-XE) ----
    "OVERDRAWN": ("PoE power budget exceeded — a port was shut to protect the supply.",
                  "Reduce PoE load or add/upsize a power supply; check 'show power inline' against the available budget."),
    "ILPOWER": ("A PoE (inline power) event on a port.",
                "Check the powered device and 'show power inline'; an AP/phone may have lost power — verify budget and cabling."),
    "THERMAL": ("A temperature sensor crossed a threshold — thermal risk.",
                "Inspect airflow, fans, and ambient/rack temperature now; clear obstructions; replace a failed fan tray."),
    "FAN": ("A fan failed or is degraded.",
            "Replace the fan tray; verify airflow and ambient temperature until resolved."),
    "POWER_SUPPLY": ("A power supply fault or redundancy loss.",
                     "Verify both feeds and PSUs; replace the failed unit to restore N+1 redundancy."),
    "PSU": ("A power supply fault or redundancy loss.",
            "Check 'show environment power'; replace the failed supply."),
    "OIR": ("Online insertion/removal of a module or transceiver.",
            "Confirm the change was intended; reseat the component if the removal was unexpected."),
    "TCAM": ("Hardware TCAM/forwarding resource exhaustion.",
             "Reduce ACL/route/SGT scale or change the SDM template ('show platform hardware fed ... utilization'); over-scale drops programming."),
    "EXHAUST": ("A hardware forwarding resource was exhausted.",
                "Check FED/TCAM utilisation; reduce scale or adjust the SDM template."),
    "FED": ("Forwarding-engine driver (FED) event on a Catalyst 9000.",
            "Collect logs; recurring FED errors usually indicate a software defect — plan an IOS-XE upgrade."),
    "FMAN": ("Forwarding-manager (FMAN) event.",
             "Collect 'show logging' / traces; typically software — evaluate an upgrade."),
    "SISF": ("IP device-tracking (SISF) event.",
             "Check for duplicate/anomalous host tracking; verify device-tracking policy on access ports."),
    "REDUNDANCY": ("Supervisor/stack redundancy state change.",
                   "Verify the standby is 'ready' ('show redundancy'); investigate any switchover reason."),
    # ---- System / process ----
    "MALLOCFAIL": ("The device failed to allocate memory — memory exhaustion.",
                   "Free memory (remove unused features), schedule a maintenance reload, and evaluate a software/memory upgrade."),
    "CPUHOG": ("A process held the CPU too long — control-plane overload.",
               "Identify it with 'show processes cpu sorted'; review ACLs/SNMP/broadcast load; consider a code upgrade."),
    "WATCHDOG": ("A process watchdog fired (possible hang/crash).",
                 "Collect crashinfo; a recurring watchdog usually indicates a bug — plan an upgrade."),
    "HARIKARI": ("A process exceeded its time budget and was restarted.",
                 "Collect traces; likely a software defect — evaluate an upgrade."),
    "MEMORY": ("Low free memory.",
               "Check 'show memory statistics'; reload in a maintenance window if it degrades; consider an upgrade."),
    "CONFIG_I": ("The running configuration was changed from the CLI.",
                 "Per policy, no config changes are authorised via this account — confirm who changed it ('show archive log config all' / AAA) and revert if unauthorised."),
    "SYS-5-CONFIG": ("A configuration change was logged.",
                     "Confirm the change was authorised; investigate if unexpected."),
    "RELOAD": ("The device reloaded.",
               "Check the reload reason in 'show version'; if unexpected, review crashinfo and power/environment."),
    # ---- Time / AAA / management ----
    "NTP": ("NTP synchronisation issue (server unreachable or clock unsynced).",
            "Restore reachability to the NTP source; time drift breaks logs, certificates and Kerberos/AAA."),
    "RADIUS": ("A RADIUS server is unreachable/dead.",
               "Check reachability and the shared secret; management/802.1X auth is at risk while it is down."),
    "TAC_PLUS": ("A TACACS+ server is unreachable.",
                 "Verify reachability and the shared secret; ensure a working fallback so admins are not locked out."),
    "TACACS": ("A TACACS+ AAA event.",
               "Confirm server reachability and shared secret; validate the local fallback."),
    "LOGIN_FAILED": ("Failed login attempt(s).",
                     "If repeated, investigate for brute force; confirm the source and lock down management access."),
    "SNMP": ("SNMP event (often an authentication/community failure).",
             "Verify the community/user; an unexpected source may be an unauthorised poller."),
    # ---- Environment (generic, 2960/3750) ----
    "ENVMON": ("Environmental monitor alarm (temperature/fan/power).",
               "Check 'show environment all' and address the specific failing sensor."),
    "ENVIRONMENT": ("Environmental alarm.",
                    "Check 'show environment all'; remediate the cooling or power fault it reports."),
    "PWR": ("A power event was logged.",
            "Check 'show power' / 'show environment power'; confirm both supplies are online."),
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
    "Default Gateway": ["show ip route 0.0.0.0"],
    "Gateway Redundancy (HSRP/VRRP)": ["show standby brief"],
    "EtherChannel / Trunk Bundles": ["show etherchannel summary"],
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

# Redaction: never persist device credentials/keys pulled from a running-config. Each rule
# keeps the line structure (so config stays readable/diffable) but masks the secret token.
_REDACTORS = [
    re.compile(r"(?i)^(\s*(?:no\s+)?(?:enable\s+)?(?:password|passwd|secret)\s+)(?:\d+\s+)?(\S.*)$"),
    re.compile(r"(?i)^(\s*username\s+\S+\s+(?:privilege\s+\d+\s+)?(?:password|secret)\s+)(?:\d+\s+)?(\S.*)$"),
    re.compile(r"(?i)^(\s*snmp-server\s+community\s+)(\S+)(.*)$"),
    re.compile(r"(?i)^(\s*snmp-server\s+(?:user|host)\s+.*?(?:auth|priv|md5|sha|des|aes|community)\s+)(\S+)(.*)$"),
    re.compile(r"(?i)^(\s*(?:pre-shared-key|key-string|key-hash|wpa-psk\s+\w+)\s+)(?:\d+\s+)?(\S.*)$"),
    re.compile(r"(?i)^(\s*crypto\s+isakmp\s+key\s+)(\S+)(.*)$"),
    re.compile(r"(?i)^(\s*(?:ppp\s+(?:chap|pap)\s+(?:password|secret)|neighbor\s+\S+\s+password)\s+)(?:\d+\s+)?(\S.*)$"),
    re.compile(r"(?i)^(\s*(?:radius|tacacs).*\bkey\s+)(?:\d+\s+)?(\S.*)$"),
]


def _redact(text: str) -> str:
    """Mask secrets in a device configuration/output before it is stored or returned."""
    if not text:
        return text
    lines = []
    for line in str(text).splitlines():
        for rx in _REDACTORS:
            m = rx.match(line)
            if m:
                tail = m.group(3) if rx.groups >= 3 else ""
                line = m.group(1) + "<redacted>" + (tail or "")
                break
        lines.append(line)
    return "\n".join(lines)


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

    # Default gateway — the default-route candidates ranked by administrative distance then metric.
    # Feeds branch failover/priority analysis (which uplink/circuit is primary/secondary/tertiary).
    default_rows = []
    for r in _rows(parsed.get("routes")):
        if str(r.get("network", "")).startswith("0.0.0.0"):
            ad = r.get("distance"); metric = r.get("metric")
            default_rows.append({"Protocol": r.get("protocol"), "Prefix": "0.0.0.0/0",
                                 "Next Hop": r.get("nexthop_ip") or r.get("nexthop_if"),
                                 "Distance": ad, "Metric": metric,
                                 "Distance/Metric": f"{ad if ad is not None else ''}/{metric if metric is not None else ''}"})
    # ordered candidates (lowest AD, then metric = most preferred = primary)
    def _rank(v):
        try: return float(v)
        except (TypeError, ValueError): return float("inf")
    default_rows.sort(key=lambda x: (_rank(x["Distance"]), _rank(x["Metric"])))
    for i, row in enumerate(default_rows, 1):
        row["Priority"] = i
    if default_rows:
        tables["Default Gateway"] = default_rows

    # Gateway redundancy — HSRP/VRRP groups (show standby brief): which device holds the virtual IP.
    fhrp = [{"Interface": r.get("interface"), "Group": r.get("group"), "Priority": r.get("priority"),
             "Preempt": r.get("preempt"), "State": r.get("state"), "Active": r.get("active"),
             "Standby": r.get("standby"), "Virtual IP": r.get("virtual_ip_address")}
            for r in _rows(parsed.get("standby"))]
    if fhrp:
        tables["Gateway Redundancy"] = fhrp

    # EtherChannel — port-channel bundles (trunk redundancy across physical members).
    etherchannel = [{"Group": r.get("group"), "Port-channel": r.get("bundle_name"),
                     "Status": (r.get("bundle_status") or "").strip() or None, "Protocol": r.get("bundle_protocol"),
                     "Members": _joined(r.get("member_interface"))}
                    for r in _rows(parsed.get("etherchannel"))]
    if etherchannel:
        tables["EtherChannel"] = etherchannel

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


def _reported_hostname(raw: dict) -> str | None:
    """The device's OWN idea of its hostname, from its running-config 'hostname' line or the
    'show version' uptime banner ('<hostname> uptime is ...') — whichever is present. This is
    ground truth for device identity: unlike LogicMonitor's management-IP-to-name mapping
    (which can go stale if a device is re-IP'd, swapped, or LM's inventory is simply wrong),
    the device itself cannot lie about its own configured hostname."""
    cfg = raw.get("config") or ""
    m = re.search(r"^hostname (\S+)", cfg, re.MULTILINE)
    if m:
        return m.group(1)
    ver = raw.get("version") or ""
    m = re.search(r"^(\S+) uptime is", ver, re.MULTILINE)
    return m.group(1) if m else None


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
    config = _redact(config)  # never persist device passwords / SNMP communities / keys
    if raw.get("config"):
        raw = {**raw, "config": _redact(raw["config"])}
    return {
        "status": "ok",
        "data": {"tables": tables, "config": config, "raw": raw, "commands": COMMANDS,
                 "recognised": sorted(k for k in blocks if not _unsupported(blocks[k])),
                 "reported_hostname": _reported_hostname(raw)},
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
