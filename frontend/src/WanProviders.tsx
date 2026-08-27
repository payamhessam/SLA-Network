/*
 * WanProviders.tsx — the WAN Providers page (carrier-managed provider routers).
 *
 * A deliberately separate, read-only view of the WAN providers' routers (e.g.
 * Centrilogic circuits). These devices are NOT Medline's and are never counted in the
 * company's SLA, Overview, or reports. Provider links do have a separate, evidence-
 * gated service-level report that cannot change the company's fleet SLA. The page mirrors
 * the Fleet + Overview style:
 * an executive summary, a per-provider breakdown, a router table, and a device-detail
 * view with routing tabs (BGP / OSPF / EIGRP / IP routing stats / interfaces / inventory
 * / neighbours). Only administrators can add or remove routers. Data: /wan/*.
 */
import React,{useEffect,useMemo,useState}from'react';
import{Globe,LayoutGrid,List,MapPin,Plus,RefreshCw,Router as RouterIcon,Search,ShieldAlert,Trash2,X}from'lucide-react';
import Help from'./Help';
import{usePager,Pager}from'./Paged';
import'./overview.css';
import{fmtPct}from'./format';

type Props={token:string;administrator:boolean};
const badge=(s:string)=>{const x=(s||'').toLowerCase();if(x.includes('healthy')||x.includes('monitored')&&!x.includes('not'))return'ok';if(x.includes('warn')||x.includes('applied')||x.includes('serious'))return'warn';if(x.includes('critical')||x.includes('outage'))return'crit';return'unknown'};
const reach=(v:any)=>fmtPct(v);

