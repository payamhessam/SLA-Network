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
        "title": "EtherChannel is static (mode on) — move to LACP",
        "why": ("Port-channels configured with 'mode on' do not negotiate. A miscable or a one-sided change "
                "can silently black-hole traffic because neither end validates the bundle."),
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
        "title": "LLDP disabled — enable the vendor-neutral neighbor protocol",
        "why": ("Only CDP is running, so non-Cisco neighbours (WAN CE routers, servers, some APs) are not "
                "discovered and topology mapping is incomplete."),
        "recommendation": "Enable LLDP globally in addition to CDP.",
        "commands": [
            "lldp run",
            "show lldp neighbors detail",
        ],
        "reference": "Cisco CLI Master Cheatsheet §10.3 (CDP/LLDP)",
    },
}


def for_branch(failover: dict, tables: dict) -> list[dict]:
    """Return the KB entries that apply to this branch, based on its collected posture."""
    out: list[dict] = []
    wan = failover.get("wan_circuits", 0)
    single_l3 = (failover.get("has_routing_evidence") and failover.get("northbound_paths", 0) <= 1
                 and failover.get("fhrp_groups", 0) == 0)
    if single_l3 and wan >= 2:
        out.append(KB["backup_circuit_idle"])   # backup circuit exists but routing doesn't use it
    elif single_l3:
        out.append(KB["single_northbound"])     # truly one path
    if wan == 1:
        out.append(KB["wan_single_circuit"])
    ether = tables.get("EtherChannel", []) or []
    if any(str(r.get("Protocol") or "-").strip() in ("-", "") and str(r.get("Status", "")).startswith("SU") for r in ether):
        out.append(KB["static_etherchannel"])
    if not failover.get("distribution_redundant"):
        out.append(KB["single_distribution"])
    # CDP present but no LLDP rows at all => LLDP likely disabled
    neighbors = tables.get("CDP-LLDP Neighbors", []) or []
    if neighbors and not any(str(r.get("Protocol")) == "LLDP" for r in neighbors):
        out.append(KB["lldp_disabled"])
    return out
