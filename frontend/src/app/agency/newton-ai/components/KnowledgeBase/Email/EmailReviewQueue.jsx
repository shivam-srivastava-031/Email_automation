"use client";
import React, { useContext, useEffect, useState } from 'react';
import { Box, Button, ButtonBase, Chip, CircularProgress, TextField, Tooltip, Typography } from '@mui/material';
import {
  RiAttachment2, RiChatQuoteLine, RiCheckDoubleLine, RiExternalLinkLine, RiInboxLine, RiLinkM, RiSendPlane2Line,
} from '@remixicon/react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiContext } from '@/components/ApiContext';
import SnackbarContext from '@/components/SnackbarContext';
import {
  approveEmailDecision, cancelEmailDecision, listEmailDecisions, skipEmailDecision, updateEmailDecisionDraft,
} from '@/apis';
import { Countdown, htmlToText, textToHtml, when } from '@/components/agentic/emailShared';
import {
  Avatar, C, Card, EmptyState, Label, StatusChip, failMsg, fieldSx, outlineBtnSx, primaryBtnSx, textBtnSx,
} from './ui';

/* Email > Review queue: every email this agent wrote, in one inbox.
 * The list filters by what needs doing; the right side shows why the agent
 * wrote it (the prospect's own words), the email, and Send / Edit / Skip. */

export const FILTERS = [
  { id: 'review', label: 'To review', status: 'awaiting_review,held' },
  { id: 'scheduled', label: 'Scheduled', status: 'scheduled,sending,evaluating' },
  { id: 'sent', label: 'Sent', status: 'sent' },
  { id: 'other', label: 'Skipped & failed', status: 'skipped,cancelled,blocked,failed' },
];

const countFor = (id, counts) => {
  if (!counts) return null;
  if (id === 'review') return (counts.awaiting_review || 0) + (counts.held || 0);
  if (id === 'scheduled') return counts.scheduled || 0;
  if (id === 'sent') return counts.sent || 0;
  return null;
};

const recordLink = (row) => (row.deal ? `/agency/deals/view/${row.deal}` : row.contact ? `/agency/contacts/view/${row.contact}` : null);

function FilterPill({ selected, label, count, onClick }) {
  return (
    <ButtonBase role="tab" aria-selected={selected} onClick={onClick} sx={{
      height: 34, px: 1.75, borderRadius: '999px', fontFamily: 'inherit', fontSize: '0.8rem',
      border: `1px solid ${selected ? C.blue : C.border}`, bgcolor: selected ? C.tint : '#fff',
      color: selected ? C.blueText : C.body, fontWeight: selected ? 600 : 500,
      '&.Mui-focusVisible': { outline: `2px solid ${C.blue}`, outlineOffset: 2 },
    }}>
      {label}{count !== null && count !== undefined ? ` · ${count}` : ''}
    </ButtonBase>
  );
}