export default function WanProviders({token,administrator}:Props){
  const[ov,setOv]=useState<any>(null);const[rows,setRows]=useState<any[]>([]);
  const[serviceLevel,setServiceLevel]=useState<any>(null);
  const[error,setError]=useState('');const[busy,setBusy]=useState(false);
  const[detail,setDetail]=useState<any>(null);const[tab,setTab]=useState('BGP Peers');
  const[adding,setAdding]=useState(false);const[q,setQ]=useState('');const[found,setFound]=useState<any[]>([]);const[searching,setSearching]=useState(false);
  const[sites,setSites]=useState<any[]>([]);const[edit,setEdit]=useState<any|null>(null);
  const[view,setView]=useState<'table'|'cards'>('table');const pg=usePager(rows);
  const request=async(path:string,options:RequestInit={})=>{const r=await fetch('/api/v1'+path,{...options,headers:{Authorization:`Bearer ${token}`,...(options.body?{'Content-Type':'application/json'}:{})}});if(!r.ok)throw new Error((await r.json().catch(()=>({}))).detail||'Request failed');return r.status===204?null:r.json()};
  const load=async()=>{setError('');try{const[o,l,sl]=await Promise.all([request('/wan/overview'),request('/wan/routers'),request('/wan/service-level')]);setOv(o);setRows(l.items);setServiceLevel(sl)}catch(x:any){setError(x.message)}request('/settings/sites').then(s=>setSites(s||[])).catch(()=>{})};
  const saveLocation=async()=>{if(!edit)return;setBusy(true);try{await request(`/wan/routers/${edit.id}/location`,{method:'PATCH',body:JSON.stringify({city:edit.city,province:edit.province})});setEdit(null);await load()}catch(x:any){setError(x.message)}finally{setBusy(false)}};
  useEffect(()=>{load()},[]);
  const openDetail=async(id:number)=>{setBusy(true);try{setDetail(await request(`/wan/routers/${id}`));setTab('BGP Peers')}catch(x:any){setError(x.message)}finally{setBusy(false)}};
  const refreshOne=async(id:number)=>{setBusy(true);try{await request(`/wan/routers/${id}/refresh`,{method:'POST'});await load();if(detail?.id===id)setDetail(await request(`/wan/routers/${id}`))}catch(x:any){setError(x.message)}finally{setBusy(false)}};
  const remove=async(r:any)=>{if(!confirm(`Remove "${r.display_name}" from WAN Providers? This only removes it from this view — LogicMonitor is untouched.`))return;setBusy(true);try{await request(`/wan/routers/${r.id}`,{method:'DELETE'});if(detail?.id===r.id)setDetail(null);await load()}catch(x:any){setError(x.message)}finally{setBusy(false)}};
  const search=async()=>{if(q.trim().length<2)return;setSearching(true);setError('');try{setFound((await request('/wan/search?q='+encodeURIComponent(q.trim()))).items)}catch(x:any){setError(x.message)}finally{setSearching(false)}};
  const add=async(lm_device_id:number)=>{setBusy(true);try{await request('/wan/routers',{method:'POST',body:JSON.stringify({lm_device_id})});setFound(f=>f.filter(x=>x.lm_device_id!==lm_device_id));await load()}catch(x:any){setError(x.message)}finally{setBusy(false)}};

  if(detail)return <WanDetail d={detail} back={()=>setDetail(null)} tab={tab} setTab={setTab} admin={administrator} busy={busy} refresh={()=>refreshOne(detail.id)}/>;

  return <div className="ov">
    <div className="ov-exec"><div><div className="ov-eyebrow">WAN PROVIDER NETWORK</div><h1 className="ov-title">WAN Providers</h1></div>
      {administrator&&<button className="ov-export" onClick={()=>{setAdding(a=>!a);setFound([]);setQ('')}}>{adding?<X size={15}/>:<Plus size={15}/>} {adding?'Close':'Add router'}</button>}</div>
    {error&&<div className="ov-summary" style={{borderLeftColor:'var(--ov-crit)'}}><p style={{color:'var(--ov-crit)'}}>{error}</p></div>}

    <div className="ov-summary"><h2>Provider-managed — separate from Medline metrics<Help text="These are the WAN providers' routers (e.g. Centrilogic circuits), shown read-only for visibility. Their separate monthly service-level report never changes Medline's fleet SLA, Overview, or trends."/></h2>
      <p>{ov?.note||'WAN provider-managed routers — shown for visibility only.'}</p></div>

    <div className="ov-panel"><div className="ov-panel-head"><h2><ShieldAlert size={15}/> Internet service level<Help text="A separate monthly measure for each provider link. The 99.9% target is shown only when enough real observations cover the month. A planned provider repair remains counted; a gap in collection withholds the SLA number instead of making it look better."/></h2><span className="tag">Target {fmtPct(serviceLevel?.target)}</span></div>
      <div className="ov-table-wrap"><table className="ov-table"><thead><tr>{['Site','Current service','Provider link','Monthly result','Evidence','Action'].map(h=><th key={h}>{h}</th>)}</tr></thead>
        <tbody>{(serviceLevel?.sites||[]).flatMap((site:any)=>(site.links||[]).map((link:any,index:number)=><tr key={link.router_id}>
          <td>{index===0?site.site:'—'}</td><td>{index===0?<span className={'ov-badge '+badge(site.state)}>{site.state}</span>:'—'}</td>
          <td>{link.provider}<div className="ov-sub">{link.name}</div></td>
          <td className="mono">{link.availability==null?<span className="muted">Insufficient evidence</span>:fmtPct(link.availability)}</td>
          <td className="mono">{fmtPct(link.coverage)} <span className="muted">coverage</span></td><td>{index===0?site.action:'—'}</td>
        </tr>))}</tbody></table></div>
      {!serviceLevel?.sites?.length&&<div className="ds-empty"><Globe size={26}/><div>No provider links have been added yet.</div></div>}
    </div>

    {administrator&&adding&&<div className="ov-panel"><div className="ov-panel-head"><h2><Search size={15}/> Add a router from LogicMonitor</h2></div>
      <div style={{padding:'16px 18px'}}>
        <div style={{display:'flex',gap:8}}><input value={q} onChange={e=>setQ(e.target.value)} onKeyDown={e=>e.key==='Enter'&&search()} placeholder="Search LogicMonitor by name or IP (e.g. Centrilogic, Bell, 900 Harbourside)" style={{flex:1,padding:'9px 12px',background:'var(--ov-surface-low)',border:'1px solid var(--ov-line)',borderRadius:6,color:'var(--ov-on)',fontFamily:'var(--ov-mono)',fontSize:13}}/><button className="ov-export" onClick={search} disabled={searching}>{searching?'Searching…':'Search'}</button></div>
        {found.length>0&&<div className="ov-table-wrap" style={{marginTop:14}}><table className="ov-table"><thead><tr><th>LogicMonitor device</th><th>Management IP</th><th>LM id</th><th></th></tr></thead>
          <tbody>{found.map(f=><tr key={f.lm_device_id}><td>{f.display_name}</td><td className="mono">{f.management_ip||'—'}</td><td className="mono">{f.lm_device_id}</td><td><button className="ov-export" onClick={()=>add(f.lm_device_id)} disabled={busy}><Plus size={14}/> Add</button></td></tr>)}</tbody></table></div>}
      </div></div>}

    <div className="ov-kpis" style={{gridTemplateColumns:'repeat(4,1fr)'}}>
      <Kpi label="Routers" value={ov?ov.routers:'—'} sub={ov?`${ov.healthy} healthy · ${ov.warning} warning · ${ov.critical} critical`:''}/>
      <Kpi label="Avg reachability (24h)" value={ov?reach(ov.avg_reachability):'—'} sub="Ping success across routers"/>
      <Kpi label="BGP peers established" value={ov?`${ov.bgp_established}/${ov.bgp_peers}`:'—'} sub="Established / total"/>
      <Kpi label="OSPF full · interfaces up" value={ov?`${ov.ospf_full} · ${ov.interfaces_up}/${ov.interfaces}`:'—'} sub="Full adjacencies · up interfaces"/>
    </div>

    <div className="ov-panel"><div className="ov-panel-head"><h2><Globe size={15}/> By Provider<Help text="Routers grouped by the provider/circuit parsed from each device name. Counts are descriptive (routers, established BGP peers, full OSPF adjacencies, healthy routers) — no availability is scored."/></h2><span className="tag">{ov?.providers?.length||0} providers</span></div>
      <div className="ov-bu">{(ov?.providers||[]).map((p:any,i:number)=><div key={i} className="ov-bu-card"><div className="ov-bu-top"><h3 style={{fontSize:15}}>{p.provider}</h3><span className="ov-badge unknown">{p.routers} rtr</span></div>
        <div className="ov-bu-stats" style={{gridTemplateColumns:'repeat(3,1fr)',borderTop:0,paddingTop:0}}><div><span>Healthy<Help text="Provider routers currently reporting a healthy state. These are the carrier's devices, not Medline's — they are shown for visibility and are never counted in Medline's SLA."/></span><b>{p.healthy}/{p.routers}</b></div><div><span>BGP est.<Help text="BGP sessions in the 'established' state — the routing conversation with the carrier is up and exchanging routes. A session that is not established means that path is not usable."/></span><b>{p.bgp}</b></div><div><span>OSPF full<Help text="OSPF neighbour relationships in the 'full' state, meaning the two routers have finished exchanging their routing tables and the link is fully usable."/></span><b>{p.ospf}</b></div></div></div>)}</div></div>

    <div className="ov-panel"><div className="ov-panel-head"><h2><RouterIcon size={15}/> Provider Routers<Help text="Every WAN provider router with live read-only state: status, 24h reachability, established BGP peers, full OSPF adjacencies, and up interfaces. Switch between table and card views, and open a router for full routing detail. Admins can add or remove routers."/></h2>
      <div style={{display:'flex',alignItems:'center',gap:12}}><div className="ds-viewtoggle"><button type="button" className={view==='table'?'active':''} onClick={()=>setView('table')}><List size={14}/> Table</button><button type="button" className={view==='cards'?'active':''} onClick={()=>setView('cards')}><LayoutGrid size={14}/> Cards</button></div><span className="tag">{rows.length} routers</span></div></div>
      {rows.length===0?<div className="ds-empty"><RouterIcon size={26}/><div>No WAN routers yet.{administrator?' Use “Add router” to import one from LogicMonitor.':''}</div></div>:view==='table'?
      <div className="ov-table-wrap"><table className="ov-table"><thead><tr>{['Router','Provider / Circuit','Site','City / State','Status','Reach 24h','BGP','OSPF','Interfaces','Last sync',''].map(h=><th key={h}>{h}</th>)}</tr></thead>
        <tbody>{pg.slice.map((r:any)=><tr key={r.id}>
          <td><a style={{color:'var(--ov-primary)',cursor:'pointer',fontWeight:600}} onClick={()=>openDetail(r.id)}>{r.display_name}</a></td>
          <td>{r.provider||'—'}</td><td>{r.site_label||'—'}</td>
          <td>{r.city?`${r.city}${r.province?', '+r.province:''}`:<span className="muted">—</span>}{administrator&&<button title="Set city/state" onClick={()=>setEdit({id:r.id,display_name:r.display_name,city:r.city||'',province:r.province||''})} style={{background:'none',border:0,color:'var(--ov-faint)',cursor:'pointer',marginLeft:6,verticalAlign:'middle'}}><MapPin size={13}/></button>}</td>
          <td><span className={'ov-badge '+badge(r.status)}>{(r.status||'Unknown').toUpperCase()}</span></td>
          <td className="mono">{reach(r.reachability)}</td>
          <td className="mono">{r.bgp_peers!=null?`${r.bgp_established??0}/${r.bgp_peers}`:'—'}</td>
          <td className="mono">{r.ospf_full!=null?r.ospf_full:'—'}</td>
          <td className="mono">{r.interfaces!=null?`${r.interfaces_up??0}/${r.interfaces}`:'—'}</td>
          <td className="mono" style={{color:'var(--ov-faint)'}}>{r.last_sync?new Date(r.last_sync).toLocaleString():'—'}</td>
          <td style={{whiteSpace:'nowrap'}}><button className="ov-export" style={{padding:'4px 8px'}} onClick={()=>openDetail(r.id)}>Detail</button>{administrator&&<button className="ov-export" style={{padding:'4px 8px',marginLeft:6}} onClick={()=>remove(r)} disabled={busy} title="Remove"><Trash2 size={13}/></button>}</td>
        </tr>)}</tbody></table></div>
      :<div className="ov-bu" style={{padding:18}}>{pg.slice.map((r:any)=><div key={r.id} className="ov-bu-card">
        <div className="ov-bu-top"><h3 style={{fontSize:14}}><a style={{color:'var(--ov-primary)',cursor:'pointer'}} onClick={()=>openDetail(r.id)}>{r.display_name}</a></h3><span className={'ov-badge '+badge(r.status)}>{(r.status||'Unknown').toUpperCase()}</span></div>
        <div className="ov-sub" style={{fontSize:12}}>{r.provider||'—'}{r.city?` · ${r.city}${r.province?', '+r.province:''}`:''}</div>
        <div className="ov-bu-stats"><div><span>Reach 24h<Help text="How much of the last 24 hours the provider router answered monitoring probes. Descriptive only — this is the carrier's equipment and does not feed Medline's SLA numbers."/></span><b>{reach(r.reachability)}</b></div><div><span>BGP<Help text="Established BGP sessions out of the total configured on this router — the routing conversations with the carrier that are actually up."/></span><b>{r.bgp_peers!=null?`${r.bgp_established??0}/${r.bgp_peers}`:'—'}</b></div><div><span>OSPF<Help text="OSPF neighbour adjacencies in the 'full' state on this router, meaning routing information has been fully exchanged."/></span><b>{r.ospf_full??'—'}</b></div><div><span>Interfaces<Help text="Interfaces currently up out of the total on this provider router."/></span><b>{r.interfaces!=null?`${r.interfaces_up??0}/${r.interfaces}`:'—'}</b></div></div>
        <div style={{display:'flex',gap:6,marginTop:4}}><button className="ov-export" style={{padding:'5px 10px'}} onClick={()=>openDetail(r.id)}>Detail</button>{administrator&&<button className="ov-export" style={{padding:'5px 10px'}} onClick={()=>remove(r)} disabled={busy}><Trash2 size={13}/> Remove</button>}</div>
      </div>)}</div>}
      {rows.length>0&&<Pager {...pg} label="routers"/>}</div>

    {edit&&<div onClick={()=>setEdit(null)} style={{position:'fixed',inset:0,background:'rgba(4,12,20,.55)',display:'grid',placeItems:'center',zIndex:80}}>
      <div onClick={e=>e.stopPropagation()} style={{background:'var(--ov-surface)',border:'1px solid var(--ov-line)',borderRadius:10,padding:22,width:'min(460px,92vw)',color:'var(--ov-on)'}}>
        <h2 style={{margin:'0 0 4px',fontSize:17}}>Set city / state</h2>
        <p style={{margin:'0 0 16px',color:'var(--ov-dim)',fontSize:13}}>{edit.display_name}</p>
        <label style={{fontFamily:'var(--ov-mono)',fontSize:11,textTransform:'uppercase',letterSpacing:'.05em',color:'var(--ov-faint)'}}>Pick a Medline branch (auto-fills)</label>
        <select value="" onChange={e=>{const s=sites.find(x=>String(x.id)===e.target.value);if(s)setEdit({...edit,city:s.city||'',province:s.province_region||''})}} style={{width:'100%',margin:'6px 0 14px',padding:'9px 10px',background:'var(--ov-surface-low)',border:'1px solid var(--ov-line)',borderRadius:6,color:'var(--ov-on)',fontSize:13}}>
          <option value="">— Select a CA branch to auto-fill —</option>
          {sites.map(s=><option key={s.id} value={s.id}>{s.site_code} — {s.city}{s.province_region?', '+s.province_region:''}</option>)}
        </select>
        <div style={{display:'grid',gridTemplateColumns:'1fr 1fr',gap:12}}>
          <div><label style={{fontFamily:'var(--ov-mono)',fontSize:11,textTransform:'uppercase',letterSpacing:'.05em',color:'var(--ov-faint)'}}>City</label>
            <input value={edit.city} onChange={e=>setEdit({...edit,city:e.target.value})} placeholder="e.g. Delta" style={{width:'100%',marginTop:6,padding:'9px 10px',background:'var(--ov-surface-low)',border:'1px solid var(--ov-line)',borderRadius:6,color:'var(--ov-on)',fontSize:13}}/></div>
          <div><label style={{fontFamily:'var(--ov-mono)',fontSize:11,textTransform:'uppercase',letterSpacing:'.05em',color:'var(--ov-faint)'}}>State / Province</label>
            <input value={edit.province} onChange={e=>setEdit({...edit,province:e.target.value})} placeholder="e.g. BC" style={{width:'100%',marginTop:6,padding:'9px 10px',background:'var(--ov-surface-low)',border:'1px solid var(--ov-line)',borderRadius:6,color:'var(--ov-on)',fontSize:13}}/></div>
        </div>
        <div style={{display:'flex',justifyContent:'flex-end',gap:10,marginTop:20}}>
          <button className="ov-export" onClick={()=>setEdit(null)}>Cancel</button>
          <button className="ov-export" style={{background:'var(--ov-primary)',color:'#04203b',borderColor:'var(--ov-primary)'}} onClick={saveLocation} disabled={busy}>{busy?'Saving…':'Save'}</button>
        </div>
      </div></div>}
  </div>;
}

