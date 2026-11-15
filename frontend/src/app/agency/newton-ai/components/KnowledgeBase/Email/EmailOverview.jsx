"use client";
import React, { useContext, useEffect, useState } from 'react';
import {
  Alert, Box, Button, ButtonBase, CircularProgress, LinearProgress, ToggleButton, ToggleButtonGroup, Typography,
} from '@mui/material';
import { RiCheckboxCircleLine, RiErrorWarningLine, RiInboxLine } from '@remixicon/react';
import { ApiContext } from '@/components/ApiContext';
import SnackbarContext from '@/components/SnackbarContext';
import {
  getAgentEmailSettings, getTeamEmailSettings, listEmailAssets, listEmailDecisions, patchAgentEmailSettings,
} from '@/apis';
import { Avatar, C, Card, CardTitle, EmptyState, StatusChip, failMsg, primaryBtnSx } from './ui';

/* Email > Overview: is it on, what needs me, and what is set up - at a glance.
 * The sending mode lives here (and only here) and saves on click. */

const MODES = [
  { id: 'off', label: 'Off' },
  { id: 'review', label: 'Review first' },
  { id: 'auto', label: 'Automatic' },
];

const STATUS = {
  off: { label: 'Paused', bg: '#F1F5F9', fg: '#475569', help: 'Nothing is emailed. The agent still asks for email addresses on LinkedIn.' },
  review: { label: 'On · Review first', bg: '#FEF3C7', fg: '#92400E', help: 'The agent drafts each email and waits for you to press Send.' },
  auto: { label: 'On · Automatic', bg: '#DCFCE7', fg: '#166534', help: 'Emails send on their own, with a short window to cancel each one.' },
};

const segmentSx = {
  display: 'flex', p: 0.5, gap: 0.5, bgcolor: '#F1F5F9', borderRadius: '10px',
  '& .MuiToggleButtonGroup-grouped': {
    flex: 1, border: 0, borderRadius: '7px !important', textTransform: 'none', fontSize: '0.84rem',
    fontWeight: 500, color: C.body, py: 1, mx: 0,
  },
  '& .MuiToggleButton-root.Mui-selected': {
    bgcolor: '#fff', color: C.blueText, fontWeight: 600, boxShadow: '0 1px 2px rgba(15,23,42,.12)',
    '&:hover': { bgcolor: '#fff' },
  },
};

function Stat({ label, value, detail, tone, onClick }) {
  return (
    <ButtonBase onClick={onClick} sx={{
      display: 'block', textAlign: 'left', bgcolor: '#fff', border: `1px solid ${C.border}`, borderRadius: '12px',
      p: 2, fontFamily: 'inherit', '&:hover': { borderColor: '#CBD5E1' },
      '&.Mui-focusVisible': { outline: `2px solid ${C.blue}`, outlineOffset: 2 },
    }}>
      <Typography sx={{ fontSize: '0.8rem', color: C.muted }}>{label}</Typography>
      <Typography sx={{ fontSize: '1.6rem', fontWeight: 600, mt: 0.25, color: tone || C.text }}>{value}</Typography>
      <Typography sx={{ fontSize: '0.74rem', color: C.blueText, mt: 0.25 }}>{detail}</Typography>
    </ButtonBase>
  );
}

function GlanceRow({ ok, label, value }) {
  const Icon = ok ? RiCheckboxCircleLine : RiErrorWarningLine;
  return (
    <Box component="li" sx={{ display: 'flex', gap: 1.25, alignItems: 'flex-start', py: 1 }}>
      <Icon size={18} style={{ color: ok ? '#16A34A' : '#D97706', flexShrink: 0, marginTop: 2 }} />
      <Box sx={{ minWidth: 0 }}>
        <Typography sx={{ fontSize: '0.76rem', color: C.muted }}>{label}</Typography>
        <Typography sx={{ fontSize: '0.86rem', fontWeight: 500, color: C.text, overflowWrap: 'anywhere' }}>{value}</Typography>
      </Box>
    </Box>
  );
}