function ListRow({ row, selected, onClick }) {
  const name = row.contact_name || row.recipient_email || 'Unknown prospect';
  return (
    <ButtonBase onClick={onClick} aria-pressed={selected} sx={{
      width: '100%', display: 'flex', alignItems: 'center', gap: 1.5, px: 2, py: 1.5, textAlign: 'left',
      fontFamily: 'inherit', borderBottom: `1px solid ${C.line}`,
      bgcolor: selected ? C.tint : '#fff', boxShadow: selected ? `inset 3px 0 0 ${C.blue}` : 'none',
      '&:hover': { bgcolor: selected ? C.tint : C.soft },
    }}>
      <Avatar name={name} />
      <Box sx={{ flex: 1, minWidth: 0 }}>
        <Typography sx={{ fontSize: '0.86rem', fontWeight: 600, color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{name}</Typography>
        <Typography sx={{ fontSize: '0.74rem', color: C.muted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {row.ask_summary || row.subject || 'No request recorded'} · {when(row.sent_at || row.created_at)}
        </Typography>
      </Box>
      <StatusChip status={row.status} />
    </ButtonBase>
  );
}

function Detail({ row, busy, onSend, onSkip, onCancel, onSaveDraft }) {
  const [editing, setEditing] = useState(false);
  const [to, setTo] = useState('');
  const [subject, setSubject] = useState('');
  const [body, setBody] = useState('');

  useEffect(() => { setEditing(false); }, [row.id]);

  const startEdit = () => {
    setTo(row.recipient_email || ''); setSubject(row.subject || ''); setBody(htmlToText(row.body_html)); setEditing(true);
  };
  const draft = () => ({ subject, body_html: textToHtml(body), ...(to ? { recipient_email: to } : {}) });

  const name = row.contact_name || row.recipient_email || 'Unknown prospect';
  const actionable = row.status === 'awaiting_review' || row.status === 'held';
  const scheduled = row.status === 'scheduled';
  const link = recordLink(row);
  const evidence = Array.isArray(row.evidence) ? row.evidence.filter(Boolean).slice(0, 3) : [];

  return (
    <Box>
      {/* Who + actions */}
      <Box sx={{ px: 2.75, py: 2.25, borderBottom: `1px solid ${C.border}`, display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap' }}>
        <Box sx={{ flex: '1 1 220px', minWidth: 0 }}>
          <Typography component="h3" sx={{ fontSize: '1.05rem', fontWeight: 600, color: C.text }}>{name}</Typography>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mt: 0.5, flexWrap: 'wrap' }}>
            <StatusChip status={row.status} long />
            {row.opened?.is_opened && (
              <Tooltip title={`Opened ${row.opened.open_count} time(s), first ${when(row.opened.opened_at)}`}>
                <Chip size="small" icon={<RiCheckDoubleLine size={13} />} label="Opened" sx={{ bgcolor: '#DCFCE7', color: '#166534', height: 22, fontSize: '0.7rem' }} />
              </Tooltip>
            )}
            {link && (
              <Button size="small" href={link} endIcon={<RiExternalLinkLine size={13} />} sx={{ textTransform: 'none', fontSize: '0.76rem', p: 0, minWidth: 0 }}>
                {row.deal ? 'Open deal' : 'Open contact'}
              </Button>
            )}
          </Box>
        </Box>

        {actionable && !editing && (
          <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
            <Button onClick={() => onSkip(row)} disabled={busy} sx={textBtnSx}>Skip</Button>
            <Button variant="outlined" onClick={startEdit} disabled={busy} sx={outlineBtnSx}>Edit</Button>
            <Button variant="contained" onClick={() => onSend(row)} disabled={busy || !row.recipient_email}
              startIcon={<RiSendPlane2Line size={15} />} sx={{ ...primaryBtnSx, px: 2.25 }}>Send now</Button>
          </Box>
        )}
        {scheduled && !editing && (
          <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
            <Typography sx={{ fontSize: '0.82rem', color: '#0369A1', fontWeight: 500 }}>
              This email <Countdown until={row.cancel_deadline_at} />
            </Typography>
            <Button variant="outlined" color="error" onClick={() => onCancel(row)} disabled={busy}
              sx={{ textTransform: 'none', borderRadius: '8px', fontWeight: 600 }}>Cancel send</Button>
            <Button variant="contained" onClick={() => onSend(row)} disabled={busy} sx={primaryBtnSx}>Send now</Button>
          </Box>
        )}
        {row.status === 'sent' && (
          <Typography sx={{ fontSize: '0.82rem', color: '#166534', fontWeight: 500 }}>Sent {when(row.sent_at)}</Typography>
        )}
      </Box>

      {/* Why: the prospect's own words */}
      {(evidence.length > 0 || row.ask_summary || row.hold_reason || row.error) && (
        <Box sx={{ px: 2.75, py: 2, borderBottom: `1px solid ${C.line}`, display: 'flex', flexDirection: 'column', gap: 1.25 }}>
          <Typography sx={{ fontSize: '0.76rem', fontWeight: 600, color: C.body }}>Why the agent wrote this</Typography>
          {row.ask_summary && (
            <Typography sx={{ fontSize: '0.86rem', color: C.text }}>They asked for: <b>{row.ask_summary}</b></Typography>
          )}
          {evidence.map((q, i) => (
            <Box key={i} sx={{ display: 'flex', gap: 1.25, alignItems: 'flex-start', p: 1.5, bgcolor: C.soft, borderRadius: '10px' }}>
              <RiChatQuoteLine size={17} style={{ color: '#94A3B8', flexShrink: 0, marginTop: 2 }} />
              <Typography sx={{ fontSize: '0.86rem', color: C.text, fontStyle: 'italic', lineHeight: 1.5 }}>“{q}”</Typography>
            </Box>
          ))}
          {row.hold_reason && (
            <Typography sx={{ fontSize: '0.82rem', color: '#991B1B', bgcolor: '#FEF2F2', px: 1.75, py: 1.25, borderRadius: '10px' }}>{row.hold_reason}</Typography>
          )}
          {row.error && (
            <Typography sx={{ fontSize: '0.82rem', color: '#991B1B' }}>{row.error}</Typography>
          )}
        </Box>
      )}

      {/* The email */}
      <Box sx={{ px: 2.75, py: 2.25, display: 'flex', flexDirection: 'column', gap: 1.75 }}>
        {editing ? (
          <>
            <Box>
              <Label>To</Label>
              <TextField fullWidth size="small" type="email" value={to} onChange={(e) => setTo(e.target.value)} sx={fieldSx} />
            </Box>
            <Box>
              <Label>Subject</Label>
              <TextField fullWidth size="small" value={subject} onChange={(e) => setSubject(e.target.value)} sx={fieldSx} />
            </Box>
            <Box>
              <Label>Message</Label>
              <TextField fullWidth multiline minRows={8} maxRows={18} size="small" value={body} onChange={(e) => setBody(e.target.value)} sx={fieldSx}
                helperText="Attachments, the button and your signature are kept and added when it sends." />
            </Box>
            <Box sx={{ display: 'flex', gap: 1, justifyContent: 'flex-end', flexWrap: 'wrap' }}>
              <Button onClick={() => setEditing(false)} disabled={busy} sx={textBtnSx}>Cancel</Button>
              <Button variant="outlined" onClick={() => onSaveDraft(row, draft(), () => setEditing(false))} disabled={busy} sx={outlineBtnSx}>Save draft</Button>
              <Button variant="contained" onClick={() => onSend(row, draft())} disabled={busy || !to || !subject.trim() || !body.trim()}
                startIcon={<RiSendPlane2Line size={15} />} sx={primaryBtnSx}>Send now</Button>
            </Box>
          </>
        ) : (
          <>
            <Box sx={{ display: 'flex', gap: 2.5, flexWrap: 'wrap', fontSize: '0.8rem', color: C.muted }}>
              <span>To <Box component="b" sx={{ color: C.text, fontWeight: 500 }}>{row.recipient_email || 'no address yet'}</Box></span>
              {row.from_email && <span>From <Box component="b" sx={{ color: C.text, fontWeight: 500 }}>{row.from_email}</Box></span>}
            </Box>
            {(row.subject || row.body_html) ? (
              <Box sx={{ border: `1px solid ${C.border}`, borderRadius: '10px', overflow: 'hidden' }}>
                <Typography sx={{ px: 2, py: 1.5, bgcolor: C.soft, borderBottom: `1px solid ${C.border}`, fontSize: '0.92rem', fontWeight: 600, color: C.text }}>
                  {row.subject || '(no subject)'}
                </Typography>
                <Box sx={{ px: 2.5, py: 2, fontSize: '0.875rem', lineHeight: 1.75, color: '#1E293B', wordBreak: 'break-word', '& p': { m: 0, mb: 1.5 }, '& a': { color: C.blue } }}
                  dangerouslySetInnerHTML={{ __html: row.body_html || '' }} />
                {Array.isArray(row.assets) && row.assets.length > 0 && (
                  <Box sx={{ px: 2, py: 1.25, borderTop: `1px solid ${C.border}`, display: 'flex', gap: 0.75, flexWrap: 'wrap' }}>
                    {row.assets.map((a) => (
                      <Chip key={a.id} size="small" icon={a.kind === 'link' ? <RiLinkM size={12} /> : <RiAttachment2 size={12} />}
                        label={a.name} sx={{ height: 24, bgcolor: C.tint, color: C.blueText, fontSize: '0.72rem' }} />
                    ))}
                  </Box>
                )}
              </Box>
            ) : (
              <Typography sx={{ fontSize: '0.84rem', color: C.muted }}>No email was written for this one.</Typography>
            )}
          </>
        )}
      </Box>
    </Box>
  );
}

export default function EmailReviewQueue({ agentId, counts, focus, onChanged }) {
  const api = useContext(ApiContext);
  const { showSnackbar } = useContext(SnackbarContext) || {};
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState(focus?.filter || 'review');
  const [selectedId, setSelectedId] = useState(focus?.id ?? null);

  // A click on the Overview ("Review" on a row, or a number) lands here.
  useEffect(() => {
    if (!focus) return;
    if (focus.filter) setFilter(focus.filter);
    setSelectedId(focus.id ?? null);
  }, [focus]);

  const active = FILTERS.find((f) => f.id === filter) || FILTERS[0];
  const key = ['email-queue', agentId, active.id];
  const { data: rows = [], isLoading } = useQuery({
    queryKey: key,
    queryFn: () => listEmailDecisions(api, { agent: agentId, status: active.status }),
    enabled: Boolean(api?.axiosInstance && agentId),
    staleTime: 10_000,
    refetchInterval: 15_000,
  });

  // Keep a valid selection: the focused row if present, else the first one.
  useEffect(() => {
    if (isLoading) return;
    if (!rows.some((r) => r.id === selectedId)) setSelectedId(rows[0]?.id ?? null);
  }, [rows, isLoading, selectedId]);

  const selected = rows.find((r) => r.id === selectedId) || null;

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['email-queue', agentId] });
    queryClient.invalidateQueries({ queryKey: ['email-decisions'] }); // deal-page panels
    onChanged?.();
  };
  const fail = (e) => showSnackbar?.({ message: failMsg(e, 'Action failed'), severity: 'error' });
  const firstName = (row) => (row.contact_name || 'the prospect').split(' ')[0];

  const approve = useMutation({
    mutationFn: ({ row, payload }) => approveEmailDecision(api, row.id, payload || {}),
    onSuccess: (_, { row }) => { refresh(); showSnackbar?.({ message: `Email sent to ${firstName(row)}`, severity: 'success' }); },
    onError: fail,
  });
  const skip = useMutation({
    mutationFn: (row) => skipEmailDecision(api, row.id, 'skipped by rep'),
    onSuccess: (_, row) => { refresh(); showSnackbar?.({ message: `Skipped. No email goes to ${firstName(row)}.`, severity: 'success' }); },
    onError: fail,
  });
  const cancel = useMutation({
    mutationFn: (row) => cancelEmailDecision(api, row.id, 'cancelled by rep'),
    onSuccess: () => { refresh(); showSnackbar?.({ message: 'Send cancelled', severity: 'success' }); },
    onError: fail,
  });
  const update = useMutation({
    mutationFn: ({ row, payload }) => updateEmailDecisionDraft(api, row.id, payload),
    onSuccess: () => { refresh(); showSnackbar?.({ message: 'Draft saved', severity: 'success' }); },
    onError: fail,
  });
  const busy = approve.isPending || skip.isPending || cancel.isPending || update.isPending;

  const empty = {
    review: ['All caught up', 'New drafts show up here when prospects ask for something.'],
    scheduled: ['Nothing scheduled', 'In automatic mode, emails wait here briefly so you can cancel.'],
    sent: ['Nothing sent yet', 'Emails the agent sends are listed here.'],
    other: ['Nothing here', 'Skipped, cancelled, blocked and failed emails are listed here.'],
  }[active.id];

  return (
    <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 2, alignItems: 'flex-start' }}>
      {/* ── List ── */}
      <Card sx={{ flex: '1 1 300px', minWidth: 0, overflow: 'hidden' }}>
        <Box role="tablist" aria-label="Filter emails" sx={{ display: 'flex', gap: 0.75, p: 1.5, borderBottom: `1px solid ${C.border}`, flexWrap: 'wrap' }}>
          {FILTERS.map((f) => (
            <FilterPill key={f.id} selected={f.id === active.id} label={f.label} count={countFor(f.id, counts)}
              onClick={() => { setFilter(f.id); setSelectedId(null); }} />
          ))}
        </Box>
        {isLoading ? (
          <Box sx={{ display: 'flex', justifyContent: 'center', py: 5 }}><CircularProgress size={22} /></Box>
        ) : rows.length === 0 ? (
          <EmptyState icon={RiInboxLine} title={empty[0]} detail={empty[1]} />
        ) : (
          <Box sx={{ maxHeight: 'calc(100vh - 260px)', overflowY: 'auto' }}>
            {rows.map((r) => (
              <ListRow key={r.id} row={r} selected={r.id === selectedId} onClick={() => setSelectedId(r.id)} />
            ))}
          </Box>
        )}
      </Card>

      {/* ── Selected email ── */}
      <Card sx={{ flex: '999 1 520px', minWidth: 0 }}>
        {selected ? (
          <Detail
            row={selected}
            busy={busy}
            onSend={(row, payload) => approve.mutate({ row, payload })}
            onSkip={(row) => skip.mutate(row)}
            onCancel={(row) => cancel.mutate(row)}
            onSaveDraft={(row, payload, done) => update.mutate({ row, payload }, { onSuccess: done })}
          />
        ) : (
          <Box sx={{ py: 8, px: 3, textAlign: 'center' }}>
            <Typography sx={{ fontSize: '0.88rem', color: C.muted }}>
              {rows.length ? 'Pick an email on the left.' : 'When there is an email here, you can read and send it on this side.'}
            </Typography>
          </Box>
        )}
      </Card>
    </Box>
  );
}