function Kpi({label,value,sub}:{label:string;value:any;sub:string}){
  return <div className="ov-card" style={{minHeight:120}}><h3>{label}</h3><div className="center"><div className="ov-ring-val" style={{fontSize:28}}>{value}</div><div className="ov-sub" style={{textAlign:'center'}}>{sub}</div></div></div>;
}

const TABS=['BGP Peers','OSPF Neighbors','EIGRP Peers','IP Routing Stats','Interfaces','Inventory','Neighbors'];
function WanDetail({d,back,tab,setTab,admin,busy,refresh}:{d:any;back:()=>void;tab:string;setTab:(t:string)=>void;admin:boolean;busy:boolean;refresh:()=>void}){
  const det=d.details||{};const tables=det.tables||{};const rows=tables[tab]||[];
  const cov=det.routing_coverage||[];
  const cols=useMemo(()=>rows.length?Object.keys(rows[0]):[],[rows]);
  return <div className="ov">
    <div className="ov-exec"><div><div className="ov-eyebrow" style={{cursor:'pointer'}} onClick={back}>← WAN PROVIDERS</div><h1 className="ov-title" style={{fontSize:24}}>{d.display_name}</h1><div className="ov-sub">{d.provider} · {d.site_label}{d.city?` · ${d.city}${d.province?', '+d.province:''}`:''} · {d.management_ip||'no IP'}</div></div>
      {admin&&<button className="ov-export" onClick={refresh} disabled={busy}><RefreshCw size={15}/> {busy?'Refreshing…':'Refresh'}</button>}</div>

    <div className="ov-kpis" style={{gridTemplateColumns:'repeat(5,1fr)'}}>
      <Kpi label="Status" value={(d.status||'Unknown').toUpperCase()} sub="Informational only"/>
      <Kpi label="Reachability 24h" value={reach(d.reachability)} sub="Ping success"/>
      <Kpi label="CPU / Memory" value={`${fmtPct(d.cpu)} / ${fmtPct(d.memory)}`} sub="Latest sample"/>
      <Kpi label="BGP established" value={`${det.counts?.bgp_established??0}/${det.counts?.bgp_peers??0}`} sub="Peers"/>
      <Kpi label="OSPF full" value={det.counts?.ospf_full??0} sub={`${det.counts?.interfaces_up??0}/${det.counts?.interfaces??0} interfaces up`}/>
    </div>

    <div className="ov-panel"><div className="ov-panel-head"><h2><ShieldAlert size={15}/> Routing Coverage<Help text="What LogicMonitor actually exposes for THIS router. 'Monitored' means live instances exist; 'Applied · no instances' means the datasource is attached but empty; 'Not monitored' means no datasource. Static routes are not monitored in this tenant."/></h2></div>
      <div className="ov-table-wrap"><table className="ov-table"><thead><tr><th>Protocol</th><th>LogicMonitor source</th><th>Coverage</th><th>Instances</th></tr></thead>
        <tbody>{cov.map((c:any,i:number)=><tr key={i}><td>{c.protocol}</td><td className="mono">{c.source}</td><td><span className={'ov-badge '+badge(c.status)}>{c.status}</span></td><td className="mono">{c.peers}</td></tr>)}</tbody></table></div></div>

    <div className="ov-panel"><div className="ov-panel-head" style={{gap:8,flexWrap:'wrap'}}>{TABS.map(t=><button key={t} className="ov-export" style={{padding:'6px 12px',background:t===tab?'var(--ov-primary)':'var(--ov-surface-high)',color:t===tab?'#04203b':'var(--ov-on)',borderColor:t===tab?'var(--ov-primary)':'var(--ov-line)'}} onClick={()=>setTab(t)}>{t}{tables[t]?.length?` (${tables[t].length})`:''}</button>)}</div>
      <div className="ov-table-wrap">{rows.length?<table className="ov-table"><thead><tr>{cols.map(c=><th key={c}>{c}</th>)}</tr></thead>
        <tbody>{rows.map((row:any,i:number)=><tr key={i}>{cols.map(c=><td key={c} className={typeof row[c]==='number'?'mono':''}>{row[c]==null?<span className="muted">—</span>:String(row[c])}</td>)}</tr>)}</tbody></table>
        :<div style={{padding:22,textAlign:'center',color:'var(--ov-dim)'}}>Not monitored — LogicMonitor exposes no {tab.toLowerCase()} for this router.</div>}</div></div>
  </div>;
}