export default function EmailOverview({ agentId, version, counts, onOpenReview, onOpenSetup, onChanged }) {
  const api = useContext(ApiContext);
  const { showSnackbar } = useContext(SnackbarContext) || {};
  const axiosInstance = api?.axiosInstance;

  const [settings, setSettings] = useState(null);
  const [team, setTeam] = useState(null);
  const [assets, setAssets] = useState([]);
  const [attention, setAttention] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [savingMode, setSavingMode] = useState('');

  // Depends on `axiosInstance`, never on `api` (a new object every render).
  useEffect(() => {
    if (!axiosInstance || !agentId) return undefined;
    let cancelled = false;
    setLoading(true); setError('');
    (async () => {
      try {
        const [s, t, open] = await Promise.all([
          getAgentEmailSettings(api, agentId),
          getTeamEmailSettings(api, agentId).catch(() => null),
          listEmailDecisions(api, { agent: agentId, status: 'awaiting_review,held' }).catch(() => []),
        ]);
        const onTeamContent = Boolean(s?.readiness?.uses_team_content);
        const a = await listEmailAssets(api, agentId, { team: onTeamContent }).catch(() => []);
        if (cancelled) return;
        setSettings(s); setTeam(t); setAttention(open); setAssets(a);
      } catch (e) {
        if (!cancelled) setError(failMsg(e, 'Could not load the email settings'));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [axiosInstance, agentId, version]);

  const rd = settings?.readiness;
  const mode = settings?.mode || 'off';
  const status = STATUS[mode] || STATUS.off;
  const ready = Boolean(rd?.ready);

  const setMode = async (next) => {
    if (!next || next === mode) return;
    setSavingMode(next);
    try {
      const s = await patchAgentEmailSettings(api, agentId, { mode: next });
      setSettings(s);
      onChanged?.();
      showSnackbar?.({ message: next === 'off' ? 'Auto email paused' : `Auto email is on: ${STATUS[next].label.replace('On · ', '').toLowerCase()}`, severity: 'success' });
    } catch (e) {
      const d = e?.response?.data;
      if (d?.readiness) setSettings((prev) => ({ ...prev, readiness: d.readiness }));
      showSnackbar?.({ message: failMsg(e, 'Could not change the mode'), severity: 'error' });
    } finally { setSavingMode(''); }
  };

  // What is actually sent: the team's content when this agent uses it.
  const content = rd?.uses_team_content && team ? team : settings;
  const activeFiles = assets.filter((a) => a.is_active).length;
  const toReview = counts?.awaiting_review ?? 0;
  const held = counts?.held ?? 0;

  if (!settings && loading) return <LinearProgress sx={{ borderRadius: 4 }} />;
  if (error && !settings) return <Alert severity="error" sx={{ borderRadius: '10px' }}>{error}</Alert>;
  if (!settings) return null;

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      {/* ── Status + the one decision that matters ── */}
      <Card sx={{ p: 3, display: 'flex', flexWrap: 'wrap', gap: 3, alignItems: 'center' }}>
        <Box sx={{ flex: '999 1 380px', minWidth: 0 }}>
          <Box component="span" sx={{
            display: 'inline-flex', alignItems: 'center', gap: 0.75, fontSize: '0.75rem', fontWeight: 600,
            px: 1.25, py: 0.5, borderRadius: '999px', bgcolor: status.bg, color: status.fg,
          }}>
            <Box component="span" sx={{ width: 7, height: 7, borderRadius: '50%', bgcolor: status.fg }} />
            {status.label}
          </Box>
          <Typography component="h2" sx={{ mt: 1.5, mb: 0.75, fontSize: '1.3rem', fontWeight: 600, color: C.text, lineHeight: 1.3 }}>
            Your agent emails prospects who ask for it
          </Typography>
          <Typography sx={{ fontSize: '0.86rem', lineHeight: 1.6, color: C.body, maxWidth: 560 }}>
            When someone shares their email in a LinkedIn chat, the agent writes a reply that picks up the conversation,
            adds your files and a booking button, and sends it from your mailbox.
          </Typography>
        </Box>
        <Box sx={{ flex: '1 1 320px', minWidth: 0, display: 'flex', flexDirection: 'column', gap: 1.25 }}>
          <Typography id="email-mode-label" sx={{ fontSize: '0.78rem', fontWeight: 600, color: C.body }}>
            How should emails go out?
          </Typography>
          <ToggleButtonGroup exclusive value={mode} onChange={(_, v) => setMode(v)} aria-labelledby="email-mode-label"
            disabled={Boolean(savingMode)} sx={segmentSx}>
            {MODES.map((m) => (
              <ToggleButton key={m.id} value={m.id} disabled={m.id !== 'off' && !ready && mode !== m.id}>
                {savingMode === m.id ? <CircularProgress size={14} /> : m.label}
              </ToggleButton>
            ))}
          </ToggleButtonGroup>
          <Typography sx={{ fontSize: '0.8rem', color: C.body, lineHeight: 1.5 }}>{status.help}</Typography>
          {!ready && (
            <Alert severity="warning" sx={{ borderRadius: '10px', alignItems: 'center' }}
              action={<Button size="small" href={rd?.connect_url || '/agency/integration'} sx={{ textTransform: 'none', fontWeight: 600 }}>Connect email</Button>}>
              Connect an email to turn this on.
            </Alert>
          )}
        </Box>
      </Card>

      {/* ── Numbers: each opens the matching list ── */}
      <Box component="section" aria-label="Email queue" sx={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))', gap: 2 }}>
        <Stat label="To review" value={toReview} tone={toReview ? '#92400E' : undefined} detail="Open queue →" onClick={() => onOpenReview({ filter: 'review' })} />
        <Stat label="Needs you" value={held} tone={held ? '#991B1B' : undefined} detail="See why →" onClick={() => onOpenReview({ filter: 'review' })} />
        <Stat label="Scheduled" value={counts?.scheduled ?? 0} detail="Cancel if needed →" onClick={() => onOpenReview({ filter: 'scheduled' })} />
        <Stat label="Sent" value={counts?.sent ?? 0} detail="See history →" onClick={() => onOpenReview({ filter: 'sent' })} />
      </Box>

      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 2, alignItems: 'flex-start' }}>
        {/* ── Needs your attention ── */}
        <Card sx={{ flex: '999 1 520px', minWidth: 0, overflow: 'hidden' }}>
          <CardTitle
            title="Needs your attention"
            sx={{ px: 2.5, py: 2, borderBottom: `1px solid ${C.border}`, alignItems: 'center' }}
            action={attention.length > 0 && (
              <Button size="small" onClick={() => onOpenReview({ filter: 'review' })} sx={{ textTransform: 'none', fontWeight: 600 }}>See all</Button>
            )}
          />
          {attention.length === 0 ? (
            <EmptyState icon={RiInboxLine} title="Nothing waiting"
              detail={mode === 'off'
                ? 'Turn auto email on and drafts will appear here when prospects ask.'
                : 'New drafts appear here when a prospect asks for something on LinkedIn.'} />
          ) : attention.slice(0, 4).map((row) => (
            <Box key={row.id} sx={{ px: 2.5, py: 1.75, borderBottom: `1px solid ${C.line}`, display: 'flex', alignItems: 'center', gap: 1.75, flexWrap: 'wrap' }}>
              <Avatar name={row.contact_name || row.recipient_email} />
              <Box sx={{ flex: '1 1 200px', minWidth: 0 }}>
                <Typography sx={{ fontSize: '0.88rem', fontWeight: 600, color: C.text }}>
                  {row.contact_name || row.recipient_email || 'Unknown prospect'}
                </Typography>
                <Typography sx={{ fontSize: '0.8rem', color: C.body, mt: 0.25, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {row.status === 'held' && row.hold_reason ? row.hold_reason : `Asked for: ${row.ask_summary || 'material'}`}
                </Typography>
              </Box>
              <StatusChip status={row.status} />
              <Button size="small" variant="contained" onClick={() => onOpenReview({ id: row.id, filter: 'review' })} sx={{ ...primaryBtnSx, px: 2 }}>
                Review
              </Button>
            </Box>
          ))}
          <Typography sx={{ px: 2.5, py: 1.5, fontSize: '0.74rem', color: C.muted }}>Drafts also show on each deal page under Emails.</Typography>
        </Card>

        {/* ── Setup at a glance ── */}
        <Card sx={{ flex: '1 1 300px', minWidth: 0 }}>
          <CardTitle
            title="Setup"
            sx={{ px: 2.5, py: 2, borderBottom: `1px solid ${C.border}`, alignItems: 'center' }}
            action={<Button size="small" onClick={onOpenSetup} sx={{ textTransform: 'none', fontWeight: 600 }}>Edit</Button>}
          />
          <Box component="ul" sx={{ listStyle: 'none', m: 0, px: 2.5, py: 1 }}>
            <GlanceRow ok={ready} label="Sending from"
              value={ready ? `${rd.from_email}${rd.shared ? ' (shared team email)' : ''}` : 'No email connected'} />
            <GlanceRow ok label="Email"
              value={`${content?.body_mode === 'fixed' ? 'Your template' : 'AI writes each one'}${rd?.uses_team_content ? ' · team setup' : ''}`} />
            <GlanceRow ok label="Files"
              value={activeFiles ? `${activeFiles} ${activeFiles === 1 ? 'file' : 'files'} sent with every email` : 'No files attached'} />
            <GlanceRow ok label="Button"
              value={content?.include_booking_link === false ? 'Off' : (content?.cta_text || 'Book a call')} />
            <GlanceRow ok label="Daily limit" value={`${settings.daily_cap} emails a day`} />
          </Box>
        </Card>
      </Box>

      {/* ── How it works ── */}
      <Box component="section" aria-label="How it works" sx={{ display: 'flex', flexWrap: 'wrap', gap: 2 }}>
        {[
          ['Prospect shares their email', 'in your LinkedIn chat.'],
          ['The agent drafts a reply', 'with your files and booking button.'],
          mode === 'auto'
            ? ['It sends on its own', 'after a short window in which you can cancel.']
            : ['You approve it', 'from the review queue or the deal page.'],
        ].map(([bold, rest], i) => (
          <Box key={bold} sx={{ flex: '1 1 220px', minWidth: 0, p: 2, borderRadius: '12px', border: '1px dashed #CBD5E1', display: 'flex', gap: 1.5 }}>
            <Box sx={{
              width: 26, height: 26, borderRadius: '50%', bgcolor: C.tint, color: C.blueText, flexShrink: 0,
              fontSize: '0.75rem', fontWeight: 700, display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}>{i + 1}</Box>
            <Typography sx={{ fontSize: '0.82rem', lineHeight: 1.5, color: C.body }}>
              <Box component="b" sx={{ color: C.text }}>{bold}</Box> {rest}
            </Typography>
          </Box>
        ))}
      </Box>
    </Box>
  );
}
