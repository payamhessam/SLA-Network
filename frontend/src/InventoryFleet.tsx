/*
 * InventoryFleet.tsx — the Device Fleet & SLA Compliance page.
 *
 * Shows the operational switch/router inventory with fleet summary KPIs, the 12-week
 * compliance combo chart (availability line + downtime bars + SLA target line, from
 * /trends), the per-device fleet status table (WTD/YTD availability), and the critical
 * applications panel. Data comes from /inventory, /sla/summary, and /trends; each
 * section carries a <Help> ⓘ tooltip describing what it means.
 */
import React,{useEffect,useMemo,useState}from'react';
import{Activity,AlertTriangle,CheckCircle2,Download,Filter,MonitorCog,RefreshCw,Server,ShieldCheck}from'lucide-react';
import './fleet.css';
import Help from './Help';

type Props={token:string;open:(device:any)=>void};
type Uptime={display:string;last_polled?:string|null;state:'valid'|'pending'|'failed'};

export default function InventoryFleet({token,open}:Props){
  const[data,setData]=useState<any>({items:[],total:0,page:1,pages:1});
  const[uptimes,setUptimes]=useState<Record<number,Uptime>>({});
  const[opening,setOpening]=useState<number|null>(null);const[loading,setLoading]=useState(false);const[error,setError]=useState('');
  const[q,setQ]=useState('');const[site,setSite]=useState('');const[slaSummary,setSlaSummary]=useState<any>(null);const[trend,setTrend]=useState<any>(null);
  const request=async(path:string,options:RequestInit={})=>{const response=await fetch('/api/v1'+path,{...options,headers:{Authorization:`Bearer ${token}`}});if(!response.ok)throw new Error((await response.json()).detail||'Request failed');return response.json()};
  const load=async(page=1)=>{setLoading(true);setError('');try{const params=new URLSearchParams({page:String(page),page_size:'25'});if(q)params.set('q',q);if(site)params.set('site',site);const result=await request('/inventory/devices?'+params);setData(result);request('/sla/summary').then(setSlaSummary).catch(()=>{});request('/trends').then(setTrend).catch(()=>{});const pairs=await Promise.all(result.items.map(async(d:any)=>{try{const value=await request(`/inventory/devices/${d.id}/uptime`);return[d.id,{display:value.display||'Not monitored',last_polled:value.last_polled,state:value.raw_seconds==null?'pending':'valid'}]}catch{return[d.id,{display:'Collection failed',state:'failed'}]}}));setUptimes(Object.fromEntries(pairs))}catch(x:any){setError(x.message||'Collection failed')}finally{setLoading(false)}};
  const showDetail=async(device:any)=>{setOpening(device.id);try{const result=await request(`/inventory/devices/${device.id}/open-detail`,{method:'POST'});open(result.device)}catch(x:any){setError(x.message||'Collection failed')}finally{setOpening(null)}};
  useEffect(()=>{load()},[]);
  const stats=useMemo(()=>{const valid=Object.values(uptimes).filter(x=>x.state==='valid').length;const failed=Object.values(uptimes).filter(x=>x.state==='failed').length;const gaps=data.items.filter((x:any)=>x.logicmonitor_match_status!=='Matched').length;return{valid,failed,gaps}},[data,uptimes]);
  const lastSync=useMemo(()=>{const values=Object.values(uptimes).map(x=>x.last_polled).filter(Boolean).sort();return values.length?new Date(values[values.length-1]!).toLocaleString():'Baseline pending'},[uptimes]);
  const slaByLm=useMemo(()=>{const m:Record<number,any>={};(slaSummary?.devices||[]).forEach((e:any)=>{if(e.lm_device_id!=null)m[e.lm_device_id]=e});return m},[slaSummary]);
  const fmt=(w:any)=>w&&w.availability!=null?w.availability.toFixed(3)+'%':(w?.status||'Baseline pending');
  const exportCsv=()=>{const rows=[['Hostname','Management IP','Site','City','Region','Current Uptime'],...data.items.map((d:any)=>[d.generated_name,d.management_ip||'',d.site_code,d.city,d.province_region||'',uptimes[d.id]?.display||'Collection pending'])];const csv=rows.map(r=>r.map((v:any)=>`"${String(v).replaceAll('"','""')}"`).join(',')).join('\n');const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));a.download='Device_Fleet_Current_View.csv';a.click();URL.revokeObjectURL(a.href)};
  return <div className="sentinel-fleet">
    <div className="fleet-title"><div><span className="fleet-kicker"><ShieldCheck size={15}/> Reliability Sentinel</span><h2>Fleet &amp; SLA Compliance</h2><p>Real-time telemetry and compliance tracking.</p></div><button className="fleet-refresh" onClick={()=>load(data.page)} disabled={loading}><RefreshCw size={16}/>{loading?'Refreshing…':'Refresh Data'}</button></div>
    {error&&<div className="fleet-error"><AlertTriangle size={16}/>{error}</div>}
    <section><h3 className="fleet-section-title">Fleet Summary<Help text="Live headline counts for the switches registered in Device Fleet: total physical network devices (stack members counted, access points excluded), how many report valid LogicMonitor uptime, fleet YTD availability, devices below the SLA target, monitoring gaps (unmapped), and collection errors."/></h3><div className="fleet-kpis">
      <article className="wide"><Server/><span>Total Network Devices</span><strong>{data.total_physical??data.total}</strong><small>Switches and routers · stack members counted</small></article>
      <article className="wide"><CheckCircle2/><span>Valid Uptime</span><strong>{stats.valid}</strong><small>Live LogicMonitor evidence</small></article>
      <article><span>YTD Availability</span><strong className={slaSummary?.fleet_ytd?.availability!=null?'':'pending'}>{slaSummary?fmt(slaSummary.fleet_ytd):'Baseline pending'}</strong></article>
      <article><span>Below SLA</span><strong className={slaSummary?(slaSummary.below_target?'danger':''):'pending'}>{slaSummary?slaSummary.below_target:'Insufficient evidence'}</strong></article>
      <article><span>Monitoring Gaps</span><strong>{stats.gaps}</strong></article>
      <article><span>Collection Errors</span><strong className={stats.failed?'danger':''}>{stats.failed}</strong></article>
    </div></section>
    <section><div className="fleet-section-head"><h3 className="fleet-section-title">Compliance Trends<Help text="12-week fleet reliability trend. The blue line is weekly availability (left % axis); the orange bars are weekly downtime minutes (right axis); the dashed amber line is the SLA target. Downtime bars spike where the availability line dips. The coverage badge shows how complete the underlying evidence is."/></h3><span className="confidence"><i/> Coverage: {slaSummary?slaSummary.fleet_ytd.coverage.toFixed(1)+'%':'Baseline pending'}</span></div>
      {trend&&trend.availability_trend.series.some((w:any)=>w.availability!=null)?(()=>{
        const s=trend.availability_trend.series,tgt=trend.availability_trend.target;const vals=s.map((w:any)=>w.availability).filter((x:any)=>x!=null);const wow=trend.deltas.wow,mom=trend.deltas.mom;const tc=(t:string)=>t==='WORSENING'?'#ffb4ab':t==='IMPROVING'?'#7fe0b6':'#c2c6d2';
        const W=840,H=210,padL=50,padR=48,padT=14,padB=26,pw=W-padL-padR,ph=H-padT-padB;const aLo=Math.max(0,Math.min(tgt,...(vals.length?vals:[tgt]))-0.1),aHi=100;const yA=(v:number)=>padT+ph-((v-aLo)/((aHi-aLo)||1))*ph;const dMax=Math.max(1,...s.map((w:any)=>w.downtime_minutes||0));const step=pw/s.length,bw=Math.min(step*0.6,24);const cx=(i:number)=>padL+step*i+step/2;const line=s.map((w:any,i:number)=>w.availability!=null?`${cx(i).toFixed(1)},${yA(w.availability).toFixed(1)}`:null).filter(Boolean).join(' ');
        return <div className="trend-panel"><div><span>Availability vs Target ({tgt}%)</span><span>12-Week Trend</span></div>
          <svg viewBox={`0 0 ${W} ${H}`} style={{width:'100%',height:'auto',display:'block',margin:'6px 0'}}>
            {[aHi,tgt,aLo].map((v,i)=><g key={i}><line x1={padL} y1={yA(v)} x2={W-padR} y2={yA(v)} stroke={v===tgt?'#e0873a':'#2a3441'} strokeWidth={v===tgt?1.2:1} strokeDasharray={v===tgt?'5 4':undefined}/><text x={padL-6} y={yA(v)+3} textAnchor="end" fontFamily="monospace" fontSize={10} fill={v===tgt?'#e0873a':'#8fa3b5'}>{v.toFixed(v===aLo?2:1)}%</text></g>)}
            <text x={W-padR+6} y={padT+4} fontFamily="monospace" fontSize={10} fill="#f5a623">0</text>
            <text x={W-padR+6} y={padT+ph} fontFamily="monospace" fontSize={10} fill="#f5a623">{dMax}m</text>
            {s.map((w:any,i:number)=>{const b=((w.downtime_minutes||0)/dMax)*ph;return <rect key={i} x={cx(i)-bw/2} y={padT+ph-b} width={bw} height={Math.max(0,b)} rx={2} fill="#f5a623" opacity={0.5}><title>{`Week of ${w.week_start}: downtime ${w.downtime_minutes||0} min`}</title></rect>})}
            <polyline points={line} fill="none" stroke="#3f7fd4" strokeWidth={2.4} strokeLinejoin="round" strokeLinecap="round"/>
            {s.map((w:any,i:number)=>w.availability!=null?<circle key={i} cx={cx(i)} cy={yA(w.availability)} r={3.2} fill="#3f7fd4"><title>{`Week of ${w.week_start}: ${w.availability.toFixed(3)}% · coverage ${w.coverage.toFixed(0)}%`}</title></circle>:null)}
            {s.map((w:any,i:number)=>(i%3===0||i===s.length-1)?<text key={i} x={cx(i)} y={H-7} textAnchor="middle" fontFamily="monospace" fontSize={9} fill="#8fa3b5">{w.week_start.slice(5)}</text>:null)}
          </svg>
          <div style={{display:'flex',gap:18,fontFamily:'monospace',fontSize:11,color:'#8fa3b5',margin:'2px 0 4px'}}><span><i style={{display:'inline-block',width:14,height:3,background:'#3f7fd4',verticalAlign:'middle',marginRight:5}}/>Availability</span><span><i style={{display:'inline-block',width:10,height:10,background:'#f5a623',opacity:.6,verticalAlign:'middle',marginRight:5}}/>Downtime (min)</span><span><i style={{display:'inline-block',width:14,height:0,borderTop:'1.5px dashed #e0873a',verticalAlign:'middle',marginRight:5}}/>Target</span></div>
          <div style={{display:'flex',gap:24,fontFamily:'monospace',fontSize:12,marginTop:6,color:'#c2c6d2'}}>
            <span>Week-over-week: {wow.delta!=null?(wow.delta>=0?'+':'')+wow.delta.toFixed(3)+'%':'—'} <b style={{color:tc(wow.trend)}}>{wow.trend}</b></span>
            <span>Month-over-month: {mom.delta!=null?(mom.delta>=0?'+':'')+mom.delta.toFixed(3)+'%':'—'} <b style={{color:tc(mom.trend)}}>{mom.trend}</b></span>
          </div></div>;
      })():<div className="trend-panel"><div><span>Availability vs Target</span><span>12W Trend</span></div><div className="trend-grid"><div className="trend-empty"><Activity size={22}/><b>Baseline pending</b><small>Historical availability collection is required before a governed SLA trend can be displayed.</small></div></div></div>}
    </section>
    <section><div className="fleet-section-head fleet-status-head"><h3 className="fleet-section-title">Fleet Status<Help text="Every switch in inventory with its site, management IP, current uptime, and week-to-date / year-to-date availability. Filter by search or site code; click a device name to open its full LogicMonitor detail (health, interfaces, neighbors, OSPF). WTD/YTD show 'Insufficient evidence' when coverage is below 90%."/></h3><div><button aria-label="Apply filters" onClick={()=>load()}><Filter size={15}/></button><button aria-label="Export current view" onClick={exportCsv}><Download size={15}/></button></div></div>
      <div className="fleet-filters"><label>Search<input value={q} onChange={e=>setQ(e.target.value)} onKeyDown={e=>e.key==='Enter'&&load()}/></label><label>Site Code<input value={site} onChange={e=>setSite(e.target.value.toUpperCase())} placeholder="All sites"/></label><button onClick={()=>load()}>Apply</button></div>
      <div className="fleet-device-list">{data.items.map((d:any)=>{const uptime=uptimes[d.id];const matched=d.logicmonitor_match_status==='Matched';const s=slaByLm[d.logicmonitor_device_id];return <article key={d.id} className={!matched?'has-gap':''}><div className="device-card-top"><button className="fleet-device-name" disabled={opening===d.id} onClick={()=>showDetail(d)}><Server size={19}/>{opening===d.id?'Loading details…':d.generated_name}</button><span className={'fleet-state '+(matched?'healthy':'pending')}>{matched?'Monitored':'Mapping pending'}</span></div><div className="device-card-grid"><div><span>Management IP</span><b>{d.management_ip||'Mapping pending'}</b></div><div><span>Site</span><b>{d.site_code} · {d.city}</b></div><div><span>Region</span><b>{d.province_region||'Not monitored'}</b></div><div><span>Current Uptime</span><b className={uptime?.state==='failed'?'danger':''}>{uptime?.display||'Collection pending'}</b></div><div><span>WTD Availability</span><b className={s?.wtd?.availability!=null?'':'pending'}>{fmt(s?.wtd)}</b></div><div><span>YTD Availability</span><b className={s?.ytd?.availability!=null?'':'pending'}>{fmt(s?.ytd)}</b></div></div></article>})}</div>
      {!data.items.length&&!loading&&<div className="fleet-empty">No inventory devices match the selected filters.</div>}<div className="fleet-pagination"><button disabled={data.page<=1} onClick={()=>load(data.page-1)}>Previous</button><span>Page {data.page} of {data.pages}</span><button disabled={data.page>=data.pages} onClick={()=>load(data.page+1)}>Next</button></div>
    </section>
    <section><h3 className="fleet-section-title lined">Critical Applications<Help text="Business-application SLA (e.g. EHR, ordering). It stays 'Mapping pending' until an authoritative LogicMonitor application SLI is mapped — application health is intentionally not inferred from device health, so nothing is fabricated here."/></h3><div className="application-placeholder"><MonitorCog/><div><b>Application mappings pending</b><span>Application SLA unavailable — no authoritative LogicMonitor application SLI mapped.</span></div><span>Mapping pending</span></div></section>
    <footer className="fleet-quality"><div><RefreshCw size={15}/><span>Last Sync: {lastSync}</span></div><div><i className={error?'bad':''}/><span>API Status: {error?'Collection failed':'Connected'}</span></div></footer>
  </div>
}
