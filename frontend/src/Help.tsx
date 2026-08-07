import React from 'react';
import {Info} from 'lucide-react';

// Small "ⓘ" affordance placed next to a section title. Hovering (or focusing for
// keyboard users) reveals a short plain-language explanation of what the section shows.
export default function Help({text}:{text:string}){
  return <span className="help" tabIndex={0} role="note" aria-label={text}>
    <Info size={14}/>
    <span className="help-tip">{text}</span>
  </span>;
}
