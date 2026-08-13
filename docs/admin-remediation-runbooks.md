# Administrator remediation runbooks

## Purpose and boundaries

These runbooks turn collected device evidence into an investigation sequence for a qualified
network engineer. They are not automation, nor a substitute for a peer-reviewed maintenance
plan. The application never sends a configuration command to a network device. A runbook must
always begin with read-only verification, identify the exact affected interface or neighbour,
and finish by proving that the original symptom has stopped.

## Evidence hierarchy

1. Prefer the device's read-only SSH output over a stale automated discovery record.
2. Correlate a syslog event with interface, neighbour, environmental and routing evidence before
   changing configuration.
3. Do not clear counters before capturing a baseline. Clearing a counter destroys the evidence
   required to show whether a fault is still incrementing.
4. Do not re-enable an errdisabled port until its trigger is known and corrected.
5. Do not treat HSRP/VRRP as a second WAN path. It protects a local default gateway only.
6. Do not claim Tier III/IV resilience from two CDP/LLDP neighbour names; independent failure
   domains and failover must be verified separately.

## Alert-to-procedure map

| Evidence | First checks | Safe decision rule | Proof after change |
|---|---|---|---|
| BPDU Guard / errdisable | `show interfaces status err-disabled`, `show spanning-tree interface <interface> detail`, `show logging` | A PortFast edge port must not receive BPDUs. Remove the bridge/switch or redesign it as a network link before recovery. | The port is no longer errdisabled and no new BPDU Guard log appears. |
| UDLD / Loop Guard | `show udld interface <interface>`, `show interfaces <interface>`, `show spanning-tree inconsistentports` | Treat as possible unidirectional fibre or optic failure; inspect both strands/optics before enabling a port. | UDLD is bidirectional; STP is forwarding as designed; logs stop. |
| LACP suspension / cannot bundle | `show etherchannel summary`, `show lacp neighbor detail`, `show interfaces trunk` | Compare every member at both ends: channel mode, speed, duplex, trunk mode, native VLAN and allowed VLANs. Change only in an approved window. | Member state is bundled/in-use and Port-channel is `SU`/`RU`. |
| CRC/FCS/alignment | `show interfaces <interface>`, `show interfaces counters errors`, `show interfaces transceiver detail` | Preserve the counter baseline. Check optic, fibre/copper, patch panel, peer NIC and duplex/MTU before replacing hardware. | Error counters stop increasing over an agreed observation period. |
| Native VLAN/PVID mismatch | `show interfaces trunk`, `show interfaces <interface> switchport`, `show spanning-tree interface <interface> detail` | Compare both trunk ends. Align the intended native/allowed VLAN policy; never infer membership from a description. | Trunk state and STP are consistent; mismatch messages stop. |
| MAC flap / storm control | `show mac address-table`, `show storm-control interface <interface>`, `show spanning-tree summary` | Locate every observed port before changing thresholds. Eliminate loops or unintended dual homes first. | MAC moves/storm events stop without masking the cause. |
| HSRP/VRRP transition | `show standby brief` or `show vrrp brief`, `show track`, `show ip route 0.0.0.0` | Fix tracked uplink, priority or preemption behaviour; use an appropriate reload/preempt delay so a rebooted device does not take traffic before routing converges. | Active/standby state is stable and the intended tracked failure changes priority. |
| DHCP snooping / DAI / IP Source Guard | `show ip dhcp snooping binding`, `show ip arp inspection`, `show logging` | Confirm DHCP binding and trust boundaries before trusting a port or changing a security policy. An unexpected source is a security event. | Expected client traffic passes; invalid packets are still blocked. |
| 802.1X / MAB / port security | `show authentication sessions interface <interface> details`, `show port-security interface <interface>`, `show radius statistics` | Identify endpoint ownership and policy first. Adjust identity/policy only when the endpoint is legitimate. | Authentication is authorized and no new violation is logged. |
| OSPF below FULL | `show ip ospf neighbor`, `show ip ospf interface <interface>`, `show interfaces <interface> | include MTU` | EXSTART/EXCHANGE warrants MTU/authentication/network-type comparison on both ends. `ip ospf mtu-ignore` is an explicit exception, not a default fix. | The adjacency reaches FULL (or valid 2-WAY on a multiaccess segment) and routes install. |

## Change-template library

These original templates are deliberately parameterised. They are planning aids for an approved
maintenance record, not runnable automation. Replace every angle-bracket value only after
collecting the device's model, release, current configuration, peer configuration, impact,
rollback command and validation owner. Capture `show` output before and after; never paste a
template into a live device without peer review.

### Host-facing BPDU Guard

Use only when the evidence proves an access port connects to an endpoint, not another switch,
hypervisor bridge, phone switch, or trunk.

```ios
interface <edge-interface>
 switchport mode access
 switchport access vlan <approved-access-vlan>
 spanning-tree portfast
 spanning-tree bpduguard enable
```

