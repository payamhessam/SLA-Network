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

## Cisco primary references used

- [EtherChannel Configuration Guide, Cisco IOS XE 17](https://www.cisco.com/c/en/us/td/docs/switches/lan/c9000/lyr2-fwd/etherchannel/etherchannel-configuration-guide/etherchannels.html)
- [PortFast and BPDU Guard, Cisco](https://www.cisco.com/c/en/us/support/docs/lan-switching/spanning-tree-protocol/10586-65.html)
- [Errdisable recovery, Cisco IOS](https://www.cisco.com/c/en/us/support/docs/lan-switching/spanning-tree-protocol/69980-errdisable-recovery.html)
- [HSRP, Cisco IOS XE](https://www.cisco.com/c/en/us/td/docs/routers/ios-xe/network-services/network-services/m_fhp-hsrp-0.html)
- [Enhanced Object Tracking, Cisco IOS XE](https://www.cisco.com/c/en/us/td/docs/routers/ios/config/17-x/ip-addressing/b-ip-addressing/m_iap-eot-xe.html)
- [Dynamic ARP Inspection, Cisco IOS XE 17](https://www.cisco.com/c/en/us/td/docs/switches/lan/c9000/sec-crypto/fhs-sisf/fhs-and-sisf-configuration-guide/dynamic-arp-inspection.html)
- [UDLD, Cisco IOS XE 17](https://www.cisco.com/c/en/us/td/docs/switches/lan/c9000/lyr2-fwd/cdp-lldp-mac-udld/cdp-lldp-mac-udld-configuration-guide/c-configure-udld.html)

The Cisco documents are the authoritative command references. The runbooks are original,
environment-specific operational summaries; command availability must be checked against the
actual platform and IOS/IOS XE release before a change.
