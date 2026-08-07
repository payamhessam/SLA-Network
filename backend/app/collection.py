import asyncio
import math
import re
import time
from datetime import datetime, timezone

from .logicmonitor import LogicMonitorClient


def latest(data):
    points, values = data.get("dataPoints", []), data.get("values", [])
    return dict(zip(points, values[-1])) if values else {}


def numeric(value):
    """Normalize LogicMonitor numeric datapoints without treating missing values as zero."""
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def percentile(values, fraction):
    valid = sorted(value for item in values if (value := numeric(item)) is not None)
    if not valid: return None
    return valid[min(len(valid) - 1, math.ceil(len(valid) * fraction) - 1)]


def state(value):
    return {1: "up", 2: "down", 3: "testing"}.get(value, "unknown")


OSPF_STATE = {1: "down", 2: "attempt", 3: "init", 4: "2-way", 5: "exstart", 6: "exchange", 7: "loading", 8: "full"}


async def collect_logicmonitor_device(client: LogicMonitorClient, local, remote):
    device_id = int(remote["id"]); end = int(time.time()); start = end - 3600
    props, applied = await asyncio.gather(client.properties(device_id), client.applied_datasources(device_id))
    by_name = {(x.get("dataSourceName") or x.get("name")): x for x in applied}
    sem = asyncio.Semaphore(8)

    async def source(name, hours=1):
        ds = by_name.get(name)
        if not ds: return [], []
        hds = int(ds["id"]); instances = await client.instances(device_id, hds)
        async def one(instance):
            async with sem:
                try:
                    data = await client.instance_data(device_id, hds, int(instance["id"]), end - hours * 3600, end)
                    return instance, data
                except Exception:
                    return instance, {}
        return instances, await asyncio.gather(*(one(x) for x in instances))

    names = ["Ping", "Cisco_CPU_SNMP", "Cisco_MemoryPools_SNMP", "Cisco_Entity_Sensors", "SNMP_Network_Interfaces", "CiscoFan-", "Cisco_System_PowerSupplies", "Cisco Switch Stack-", "Cisco Switch Stack Ports-", "SNMP_Host_Uptime", "Device_Component_Inventory", "LLDP_Neighbors", "CDP_Neighbors", "LogicMonitor_ConfigSource_Metrics", "Cisco_NTP", "OSPF_Neighbors"]
    results = dict(zip(names, await asyncio.gather(*(source(x, 24 if x == "Ping" else 1) for x in names))))

    ping_pairs = results["Ping"][1]; ping_data = ping_pairs[0][1] if ping_pairs else {}; ping_now = latest(ping_data)
    pidx = {name: i for i, name in enumerate(ping_data.get("dataPoints", []))}
    rows = ping_data.get("values", [])
    losses = [value for r in rows if "PingLossPercent" in pidx and (value := numeric(r[pidx["PingLossPercent"]])) is not None]
    avgs = [value for r in rows if "average" in pidx and (value := numeric(r[pidx["average"]])) is not None]
    availability = 100 - (sum(losses) / len(losses)) if losses else None

    cpu_values = [latest(data) for _, data in results["Cisco_CPU_SNMP"][1]]
    cpu = max((x.get("CPU1min") for x in cpu_values if isinstance(x.get("CPU1min"), (int, float))), default=None)
    mem_values = [latest(data) for _, data in results["Cisco_MemoryPools_SNMP"][1]]
    free = min((x.get("PercentFree") for x in mem_values if isinstance(x.get("PercentFree"), (int, float))), default=None)
    memory = 100 - free if free is not None else None

    sensor_rows=[]
    for inst,data in results["Cisco_Entity_Sensors"][1]:
        value=latest(data); sensor_rows.append({"Category":"Sensor","Switch/Module":"Cisco","Component/Interface":inst.get("displayName") or inst.get("name"),"State":"Normal" if value.get("sensor_status")==1 else "Unknown","Temperature C":value.get("sensor_value"),"Fan RPM":None,"Watts":None,"Details":inst.get("description")})
    temperatures=[x["Temperature C"] for x in sensor_rows if isinstance(x["Temperature C"],(int,float))]
    for inst,data in results["CiscoFan-"][1]:
        value=latest(data); sensor_rows.append({"Category":"Fan","Switch/Module":"Cisco","Component/Interface":inst.get("displayName") or inst.get("name"),"State":"Normal" if value.get("Status")==1 else "Fault","Temperature C":None,"Fan RPM":None,"Watts":None,"Details":inst.get("description")})
    for inst,data in results["Cisco_System_PowerSupplies"][1]:
        value=latest(data); sensor_rows.append({"Category":"Power Supply","Switch/Module":"Cisco","Component/Interface":inst.get("displayName") or inst.get("name"),"State":"Normal" if value.get("Status")==1 and not value.get("ErrorAlert") else "Fault","Temperature C":None,"Fan RPM":None,"Watts":None,"Details":inst.get("description")})

    interface_rows=[]
    for inst,data in results["SNMP_Network_Interfaces"][1]:
        v=latest(data); display=re.sub(r"\s*\[ID:\d+\]$","",inst.get("displayName") or inst.get("name", ""))
        interface_rows.append({"Interface":display,"Description":inst.get("description"),"Status":state(v.get("OperState")),"VLAN":"Not available from LogicMonitor","Duplex":"Not available from LogicMonitor","Speed":v.get("InInterfaceSpeed") or v.get("OutInterfaceSpeed"),"Type":"SNMP","Align Errors":"Not monitored","FCS/CRC Errors":"Not monitored","TX Errors":v.get("OutErrors"),"RX Errors":v.get("InErrors"),"Undersize":"Not monitored","Out Discards":v.get("OutDiscards"),"In Utilization %":v.get("InUtilizationPercent"),"Out Utilization %":v.get("OutUtilizationPercent"),"Admin State":state(v.get("AdminState")),"Flaps":v.get("StatusFlap")})

    inventory_rows=[]
    for inst in results["Device_Component_Inventory"][0]:
        description=inst.get("description"); serial=inst.get("displayName") or inst.get("wildValue")
        inventory_rows.append({"Component":inst.get("wildAlias") or inst.get("name"),"Description":description,"PID":description if description and re.search(r"C\d{4}|PWR-|STACK-",description,re.I) else "Not available from LogicMonitor","VID":"Not available from LogicMonitor","Serial Number":serial})
    neighbor_rows=[]
    for protocol in ("LLDP_Neighbors","CDP_Neighbors"):
        for inst in results[protocol][0]: neighbor_rows.append({"Protocol":"LLDP" if protocol.startswith("LLDP") else "CDP","Local Interface":inst.get("description"),"Neighbor":inst.get("displayName") or inst.get("wildValue"),"Management IP":"Not available from LogicMonitor","Platform/Model":"Not available from LogicMonitor","Remote Port":"Not available from LogicMonitor"})
    config_rows=[]
    for inst,data in results["LogicMonitor_ConfigSource_Metrics"][1]:
        v=latest(data); config_rows.append({"Running Config":inst.get("displayName") or inst.get("name"),"Running Saved":v.get("Result") in (0,1),"Startup Config":"LogicMonitor ConfigSource","Startup Saved":v.get("CleanExit")==1,"Collected":datetime.now(timezone.utc).isoformat()})
    ospf_rows=[]
    for inst,data in results["OSPF_Neighbors"][1]:
        v=latest(data); ps=v.get("peerState")
        ospf_rows.append({"Neighbor":inst.get("displayName") or inst.get("name"),"State":OSPF_STATE.get(int(ps),"unknown") if isinstance(ps,(int,float)) else "Not available from LogicMonitor","Neighbor Events":v.get("neighborEvents"),"Restarts":v.get("NeighborRestart"),"Retransmit Queue":v.get("retransQueueSize")})

    model_text=props.get("auto.endpoint.model", "")
    inventory_models = [
        match.group(0).upper()
        for row in inventory_rows
        if (match := re.search(r"\bC\d{4}(?:-[A-Z0-9]+)+\b", row.get("Description") or "", re.I))
    ]
    model_match=re.search(r"Catalyst.*?\(([^)]+)\)|\b(C\d{4}[A-Z0-9-]*)\b",model_text,re.I)
    model=inventory_models[0] if inventory_models else ((model_match.group(1) or model_match.group(2)) if model_match else None)
    version_match=re.search(r"Version\s+([\w.()-]+)",model_text,re.I); os_version=version_match.group(1) if version_match else None
    bad_hardware=any(x["State"]=="Fault" for x in sensor_rows); loss=ping_now.get("PingLossPercent")
    if remote.get("hostStatus") != "normal" or bad_hardware: status="Critical"
    elif (isinstance(loss,(int,float)) and loss>=2) or (cpu is not None and cpu>=85) or (memory is not None and memory>=85): status="Warning"
    elif availability is None: status="Unknown"
    else: status="Healthy"
    uptime_values = [latest(data).get("Uptime") for _, data in results["SNMP_Host_Uptime"][1]]
    uptime = next((value for value in uptime_values if isinstance(value, (int, float))), None)
    serial = next((row["Serial Number"] for row in inventory_rows if re.fullmatch(r"[A-Z0-9]{8,}", str(row.get("Serial Number") or ""), re.I)), None)
    collection_rows = [{"DataSource": name, "Instance": ds.get("instanceNumber", 0), "Success": True, "Latest Data": "Queried from LogicMonitor", "Error": None} for name, ds in by_name.items() if name in names]
    details={"model":model,"os_version":os_version,"serial":serial,"uptime":uptime,"ping":{"loss":loss,"average":ping_now.get("average"),"max":ping_now.get("maxrtt"),"min":ping_now.get("minrtt"),"p95":percentile(avgs,.95),"availability_24h":availability},"tables":{"Interfaces":interface_rows,"Environmental and PoE":sensor_rows,"Inventory":inventory_rows,"CDP-LLDP Neighbors":neighbor_rows,"OSPF Neighbors":ospf_rows,"Configuration Backups":config_rows,"Collection Details":collection_rows,"VLANs":[],"Spanning Tree":[],"Alerts":[]},"mapping":{"device_id":device_id,"datasources":{k:{"id":v.get("id"),"name":k,"instances":v.get("instanceNumber")} for k,v in by_name.items() if k in names}}}
    return {"status":status,"availability":availability,"cpu":cpu,"memory":memory,"temperature":max(temperatures,default=None),"details":details,"model":model,"os_version":os_version}
