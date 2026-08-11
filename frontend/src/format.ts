// Single shared percentage formatter — every page must use this so the same underlying
// value always displays the same way. Rounds to 2 decimal places for meaningful precision,
// then trims trailing zeros (100.000 -> "100%", 99.923 -> "99.92%", 99.900 -> "99.9%").
// Calculations elsewhere must keep full precision; this is presentation-only rounding.
export function fmtPct(v: number | null | undefined, fallback: string = '—'): string {
  if (v == null || typeof v !== 'number' || Number.isNaN(v)) return fallback;
  const s = v.toFixed(2).replace(/\.?0+$/, '');
  return (s === '' || s === '-' ? '0' : s) + '%';
}

// Same rounding for a signed delta (+0.04%, -1.2%) used in trend/WoW/MoM indicators.
export function fmtPctDelta(v: number | null | undefined, fallback: string = '—'): string {
  if (v == null || typeof v !== 'number' || Number.isNaN(v)) return fallback;
  return (v >= 0 ? '+' : '') + fmtPct(v);
}
