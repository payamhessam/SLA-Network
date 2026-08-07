/*
 * Paged.tsx — the application-wide list pagination primitive.
 *
 * Every list/table frame uses this so pagination looks and behaves identically:
 * a rows-per-page selector (10 / 20 / 50 / 100, default 10) plus prev/next and a
 * "from–to of total" range. `usePager` handles client-side slicing; `<Pager>` renders
 * the design-system control. Server-paginated lists can use PAGE_SIZES + <Pager> in
 * "controlled" mode by passing their own page/size state.
 */
import React,{useEffect,useMemo,useState} from 'react';
import{ChevronLeft,ChevronRight} from 'lucide-react';

export const PAGE_SIZES=[10,20,50,100];
export const DEFAULT_PAGE_SIZE=10;

export function usePager<T>(items:T[]){
  const[size,setSize]=useState(DEFAULT_PAGE_SIZE);
  const[page,setPage]=useState(1);
  const total=items.length;
  const pages=Math.max(1,Math.ceil(total/size));
  const cur=Math.min(page,pages);
  const slice=useMemo(()=>items.slice((cur-1)*size,(cur-1)*size+size),[items,cur,size]);
  useEffect(()=>{setPage(1)},[size,total]);
  return{slice,page:cur,pages,size,setSize,setPage,total};
}

type PagerProps={page:number;pages:number;size:number;total:number;setSize:(n:number)=>void;setPage:(n:number)=>void;label?:string};
export function Pager({page,pages,size,total,setSize,setPage,label='rows'}:PagerProps){
  const from=total===0?0:(page-1)*size+1;
  const to=Math.min(page*size,total);
  return <div className="ds-pager">
    <label className="ds-pager-size">Show
      <select value={size} onChange={e=>setSize(Number(e.target.value))} aria-label="Rows per page">{PAGE_SIZES.map(s=><option key={s} value={s}>{s}</option>)}</select>
      per page
    </label>
    <div className="ds-pager-nav">
      <span className="ds-pager-range">{from}–{to} of {total} {label}</span>
      <button type="button" className="ds-icon-btn sm" disabled={page<=1} onClick={()=>setPage(page-1)} aria-label="Previous page"><ChevronLeft size={16}/></button>
      <span className="ds-pager-page">{page} / {pages}</span>
      <button type="button" className="ds-icon-btn sm" disabled={page>=pages} onClick={()=>setPage(page+1)} aria-label="Next page"><ChevronRight size={16}/></button>
    </div>
  </div>;
}
