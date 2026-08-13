/*
 * HelpCenter.tsx — the searchable, indexed Help Center.
 *
 * Renders the content registry (helpContent.ts) as browsable articles with a search box,
 * category index, glossary and FAQ. Articles with a `live` key get a dynamic "Why this
 * number?" panel filled from GET /help/explain — the same analytics the dashboards use,
 * so Help never disagrees with a page. Deep-linkable via the URL hash (#help/<id>).
 */
import React,{useEffect,useMemo,useState} from 'react';
import{Search,BookOpen,ArrowRight,HelpCircle,ListOrdered} from 'lucide-react';
import{ARTICLES,CATEGORIES,GLOSSARY,FAQ,type Article} from './helpContent';
import'./help.css';
import{fmtPct}from'./format';

const GLOSSARY_ID='__glossary', FAQ_ID='__faq', INDEX_ID='__index';
const pct=(v:any)=>typeof v==='number'?fmtPct(v):'Insufficient evidence';

// Dynamic "Why this number?" text built from the live /help/explain payload.
function whyLines(live:string,e:any):string[]{
  if(!e)return[];
  const g=e.global_sla||{},t=e.telemetry||{};
  switch(live){
    case'global_sla':{
      const out=[`Global SLA Status is currently ${pct(g.current)} over the last 30 days, against a ${g.target}% target — ${g.status}.`];
      out.push(`Year-to-date it is ${pct(g.ytd)}, and evidence coverage is ${g.coverage?.toFixed?.(1)??g.coverage}%.`);
      if(e.criticality?.unreachable)out.push(`${e.criticality.unreachable} device(s) currently lack confirmed reachability, which holds the number below 100%.`);
      if(e.worst_site)out.push(`The lowest-availability site is ${e.worst_site.city} (${e.worst_site.site}) at ${pct(e.worst_site.ytd)} YTD.`);
      if(e.incidents?.count)out.push(`There were ${e.incidents.count} availability-derived incident(s) in the last ${e.incidents.window_days} days.`);
      return out;}
    case'criticality':{const b=e.criticality?.bands||{};return[
      `There are ${e.criticality?.total} devices in the fleet: ${b.Critical||0} Critical, ${b.High||0} High, ${b.Medium||0} Medium, ${b.Low||0} Low.`,
      `${e.criticality?.degraded||0} are degraded and ${e.criticality?.unreachable||0} are unreachable right now.`];}
    case'business_units':return(e.business_units||[]).map((b:any)=>`${b.name}: ${pct(b.ytd)} YTD across ${b.devices} devices and ${b.sites} site(s), ${b.incidents} incident(s) — ${b.status}.${b.reason?' '+b.reason:''}`);
    case'worst_site':return e.worst_site?[`The lowest-availability site is ${e.worst_site.city} (${e.worst_site.site}) at ${pct(e.worst_site.ytd)} year-to-date.`]:['No site is currently below target.'];
    case'incidents':return[
      `There were ${e.incidents?.count} incident(s) in the last ${e.incidents?.window_days} days.`,
      `Average time to recover (MTTR) ≈ ${e.incidents?.mttr_minutes??'—'} minutes; mean time between failures (MTBF) ≈ ${e.incidents?.mtbf_hours??'—'} hours.`];
    case'trends':return[`Week-over-week the fleet is ${e.trends?.wow}, and month-over-month it is ${e.trends?.mom}.`];
    case'latency':return[`Average latency is ${t.latency_avg??'—'} ms and the worst-case is ${t.latency_max??'—'} ms across the fleet.`,`Average packet loss is ${t.loss_avg??'—'}%.`];
    case'loss':return[`Average packet loss is ${t.loss_avg??'—'}% across the fleet.`,`Average latency is ${t.latency_avg??'—'} ms.`];
    case'interfaces':return[`Of ${t.if_total} interfaces, ${t.if_up} are up, ${t.if_down} are down (admin-up), ${t.if_high} are high-utilisation and ${t.if_errors} have errors.`];
    case'routing':return[`OSPF is monitored on ${t.ospf_devices}/${t.ospf_fleet} devices. BGP, EIGRP and static routes are not monitored in this tenant.`];
    case'resilience':return[`The estimated fleet resilience tier is ${e.resilience?.tier}. The fleet is limited by its weakest critical node.`];
    default:return[];
  }
}

