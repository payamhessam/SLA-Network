"""CCIE knowledge base — maps a resilience finding to a plain-English recommendation and the
exact verify/config commands to run in the admin's own MFA-authenticated SSH session.

Content is written in our own words with standard Cisco IOS CLI, informed by (and cross-
referenced to) the internal Cisco CLI Master Cheatsheet / Network Engineering Guide sections.
We deliberately do not reproduce the book's prose. No command here changes state on its own;
these are copy-ready blocks for a human to review and apply during a change window.
"""
from __future__ import annotations

KB: dict[str, dict] = {
    "single_northbound": {
        "id": "single_northbound",
        "severity": "critical",
        "title": "Single northbound Layer-3 path — add a tracked floating backup",
        "why": ("The branch reaches everything through one default route with no floating backup and no "
                "first-hop redundancy. If that upstream path or next-hop fails, the whole site is isolated "
                "until routing is manually restored."),
        "recommendation": ("Add a second upstream path (a backup circuit or a link to a second core/WAN "
                           "device) and install a floating static default route that is tracked by IP SLA, "
                           "so it only takes over when the primary path actually fails."),
        "commands": [
            "! 1) Probe the PRIMARY next-hop so we know when it is really down",
            "ip sla 1",
            " icmp-echo <primary-next-hop-ip> source-interface <primary-uplink>",
            " frequency 5",
            "ip sla schedule 1 life forever start-time now",
            "track 1 ip sla 1 reachability",
            "! 2) Floating static default via the BACKUP next-hop (AD 250 > the OSPF/primary default),",
            "!    installed only while track 1 is up",
            "ip route 0.0.0.0 0.0.0.0 <backup-next-hop-ip> 250 track 1",
            "! 3) Verify",
            "show ip sla statistics",
            "show track 1",
            "show ip route 0.0.0.0",
        ],
        "reference": "Cisco CLI Master Cheatsheet §15.1–15.2 (Static Routing / Static Route Tracking)",
    },
    "static_etherchannel": {
        "id": "static_etherchannel",
        "severity": "warning",
        "title": "EtherChannel is static (mode on) — move to LACP",
        "why": ("Port-channels configured with 'mode on' do not negotiate. A miscable or a one-sided change "
                "can silently black-hole traffic, or bridge a loop that spanning-tree only partially catches, "
                "because neither end validates the bundle."),
        "recommendation": "Run the bundles as LACP (mode active on both ends) so mismatches are detected and suspended safely.",
        "commands": [
            "interface range <member-interfaces>",
            " channel-group <n> mode active",
            "! Verify negotiation and members",
            "show etherchannel summary",
            "show lacp neighbor",
        ],
        "reference": "Cisco CLI Master Cheatsheet §13 (EtherChannel & LACP)",
    },
    "single_distribution": {
        "id": "single_distribution",
        "severity": "critical",
        "title": "Single (non-stacked) distribution — no supervisor redundancy",
        "why": ("The distribution layer is one switch with no stack peer. Its failure drops every access "
                "switch that homes to it — a site-wide outage from one box."),
        "recommendation": ("Add a stack member (or a second distribution switch with cross-stack EtherChannels "
                           "to each access switch) so the access layer survives a distribution failure."),
        "commands": [
            "! Confirm current stack / redundancy state",
            "show switch",
            "show switch stack-ports",
            "show redundancy",
        ],
        "reference": "Cisco CLI Master Cheatsheet §13 (EtherChannel & LACP) · §2.4 (Hardware Inventory)",
    },
    "backup_circuit_idle": {
        "id": "backup_circuit_idle",
        "severity": "critical",
        "title": "Backup WAN circuit present but not used by routing — wire in the failover",
        "why": ("A second provider circuit is mapped to this branch, but the DSW carries a single default "
                "route (one next-hop, no floating backup, no FHRP). The backup path exists physically but "
                "the switch will not fail over to it on its own — the redundancy is not actually usable at L3."),
        "recommendation": ("Install a floating static default via the backup circuit's next-hop, tracked by IP "
                           "SLA against the primary, so the branch fails over to the second carrier automatically. "
                           "Confirm the upstream/WAN edge actually re-advertises during a primary outage."),
        "commands": [
            "! Track the PRIMARY circuit next-hop",
            "ip sla 1",
            " icmp-echo <primary-next-hop-ip> source-interface <primary-uplink>",
            " frequency 5",
            "ip sla schedule 1 life forever start-time now",
            "track 1 ip sla 1 reachability",
            "! Floating default via the BACKUP circuit (installed only while primary is down)",
            "ip route 0.0.0.0 0.0.0.0 <backup-circuit-next-hop> 250 track 1",
            "! Verify failover behaviour",
            "show track 1",
            "show ip route 0.0.0.0",
            "show ip sla statistics",
        ],
        "reference": "Cisco CLI Master Cheatsheet §15.1–15.2 (Static Routing / Static Route Tracking)",
    },
    "wan_single_circuit": {
        "id": "wan_single_circuit",
        "severity": "warning",
        "title": "Single WAN circuit — no carrier diversity",
        "why": ("Only one provider circuit is mapped to this branch. A carrier outage, a cut, or provider "
                "maintenance takes the whole site offline with no alternate path."),
        "recommendation": ("Add a second circuit from a diverse carrier (ideally different media — e.g. fibre + "
                           "LTE/DMVPN) and wire it as a tracked floating backup, or via the WAN edge with dynamic "
                           "routing, so the branch survives a single-carrier failure."),
        "commands": [
            "! After the second circuit is delivered, verify both are up at the WAN edge",
            "show ip interface brief",
            "show ip route 0.0.0.0",
            "! Then add IP-SLA-tracked floating backup as in 'single northbound' remediation",
        ],
        "reference": "Cisco CLI Master Cheatsheet §15.2 (Static Route Tracking) · §21.4 (DMVPN)",
    },
    "lldp_disabled": {
        "id": "lldp_disabled",
        "severity": "info",
        "title": "LLDP disabled — enable the vendor-neutral neighbor protocol",
        "why": ("No LLDP neighbours were seen in the collected topology, only CDP. This usually means LLDP "
                "is disabled on this device, so non-Cisco neighbours (WAN CE routers, servers, some APs) are "
                "not discovered and topology mapping is incomplete. It can also mean every attached neighbour "
                "simply has LLDP off — confirm with 'show lldp' before assuming this device is the cause."),
        "recommendation": "Enable LLDP globally in addition to CDP.",
        "commands": [
            "! Confirm LLDP is actually disabled locally before changing anything",
            "show lldp",
            "lldp run",
            "show lldp neighbors detail",
        ],
        "reference": "Cisco CLI Master Cheatsheet §10.3 (CDP/LLDP)",
    },
    "single_access_uplink": {
        "id": "single_access_uplink",
        "severity": "critical",
        "title": "Access switch(es) not dual-homed — single uplink to distribution",
        "why": ("No access switch at this branch is dual-homed to two distribution ports and no EtherChannel "
                "bundle is up. Every attached access switch depends on one physical link, port, and SFP — "
                "losing any one of those isolates that switch and everything plugged into it."),
        "recommendation": ("Home each access switch to two distribution uplinks — either bundled (EtherChannel/"
                           "LACP to the same distribution device or stack) or dual-homed to two separate "
                           "distribution switches — so a single link/port failure doesn't isolate the switch."),
        "commands": [
            "show cdp neighbors detail",
            "show lldp neighbors detail",
            "show etherchannel summary",
            "show interfaces status",
        ],
        "reference": "Cisco CLI Master Cheatsheet §13 (EtherChannel & LACP) · §10.3 (CDP/LLDP)",
    },
    "etherchannel_down": {
        "id": "etherchannel_down",
        "severity": "warning",
        "title": "EtherChannel bundle configured but not passing traffic",
        "why": ("At least one port-channel is configured but is currently down or suspended, not actively "
                "in use (a member reporting anything other than in-use/'SU'/'RU'). Redundancy or capacity "
                "you're counting on from this bundle may not actually be there right now — commonly caused "
                "by a one-sided LACP mode mismatch or an allowed-VLAN/trunk mismatch between the two ends."),
        "recommendation": ("Confirm every configured bundle shows in-use (SU/RU); investigate and correct any "
                           "bundle reporting down/suspended before relying on it for capacity or failover."),
        "commands": [
            "show etherchannel summary",
            "show lacp neighbor detail",
            "show interfaces status",
        ],
        "reference": "Cisco CLI Master Cheatsheet §13 (EtherChannel & LACP)",
    },
    "ospf_stuck_adjacency": {
        "id": "ospf_stuck_adjacency",
        "severity": "critical",
        "title": "OSPF neighbor stuck below FULL — likely MTU mismatch",
        "why": ("An OSPF neighbor is parked in a state other than FULL/2-WAY. A neighbor stuck in EXSTART or "
                "EXCHANGE is the classic signature of an MTU mismatch on the link — DBD packets larger than "
                "the smaller side's MTU get silently dropped, so the link looks up but the link-state database "
                "never finishes syncing and that neighbor's routes never install. INIT/ATTEMPT usually means "
                "one-way reachability or an authentication/network-type mismatch."),
        "recommendation": ("Compare interface MTU on both ends. If the mismatch is intentional, apply "
                           "'ip ospf mtu-ignore' as a documented exception; otherwise correct the smaller side "
                           "and clear the adjacency to confirm it reaches FULL."),
        "commands": [
            "show ip ospf neighbor",
            "show ip ospf interface <interface>",
            "show interfaces <interface> | include MTU",
        ],
        "reference": "Cisco CLI Master Cheatsheet §16 (OSPF)",
    },
    "hsrp_no_preempt": {
        "id": "hsrp_no_preempt",
        "severity": "warning",
        "title": "HSRP/VRRP group without preempt configured",
        "why": ("At least one FHRP group has no preempt configured. When the intended-primary chassis "
                "recovers from a reload it stays Standby, so traffic keeps riding what was meant to be the "
                "backup path with no alert that you're on the 'wrong' gateway — a second failure on that box "
                "then causes a real outage."),
        "recommendation": ("Enable 'standby <group> preempt' with a delay long enough to cover full "
                           "reconvergence, paired with interface tracking so priority drops when the box's "
                           "own uplink is down."),
        "commands": [
            "show standby brief",
            "show standby <interface> <group>",
            "! interface <svi>",
            "!  standby <group> preempt delay minimum 180",
        ],
        "reference": "Cisco CLI Master Cheatsheet §14 (FHRP / HSRP-VRRP)",
    },
    "unsurfaced_critical_alert": {
        "id": "unsurfaced_critical_alert",
        "severity": "critical",
        "title": "Device is logging P1 alerts not reflected in this branch's posture",
        "why": ("A device at this branch has an unresolved P1 syslog finding (e.g. a stack member dropping "
                "out, a power supply fault). The redundancy posture shown above reflects the LAST collected "
                "state and will not automatically downgrade itself when a P1 event happens after that — a "
                "branch can still read 'Fully redundant' while the stack that redundancy depends on is "
                "actively degraded."),
        "recommendation": ("Investigate and clear the outstanding P1 finding for this branch's devices, then "
                           "re-run Fill gaps / Pull via SSH to refresh the posture."),
        "commands": [
            "show logging",
            "show switch",
            "show switch stack-ports",
        ],
        "reference": "Cisco CLI Master Cheatsheet §2.4 (Hardware Inventory) · §17 (Syslog & Troubleshooting)",
    },
}


