import React,{useEffect,useState}from'react';
import './fleet.css';

type Props={token:string;open:(device:any)=>void};

export default function InventoryFleet({token,open}:Props){
  const[data,setData]=useState<any>({items:[],total:0,page:1,pages:1});
  const[uptimes,setUptimes]=useState<Record<number,string>>({});
  const[opening,setOpening]=useState<number|null>(null);
  const[q,setQ]=useState('');const[site,setSite]=useState('');
  const request=async(path:string,options:RequestInit={})=>{const response=await fetch('/api/v1'+path,{...options,headers:{Authorization:`Bearer ${token}`}});if(!response.ok)throw new Error((await response.json()).detail||'Request failed');return response.json()};
  const load=async(page=1)=>{const params=new URLSearchParams({page:String(page),page_size:'25'});if(q)params.set('q',q);if(site)params.set('site',site);const result=await request('/inventory/devices?'+params);setData(result);const pairs=await Promise.all(result.items.map(async(d:any)=>{try{return[d.id,(await request(`/inventory/devices/${d.id}/uptime`)).display||'Unavailable']}catch{return[d.id,'Unavailable']}}));setUptimes(Object.fromEntries(pairs))};
  const showDetail=async(device:any)=>{setOpening(device.id);try{const result=await request(`/inventory/devices/${device.id}/open-detail`,{method:'POST'});open(result.device)}finally{setOpening(null)}};
  useEffect(()=>{load()},[]);
  return <section className="panel inventory-panel"><div className="panelhead"><div><h2>Device Fleet</h2><p className="muted">Inventory identity and live LogicMonitor uptime</p></div><span className="pill">{data.total} devices</span></div><div className="inventory-controls"><label>Search<input value={q} onChange={e=>setQ(e.target.value)} onKeyDown={e=>e.key==='Enter'&&load()}/></label><label>Site Code<input value={site} onChange={e=>setSite(e.target.value.toUpperCase())} placeholder="All"/></label><button onClick={()=>load()}>Apply Filters</button></div><div className="table-scroll"><table><thead><tr><th>Device Name</th><th>Site Code</th><th>City</th><th>Region</th><th>Management IP</th><th>Uptime</th></tr></thead><tbody>{data.items.map((d:any)=><tr key={d.id}><td><button className="device-name-link" disabled={opening===d.id} onClick={()=>showDetail(d)}>{opening===d.id?'Loading details…':d.generated_name}</button></td><td>{d.site_code}</td><td>{d.city}</td><td>{d.province_region||'—'}</td><td>{d.management_ip||'Pending LogicMonitor match'}</td><td>{uptimes[d.id]||'Loading…'}</td></tr>)}</tbody></table></div>{!data.items.length&&<div className="empty">No inventory devices match the selected filters.</div>}<div className="pagination"><button disabled={data.page<=1} onClick={()=>load(data.page-1)}>Previous</button><span>Page {data.page} of {data.pages}</span><button disabled={data.page>=data.pages} onClick={()=>load(data.page+1)}>Next</button></div></section>
}