Verify with `show spanning-tree interface <edge-interface> detail` and `show logging`. If the
port received a BPDU, remove or redesign the attached bridge before recovering it. The
configuration pattern and PortFast scope are documented by [Cisco](https://www.cisco.com/c/en/us/support/docs/lan-switching/spanning-tree-protocol/10586-65.html) and illustrated by [Firewall.cx](https://www.firewall.cx/cisco/cisco-switches/spanning-tree-protocol-bpdu-guard-deployment-configuration.html).

### LACP EtherChannel mismatch

First compare both ends' speed, duplex, mode, native VLAN and allowed VLANs. Build the logical
port and policy first; join one approved physical member at a time so rollback is explicit.

```ios
interface port-channel <id>
 switchport mode trunk
 switchport trunk native vlan <approved-native-vlan>
 switchport trunk allowed vlan <approved-vlan-list>
!
interface <member-interface>
 channel-group <id> mode active
```

Validate `show etherchannel summary`, `show lacp neighbor detail`, and `show interfaces
port-channel <id>`. Cisco notes that LACP members must be compatible in settings such as speed,
duplex, native VLAN and trunking; its [current EtherChannel guide](https://www.cisco.com/c/en/us/td/docs/switches/lan/c9000/lyr2-fwd/etherchannel/etherchannel-configuration-guide/etherchannels.html) is authoritative. [Firewall.cx's example](https://www.firewall.cx/operating-systems/microsoft/windows-servers/windows-server-nic-teaming-load-balancing-failover-lacp.html) is a useful topology illustration, but not a substitute for the platform guide.

### Tracked primary route with a floating backup

This is appropriate only after proving distinct circuits and routing domains. Probe an address
beyond the primary next hop. The tracked route is the primary; the backup has a higher
administrative distance. Do not put a positive-reachability track on the backup route if the
intent is to use that backup when the primary fails.

```ios
ip sla <operation>
 icmp-echo <stable-probe-target> source-interface <primary-wan-interface>
 frequency <seconds>
ip sla schedule <operation> life forever start-time now
track <track-id> ip sla <operation> reachability
 delay up <seconds> down <seconds>
ip route 0.0.0.0 0.0.0.0 <primary-next-hop> track <track-id>
ip route 0.0.0.0 0.0.0.0 <backup-next-hop> <higher-administrative-distance>
```

Prove normal and controlled-failure behaviour with `show ip sla statistics`, `show track
<track-id>`, and `show ip route 0.0.0.0`. Cisco's [object tracking guide](https://www.cisco.com/c/en/us/td/docs/routers/ios/config/17-x/ip-addressing/b-ip-addressing/m_iap-eot-xe.html) documents reachability tracking and delays; [Firewall.cx's IP SLA walkthrough](https://www.firewall.cx/cisco/cisco-routers/cisco-router-pbr-ipsla-auto-redirect.html) provides a readable scenario example.

### HSRP restoration after an uplink or tracking fault

Use a preempt delay only after confirming that routing and upstream reachability converge
before the restored device can become active. HSRP gives first-hop gateway redundancy; it is
not evidence of a second WAN path.

```ios
interface <gateway-svi>
 standby <group> priority <approved-priority>
 standby <group> preempt delay minimum <seconds>
 standby <group> track <uplink-or-track-id> decrement <approved-decrement>
```

Verify stable state, priority and tracking with `show standby brief`, `show standby
<interface> <group>`, `show track`, and `show ip route 0.0.0.0`. Confirm supported syntax in the
[Cisco HSRP guide](https://www.cisco.com/c/en/us/td/docs/routers/ios-xe/network-services/network-services/m_fhp-hsrp-0.html) for the installed release.

## Cisco primary references used

- [EtherChannel Configuration Guide, Cisco IOS XE 17](https://www.cisco.com/c/en/us/td/docs/switches/lan/c9000/lyr2-fwd/etherchannel/etherchannel-configuration-guide/etherchannels.html)
- [PortFast and BPDU Guard, Cisco](https://www.cisco.com/c/en/us/support/docs/lan-switching/spanning-tree-protocol/10586-65.html)
- [Errdisable recovery, Cisco IOS](https://www.cisco.com/c/en/us/support/docs/lan-switching/spanning-tree-protocol/69980-errdisable-recovery.html)
- [HSRP, Cisco IOS XE](https://www.cisco.com/c/en/us/td/docs/routers/ios-xe/network-services/network-services/m_fhp-hsrp-0.html)
- [Enhanced Object Tracking, Cisco IOS XE](https://www.cisco.com/c/en/us/td/docs/routers/ios/config/17-x/ip-addressing/b-ip-addressing/m_iap-eot-xe.html)
- [Dynamic ARP Inspection, Cisco IOS XE 17](https://www.cisco.com/c/en/us/td/docs/switches/lan/c9000/sec-crypto/fhs-sisf/fhs-and-sisf-configuration-guide/dynamic-arp-inspection.html)
- [UDLD, Cisco IOS XE 17](https://www.cisco.com/c/en/us/td/docs/switches/lan/c9000/lyr2-fwd/cdp-lldp-mac-udld/cdp-lldp-mac-udld-configuration-guide/c-configure-udld.html)
- [BPDU Guard and errdisable example, Firewall.cx](https://www.firewall.cx/cisco/cisco-switches/spanning-tree-protocol-bpdu-guard-deployment-configuration.html)
- [IP SLA tracking scenario, Firewall.cx](https://www.firewall.cx/cisco/cisco-routers/cisco-router-pbr-ipsla-auto-redirect.html)
- [LACP EtherChannel scenario, Firewall.cx](https://www.firewall.cx/operating-systems/microsoft/windows-servers/windows-server-nic-teaming-load-balancing-failover-lacp.html)

The Cisco documents are the authoritative command references. The runbooks are original,
environment-specific operational summaries; command availability must be checked against the
actual platform and IOS/IOS XE release before a change.