function scoreArticle(a:Article,q:string):number{
  const hay=(a.name+' '+a.short+' '+(a.synonyms||[]).join(' ')+' '+(a.what||'')+' '+(a.why||'')).toLowerCase();
  let s=0; for(const w of q.toLowerCase().split(/\s+/).filter(Boolean)){
    if(a.name.toLowerCase().includes(w))s+=5;
    if((a.synonyms||[]).some(x=>x.includes(w)))s+=4;
    if(hay.includes(w))s+=1;
  } return s;
}

export default function HelpCenter({token}:{token:string}){
  const[q,setQ]=useState('');
  const[sel,setSel]=useState<string>(()=>location.hash.startsWith('#help/')?location.hash.slice(6):'getting-started');
  const[explain,setExplain]=useState<any>(null);
  useEffect(()=>{fetch('/api/v1/help/explain',{headers:{Authorization:`Bearer ${token}`}}).then(r=>r.ok?r.json():null).then(setExplain).catch(()=>{})},[token]);
  useEffect(()=>{try{location.hash=sel.startsWith('__')?'':'help/'+sel}catch{}},[sel]);
  const open=(id:string)=>{setQ('');setSel(id);window.scrollTo({top:0,behavior:'smooth'})};

  const results=useMemo(()=>{
    if(q.trim().length<2)return null;
    const arts=ARTICLES.map(a=>({a,s:scoreArticle(a,q)})).filter(x=>x.s>0).sort((a,b)=>b.s-a.s).slice(0,12);
    const gloss=GLOSSARY.filter(g=>g.term.toLowerCase().includes(q.toLowerCase())||g.def.toLowerCase().includes(q.toLowerCase())).slice(0,4);
    const faqs=FAQ.filter(f=>f.q.toLowerCase().includes(q.toLowerCase())).slice(0,4);
    return{arts,gloss,faqs};
  },[q]);

  const article=ARTICLES.find(a=>a.id===sel);
  return <div className="ov"><div className="ov-exec"><div><div className="ov-eyebrow">MEDLINE CANADA</div><h1 className="ov-title">Help Center</h1></div></div>
    <div className="help-center">
      <aside className="help-side">
        <div className="help-search"><Search size={16}/><input value={q} onChange={e=>setQ(e.target.value)} placeholder="Search anything…" aria-label="Search help"/></div>
        <button className={'help-nav-item'+(sel===INDEX_ID?' active':'')} onClick={()=>open(INDEX_ID)}><ListOrdered size={13} style={{verticalAlign:-2,marginRight:6}}/>A–Z Index</button>
        <button className={'help-nav-item'+(sel===GLOSSARY_ID?' active':'')} onClick={()=>open(GLOSSARY_ID)}><BookOpen size={13} style={{verticalAlign:-2,marginRight:6}}/>Glossary</button>
        <button className={'help-nav-item'+(sel===FAQ_ID?' active':'')} onClick={()=>open(FAQ_ID)}><HelpCircle size={13} style={{verticalAlign:-2,marginRight:6}}/>FAQ</button>
        {CATEGORIES.map(cat=>{const items=ARTICLES.filter(a=>a.cat===cat);if(!items.length)return null;return <div key={cat}>
          <div className="help-cat">{cat}</div>
          <div className="help-nav">{items.map(a=><button key={a.id} className={'help-nav-item'+(sel===a.id?' active':'')} onClick={()=>open(a.id)}>{a.name}</button>)}</div>
        </div>;})}
      </aside>

      <main className="help-main">
        {results? <>
          <div className="help-eyebrow">Search results</div>
          <h1>“{q}”</h1>
          {results.arts.length===0&&results.gloss.length===0&&results.faqs.length===0&&<p className="help-lead">No matches. Try a simpler word like “availability”, “latency”, “resilience”, or “SLA”.</p>}
          {results.arts.map(({a})=><button key={a.id} className="help-result" onClick={()=>open(a.id)}><b>{a.name}</b><span className="path">{a.cat}</span><p>{a.short}</p></button>)}
          {results.gloss.map(g=><button key={g.term} className="help-result" onClick={()=>open(GLOSSARY_ID)}><b>{g.term}</b><span className="path">Glossary</span><p>{g.def}</p></button>)}
          {results.faqs.map(f=><button key={f.q} className="help-result" onClick={()=>f.link?open(f.link):open(FAQ_ID)}><b>{f.q}</b><span className="path">FAQ</span><p>{f.a}</p></button>)}
        </> : sel===GLOSSARY_ID? <>
          <div className="help-eyebrow">Reference</div><h1>Glossary</h1><p className="help-lead">Plain-English definitions of the terms used in the app.</p>
          <dl className="help-gloss">{GLOSSARY.map(g=><React.Fragment key={g.term}><dt>{g.term}</dt><dd>{g.def}</dd></React.Fragment>)}</dl>
        </> : sel===FAQ_ID? <>
          <div className="help-eyebrow">Reference</div><h1>Frequently asked questions</h1>
          <div className="help-faq">{FAQ.map(f=><details key={f.q}><summary>{f.q}</summary><p>{f.a}{f.link&&<> <button className="help-result" style={{display:'inline',border:0,background:'none',padding:0,color:'var(--ds-accent)',cursor:'pointer'}} onClick={()=>open(f.link!)}>Learn more →</button></>}</p></details>)}</div>
        </> : sel===INDEX_ID? <>
          <div className="help-eyebrow">Reference</div><h1>A–Z Index</h1><p className="help-lead">Browse every topic alphabetically.</p>
          <div className="help-nav">{[...ARTICLES].sort((a,b)=>a.name.localeCompare(b.name)).map(a=><button key={a.id} className="help-nav-item" onClick={()=>open(a.id)}>{a.name} <span className="path" style={{fontFamily:'var(--ds-mono)',fontSize:11,color:'var(--ds-ink-3)'}}>· {a.cat}</span></button>)}</div>
        </> : article? <>
          <div className="help-eyebrow">{article.cat}</div>
          <h1>{article.name}</h1>
          <p className="help-lead">{article.short}</p>
          {article.live&&explain&&<div className="help-why"><h3>Why this number?</h3><ul>{whyLines(article.live,explain).map((l,i)=><li key={i}>{l}</li>)}</ul></div>}
          <div className="help-sec"><h3>What is it?</h3><p>{article.what}</p></div>
          {article.why&&<div className="help-sec"><h3>Why do we show it?</h3><p>{article.why}</p></div>}
          {article.from&&<div className="help-sec"><h3>Where does the number come from?</h3><ul>{article.from.map((f,i)=><li key={i}>{f}</li>)}</ul></div>}
          {article.formula&&<div className="help-sec"><h3>Formula</h3><div className="help-mono">{article.formula}</div></div>}
          {article.example&&<div className="help-sec"><h3>Simple example</h3><p>{article.example}</p></div>}
          {article.good&&<div className="help-sec"><h3>What is good or bad?</h3><p>{article.good}</p></div>}
          {article.down&&<div className="help-sec"><h3>What makes it go down?</h3><p>{article.down}</p></div>}
          {article.up&&<div className="help-sec"><h3>What makes it go up?</h3><p>{article.up}</p></div>}
          {article.tech&&<details className="help-tech"><summary>Technical details</summary><p>{article.tech}</p></details>}
          {article.related&&article.related.length>0&&<div className="help-sec"><h3>Related</h3><div className="help-related">{article.related.map(r=>{const t=ARTICLES.find(a=>a.id===r);return t?<button key={r} onClick={()=>open(r)}>{t.name} <ArrowRight size={12} style={{verticalAlign:-1}}/></button>:null})}</div></div>}
        </> : <p className="help-lead">Pick a topic on the left, or search above.</p>}
      </main>
    </div>
  </div>;
}
