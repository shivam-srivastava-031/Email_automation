"use client";
import React from 'react';
import { Box, ButtonBase, Chip, CircularProgress, Switch, Typography } from '@mui/material';
import { STATUS_META } from '@/components/agentic/emailShared';

/* Building blocks shared by the three Email tabs (Overview, Review queue,
 * Setup). One look everywhere: white 12px cards on slate, blue for action. */

export const C = {
  text: '#0F172A',
  body: '#475569',
  muted: '#64748B',
  border: '#E2E8F0',
  line: '#F1F5F9',
  soft: '#F8FAFC',
  blue: '#2563eb',
  blueText: '#1D4ED8',
  tint: '#EFF6FF',
};

export const fieldSx = {
  '& .MuiOutlinedInput-root': {
    borderRadius: '8px', backgroundColor: '#fff', fontSize: '0.875rem',
    '& fieldset': { borderColor: C.border },
    '&:hover fieldset': { borderColor: '#CBD5E1' },
  },
};
export const readOnlySx = {
  '& .MuiOutlinedInput-root': { ...fieldSx['& .MuiOutlinedInput-root'], backgroundColor: C.soft, color: C.body },
};

export const primaryBtnSx = {
  textTransform: 'none', bgcolor: C.blue, borderRadius: '8px', fontWeight: 600, boxShadow: 'none',
  '&:hover': { bgcolor: '#1D4ED8', boxShadow: 'none' },
};
export const outlineBtnSx = {
  textTransform: 'none', borderRadius: '8px', fontWeight: 600, color: C.text, borderColor: '#CBD5E1',
  '&:hover': { borderColor: '#94A3B8', bgcolor: C.soft },
};
export const textBtnSx = { textTransform: 'none', borderRadius: '8px', fontWeight: 500, color: C.body };

export const failMsg = (e, fallback) => {
  const d = e?.response?.data;
  return d?.detail || d?.error || (d && typeof d === 'object' ? Object.values(d).flat().join(' ') : '') || e?.message || fallback;
};

export const formatBytes = (n) => (!n ? '' : n < 1024 * 1024 ? `${Math.round(n / 1024)} KB` : `${(n / (1024 * 1024)).toFixed(1)} MB`);

export const initialsOf = (name) => String(name || '?').trim().split(/\s+/).slice(0, 2).map((p) => p[0]).join('').toUpperCase() || '?';

export function Card({ children, sx }) {
  return (
    <Box sx={{ bgcolor: '#fff', border: `1px solid ${C.border}`, borderRadius: '12px', boxSizing: 'border-box', ...sx }}>
      {children}
    </Box>
  );
}

/* Card title row: a heading, an optional one-line hint and a right-side action. */
export function CardTitle({ title, hint, action, sx }) {
  return (
    <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1.5, ...sx }}>
      <Box sx={{ flex: 1, minWidth: 0 }}>
        <Typography component="h3" sx={{ fontWeight: 600, fontSize: '0.95rem', color: C.text }}>{title}</Typography>
        {hint && <Typography sx={{ fontSize: '0.8rem', color: C.muted, mt: 0.25 }}>{hint}</Typography>}
      </Box>
      {action}
    </Box>
  );
}

export function Label({ children }) {
  return <Typography sx={{ fontSize: '0.78rem', fontWeight: 600, color: C.body, mb: 0.5 }}>{children}</Typography>;
}

/* A labelled on/off row: the one control used for every sharing choice. */
export function SwitchRow({ title, detail, checked, disabled, busy, onChange }) {
  return (
    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, p: 1.5, borderRadius: '10px', border: `1px solid ${C.border}`, bgcolor: disabled ? C.soft : '#fff' }}>
      <Box sx={{ flex: 1, minWidth: 0 }}>
        <Typography sx={{ fontSize: '0.86rem', fontWeight: 600, color: disabled ? '#94A3B8' : C.text }}>{title}</Typography>
        {detail && <Typography sx={{ fontSize: '0.76rem', color: C.muted, mt: 0.25 }}>{detail}</Typography>}
      </Box>
      {busy ? <CircularProgress size={16} /> : (
        <Switch checked={Boolean(checked)} disabled={disabled} onChange={(e) => onChange(e.target.checked)}
          inputProps={{ 'aria-label': title }} />
      )}
    </Box>
  );
}

/* A big selectable card: used where the choice deserves a sentence. */
export function OptionCard({ selected, disabled, onClick, icon: Icon, title, detail }) {
  return (
    <ButtonBase
      role="radio" aria-checked={Boolean(selected)} disabled={disabled} onClick={onClick}
      sx={{
        flex: '1 1 220px', minWidth: 0, display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 0.5,
        textAlign: 'left', p: 1.75, borderRadius: '10px', fontFamily: 'inherit',
        border: `2px solid ${selected ? C.blue : C.border}`, bgcolor: selected ? C.tint : '#fff',
        opacity: disabled && !selected ? 0.6 : 1,
        '&:hover': disabled ? {} : { borderColor: selected ? C.blue : '#CBD5E1' },
        '&.Mui-focusVisible': { outline: `2px solid ${C.blue}`, outlineOffset: 2 },
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
        {Icon && <Icon size={15} style={{ color: selected ? C.blueText : C.muted }} />}
        <Typography sx={{ fontSize: '0.88rem', fontWeight: 600, color: C.text }}>{title}</Typography>
      </Box>
      <Typography sx={{ fontSize: '0.8rem', color: C.body, lineHeight: 1.5 }}>{detail}</Typography>
    </ButtonBase>
  );
}

export function StatusChip({ status, long = false }) {
  const meta = STATUS_META[status] || STATUS_META.evaluating;
  return (
    <Chip size="small" label={long ? meta.label : meta.short}
      sx={{ bgcolor: meta.bg, color: meta.fg, fontWeight: 600, height: 22, fontSize: '0.7rem' }} />
  );
}

export function Avatar({ name }) {
  return (
    <Box aria-hidden sx={{
      width: 36, height: 36, borderRadius: '50%', bgcolor: C.tint, color: C.blueText, flexShrink: 0,
      fontSize: '0.8rem', fontWeight: 600, display: 'flex', alignItems: 'center', justifyContent: 'center',
    }}>
      {initialsOf(name)}
    </Box>
  );
}

export function EmptyState({ icon: Icon, title, detail }) {
  return (
    <Box sx={{ textAlign: 'center', py: 5, px: 2 }}>
      {Icon && <Icon size={26} style={{ color: '#94A3B8' }} />}
      <Typography sx={{ fontSize: '0.9rem', fontWeight: 600, color: C.text, mt: 1 }}>{title}</Typography>
      {detail && <Typography sx={{ fontSize: '0.8rem', color: C.muted, mt: 0.5 }}>{detail}</Typography>}
    </Box>
  );
}