def for_branch(failover: dict, tables: dict, include_commands: bool = True) -> list[dict]:
    """Return the KB entries that apply to this branch, based on its collected posture.

    `include_commands=False` strips the device CLI (and its reference) from every entry,
    leaving the plain-English title/why/recommendation. Read-only viewers get the business
    finding — "this branch has no automatic failover and here is what should change" —
    while the copy-ready configuration commands stay with administrators, who are the only
    role that can act on them. Enforced here in the API rather than hidden in the UI, so
    the commands are genuinely absent from a non-admin response.
    """
    from .pathres import _ETHER_UP  # local import: avoid a module import cycle at load time

    out: list[dict] = []
    wan = failover.get("wan_circuits", 0)
    # A single WAN/L3 path is a single WAN/L3 path regardless of FHRP: HSRP/VRRP only protects
    # the LAN-side gateway between two local chassis, it does not give the branch a second
    # northbound route. See _failover()'s own north_redundant computation for the same reasoning.
    single_l3 = failover.get("has_routing_evidence") and failover.get("northbound_paths", 0) <= 1
    if single_l3 and wan >= 2:
        out.append(KB["backup_circuit_idle"])   # backup circuit exists but routing doesn't use it
    elif single_l3:
        out.append(KB["single_northbound"])     # truly one path
    if wan == 1:
        out.append(KB["wan_single_circuit"])
    ether = tables.get("EtherChannel", []) or []
    if any(str(r.get("Protocol") or "-").strip() in ("-", "") and str(r.get("Status", "")).startswith(_ETHER_UP) for r in ether):
        out.append(KB["static_etherchannel"])
    ether_down = [r for r in ether if str(r.get("Status", "")).strip() and not str(r.get("Status", "")).startswith(_ETHER_UP)]
    if ether_down:
        out.append(KB["etherchannel_down"])
    if not failover.get("distribution_redundant"):
        out.append(KB["single_distribution"])
    if not failover.get("access_redundant"):
        out.append(KB["single_access_uplink"])
    # CDP present but no LLDP rows at all => LLDP likely disabled
    neighbors = tables.get("CDP-LLDP Neighbors", []) or []
    if neighbors and not any(str(r.get("Protocol")) == "LLDP" for r in neighbors):
        out.append(KB["lldp_disabled"])
    ospf = tables.get("OSPF Neighbors", []) or []
    # Normalize both sources: LM gives lowercase "full"/"2-way"; real IOS/SSH output gives
    # "FULL/BDR", "2WAY/DROTHER" (role suffix after '/'). Strip hyphens and any '/' suffix
    # before comparing so a DR/BDR-qualified FULL state isn't mistaken for a stuck adjacency.
    def _ospf_ok(state):
        s = str(state or "").upper().replace("-", "").split("/")[0]
        return s in ("", "FULL", "2WAY")
    if any(not _ospf_ok(r.get("State")) for r in ospf):
        out.append(KB["ospf_stuck_adjacency"])
    fhrp = tables.get("Gateway Redundancy", []) or []
    if any(str(r.get("Preempt") or "").strip().lower() not in ("p", "yes", "enabled", "true") for r in fhrp):
        out.append(KB["hsrp_no_preempt"])
    recs = tables.get("Recommendations (SSH)", []) or []
    if any(str(r.get("Priority") or "").upper() == "P1" for r in recs):
        out.append(KB["unsurfaced_critical_alert"])
    if not include_commands:
        # Strip the CLI for non-administrators. Copy (don't mutate) so the module-level KB
        # dict is never modified for subsequent admin requests.
        out = [{k: v for k, v in entry.items() if k not in ("commands", "reference")} for entry in out]
    return out
