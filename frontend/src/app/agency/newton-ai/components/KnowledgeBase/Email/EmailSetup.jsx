"use client";
import React, { useCallback, useContext, useEffect, useRef, useState } from 'react';
import {
  Alert, Box, Button, Chip, CircularProgress, IconButton, LinearProgress, Switch, TextField, Tooltip, Typography,
} from '@mui/material';
import {
  RiAddLine, RiAttachment2, RiCheckboxCircleLine, RiDeleteBinLine, RiErrorWarningLine, RiEyeLine,
  RiFileCopyLine, RiFileTextLine, RiLinkM, RiSparklingLine, RiSubtractLine, RiTeamLine, RiUploadCloud2Line,
} from '@remixicon/react';
import { ApiContext } from '@/components/ApiContext';
import SnackbarContext from '@/components/SnackbarContext';
import {
  copyTeamEmailContent, createEmailAsset, deleteEmailAsset, getAgentEmailSettings, getTeamEmailSettings,
  listEmailAssets, patchAgentEmailSettings, patchEmailAsset, patchTeamEmailSettings, previewEmailTemplate,
} from '@/apis';
import { escapeHtml } from '@/components/agentic/emailShared';
import {
  C, Card, CardTitle, Label, OptionCard, SwitchRow, failMsg, fieldSx, formatBytes, outlineBtnSx, primaryBtnSx, readOnlySx,
} from './ui';

/* Email > Setup: who it comes from, what it says, which files go with it,
 * and the booking button - with a live preview of the real email.
 *
 * The sending mode (off / review / auto) lives on the Overview tab, so a save
 * here never sends `mode`.
 *
 * Team sharing: the business admin's email and email content are shared with
 * the whole team by default. The admin's two switches (admin only) apply to
 * everyone at once; this agent's two switches are its own choice. The content
 * shown is the TEAM's when this agent uses it (editable by the admin only)
 * and the agent's own otherwise - never a mix.
 *
 * Every effect depends on `api?.axiosInstance`, never on `api`: the context
 * value is a new object on every provider render, and depending on it causes
 * a self-sustaining request storm that freezes the app.
 */

const UPLOAD_ACCEPT = '.pdf,.doc,.docx,.ppt,.pptx,.xls,.xlsx,.png,.jpg,.jpeg';
const MAX_UPLOAD_MB = 25;
const CAP_MIN = 1;
const CAP_MAX = 200;

// What the AI-mode preview shows in place of the body it will write.
const AI_SAMPLE_BODY = 'Hi {{first_name}},\n\n'
  + '[The AI writes this part for each prospect: it picks up your LinkedIn conversation, '
  + 'answers what they asked, and mentions the files. Your tips steer it.]';

const TIP_CHIPS = [
  'Keep it under 100 words.',
  'Never quote prices; offer a call instead.',
  'Mention the attachments by name.',
  'Friendly, not salesy.',
];

const CONTENT_KEYS = ['body_mode', 'playbook', 'fixed_subject', 'fixed_body', 'include_booking_link', 'cta_text', 'cta_url'];
const contentOf = (s) => ({
  body_mode: s?.body_mode || 'ai',
  playbook: s?.playbook || '',
  fixed_subject: s?.fixed_subject || '',
  fixed_body: s?.fixed_body || '',
  include_booking_link: s?.include_booking_link !== false,
  cta_text: s?.cta_text || '',
  cta_url: s?.cta_url || '',
});
const formOf = (s) => ({
  daily_cap: s.daily_cap,
  use_team_mailbox: s.use_team_mailbox !== false,
  use_team_content: s.use_team_content !== false,
  ...contentOf(s),
});
const FORM_KEYS = ['daily_cap', 'use_team_mailbox', 'use_team_content', ...CONTENT_KEYS];
const sameKeys = (keys, a, b) => keys.every((k) => (a?.[k] ?? '') === (b?.[k] ?? ''));
const hasContent = (c, assets) => Boolean(
  c && (c.playbook.trim() || c.fixed_subject.trim() || c.fixed_body.trim() || c.body_mode === 'fixed'
    || c.cta_url.trim() || (assets || []).length),
);

const localPreviewHtml = (text) => String(text || '')
  .split(/\n\s*\n/).filter((p) => p.trim())
  .map((p) => `<p>${escapeHtml(p.trim()).replace(/\n/g, '<br>')}</p>`).join('');

function FileRow({ asset, readOnly, onToggle, onDelete }) {
  const isLink = asset.kind === 'link';
  const Icon = isLink ? RiLinkM : RiAttachment2;
  return (
    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, p: 1.25, borderRadius: '10px', border: `1px solid ${C.border}`, bgcolor: asset.is_active ? '#fff' : C.soft }}>
      <Box sx={{ width: 34, height: 34, bgcolor: C.tint, borderRadius: '8px', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
        <Icon size={16} style={{ color: C.blueText }} />
      </Box>
      <Box sx={{ flex: 1, minWidth: 0, opacity: asset.is_active ? 1 : 0.7 }}>
        <Typography sx={{ fontSize: '0.86rem', fontWeight: 500, color: C.text, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{asset.name}</Typography>
        <Typography sx={{ fontSize: '0.74rem', color: C.muted, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {isLink ? asset.url : `${asset.filename || 'file'}${asset.file_size ? ` · ${formatBytes(asset.file_size)}` : ''}`}
          {!asset.is_active && ' · not sent'}
        </Typography>
      </Box>
      {!readOnly && (
        <>
          <Tooltip title={asset.is_active ? 'Sent with every email' : 'Not sent'}>
            <Switch checked={Boolean(asset.is_active)} onChange={() => onToggle(asset)} inputProps={{ 'aria-label': `Send ${asset.name}` }} />
          </Tooltip>
          <IconButton size="small" aria-label={`Remove ${asset.name}`} onClick={() => onDelete(asset)}
            sx={{ color: '#F87171', '&:hover': { color: '#DC2626', bgcolor: '#FEF2F2' } }}>
            <RiDeleteBinLine size={16} />
          </IconButton>
        </>
      )}
    </Box>
  );
}

function PreviewPanel({ fromEmail, subject, subjectNote, bodyHtml, attachments, note, source }) {
  return (
    <Card sx={{ overflow: 'hidden' }}>
      <Box sx={{ px: 2, py: 1.25, bgcolor: C.soft, borderBottom: `1px solid ${C.border}`, display: 'flex', alignItems: 'center', gap: 1 }}>
        <RiEyeLine size={15} style={{ color: C.muted }} />
        <Typography sx={{ fontSize: '0.76rem', fontWeight: 600, color: C.body }}>Preview</Typography>
        {source && (
          <Chip size="small" label={source} sx={{ ml: 'auto', height: 20, fontSize: '0.68rem', bgcolor: C.tint, color: C.blueText, fontWeight: 600 }} />
        )}
      </Box>
      {fromEmail && (
        <Typography sx={{ px: 2, py: 1.25, borderBottom: `1px solid ${C.border}`, fontSize: '0.78rem', color: C.muted }}>
          From <Box component="b" sx={{ color: C.text, fontWeight: 500 }}>{fromEmail}</Box>
        </Typography>
      )}
      <Typography sx={{ px: 2, pt: 1.75, fontSize: '0.92rem', fontWeight: 600, color: subject ? C.text : C.muted, fontStyle: subject ? 'normal' : 'italic' }}>
        {subject || subjectNote || 'No subject'}
      </Typography>
      <Box sx={{ px: 2, py: 1.5, maxHeight: 'calc(100vh - 380px)', overflowY: 'auto' }}>
        {bodyHtml
          ? <Box sx={{ fontSize: '0.875rem', lineHeight: 1.75, color: '#1E293B', wordBreak: 'break-word', '& p': { m: 0, mb: 1.5 }, '& a': { color: C.blue } }}
                 dangerouslySetInnerHTML={{ __html: bodyHtml }} />
          : <Typography sx={{ fontSize: '0.84rem', color: C.muted, fontStyle: 'italic' }}>Start writing to see the email.</Typography>}
      </Box>
      {attachments.length > 0 && (
        <Box sx={{ px: 2, py: 1.25, borderTop: `1px solid ${C.border}`, display: 'flex', gap: 0.75, flexWrap: 'wrap' }}>
          {attachments.map((a) => (
            <Chip key={a.id} size="small" icon={a.kind === 'link' ? <RiLinkM size={12} /> : <RiAttachment2 size={12} />}
              label={a.name} sx={{ height: 24, bgcolor: C.tint, color: C.blueText, fontSize: '0.72rem' }} />
          ))}
        </Box>
      )}
      {note && (
        <Typography sx={{ px: 2, py: 1.25, borderTop: `1px solid ${C.border}`, bgcolor: C.soft, fontSize: '0.74rem', color: C.muted }}>{note}</Typography>
      )}
    </Card>
  );
}

/* ══════════════════════════════════════════════════════════════════════ */
export default function EmailSetup({ agentId, onSaved }) {
  const api = useContext(ApiContext);
  const { showSnackbar } = useContext(SnackbarContext) || {};
  const axiosInstance = api?.axiosInstance;

  const [settings, setSettings] = useState(null);
  const [form, setForm] = useState(null);         // this agent: daily cap, its two switches, its own content
  const [savedForm, setSavedForm] = useState(null);
  const [team, setTeam] = useState(null);         // the admin's switches + team content, as saved
  const [teamForm, setTeamForm] = useState(null); // team content being edited (admin only)
  const [ownAssets, setOwnAssets] = useState([]);
  const [teamAssets, setTeamAssets] = useState([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [sharingBusy, setSharingBusy] = useState('');
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [copying, setCopying] = useState(false);
  const [error, setError] = useState('');
  const [preview, setPreview] = useState(null);
  const bodyRef = useRef(null);
  const fileRef = useRef(null);

  const load = useCallback(async () => {
    if (!axiosInstance || !agentId) return;
    setLoading(true); setError('');
    try {
      const [s, own, t, ta] = await Promise.all([
        getAgentEmailSettings(api, agentId),
        listEmailAssets(api, agentId),
        getTeamEmailSettings(api, agentId).catch(() => null), // an agent with no business has no team
        listEmailAssets(api, agentId, { team: true }).catch(() => []),
      ]);
      setSettings(s);
      setForm(formOf(s));
      setSavedForm(formOf(s));
      setOwnAssets(Array.isArray(own) ? own : []);
      setTeam(t);
      setTeamForm(t ? contentOf(t) : null);
      setTeamAssets(Array.isArray(ta) ? ta : []);
    } catch (e) {
      setError(e?.response?.data?.error || e?.message || 'Could not load the email settings');
    } finally { setLoading(false); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [axiosInstance, agentId]);

  useEffect(() => { load(); }, [load]);

  // ── who sends, whose content ────────────────────────────────────────────
  const rd = settings?.readiness;
  const isAdminAgent = Boolean(rd?.is_admin_agent);
  const canManage = Boolean(team?.can_manage);
  const adminName = team?.admin?.name || rd?.team?.admin_name || 'your admin';
  const adminFirst = adminName.split(' ')[0];
  const adminMailboxReady = Boolean(team?.admin?.mailbox_ready);
  const adminMailboxEmail = team?.admin?.mailbox_email || '';
  const shareMailbox = Boolean(team?.share_mailbox);
  const shareContent = Boolean(team?.share_content);
  const usage = team?.usage;
  // Live from the form, so the page reflects a switch before it is saved.
  // Same rules as the backend's sharing.py: both sides must be on, and the
  // admin's own agents always use the team content.
  const usesTeamMailbox = Boolean(team) && !isAdminAgent && shareMailbox && Boolean(form?.use_team_mailbox);
  const usesTeamContent = Boolean(team) && Boolean(form?.use_team_content) && (isAdminAgent || shareContent);
  const content = usesTeamContent ? teamForm : form;
  const editable = usesTeamContent ? canManage : true;
  const assets = usesTeamContent ? teamAssets : ownAssets;
  const teamDirty = Boolean(canManage && team && teamForm && !sameKeys(CONTENT_KEYS, teamForm, contentOf(team)));
  const ownDirty = Boolean(form && savedForm && !sameKeys(FORM_KEYS, form, savedForm));
  const dirty = teamDirty || ownDirty;
  const ownMailboxReady = Boolean(rd?.own_mailbox?.ready);
  const explicitSender = Boolean(rd?.ready && rd?.sender_type === 'sender_config');

  const sendingFrom = (() => {
    if (explicitSender) return { ok: true, email: rd.from_email, shared: false };
    if (usesTeamMailbox && adminMailboxReady) return { ok: true, email: adminMailboxEmail, shared: true };
    if (ownMailboxReady) return { ok: true, email: rd.own_mailbox.email, shared: false };
    return { ok: false };
  })();

  const patch = (k, v) => setForm((f) => ({ ...f, [k]: v }));
  const setContent = (k, v) => (usesTeamContent ? setTeamForm : setForm)((f) => ({ ...f, [k]: v }));

  // ── preview: rendered by the BACKEND with the same functions that build
  // the real email, so the button and merge tags can never differ from it.
  const isFixed = content?.body_mode === 'fixed';
  const pvSubject = isFixed ? (content?.fixed_subject || '') : '';
  const pvBody = isFixed ? (content?.fixed_body || '') : AI_SAMPLE_BODY;
  const pvCta = Boolean(content?.include_booking_link);
  const pvCtaText = content?.cta_text || '';
  const pvCtaUrl = content?.cta_url || '';

  useEffect(() => {
    if (!axiosInstance || !agentId || !content) return undefined;
    let cancelled = false;
    const t = setTimeout(() => {
      previewEmailTemplate(api, agentId, {
        subject: pvSubject, body: pvBody, include_cta: pvCta, cta_text: pvCtaText, cta_url: pvCtaUrl,
      })
        .then((d) => { if (!cancelled) setPreview(d); })
        .catch(() => { if (!cancelled) setPreview(null); });
    }, 450);
    return () => { cancelled = true; clearTimeout(t); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [axiosInstance, agentId, Boolean(content), pvSubject, pvBody, pvCta, pvCtaText, pvCtaUrl]);

  const mergeTags = settings?.merge_tags || ['first_name', 'company_name', 'role', 'sender_name', 'sender_company'];
  const activeAssets = assets.filter((a) => a.is_active);
  const ctaResolvedUrl = preview?.cta_url || '';
  const ownIsEmpty = !hasContent(form && contentOf(form), ownAssets);
  const teamHasContent = hasContent(teamForm, teamAssets);

  // ── actions ─────────────────────────────────────────────────────────────
  const save = async () => {
    if (!form) return;
    setSaving(true);
    try {
      if (teamDirty) {
        const t = await patchTeamEmailSettings(api, agentId, teamForm);
        setTeam(t);
        setTeamForm(contentOf(t));
      }
      if (ownDirty) {
        const s = await patchAgentEmailSettings(api, agentId, form); // never `mode`: that is the Overview's
        setSettings(s);
        setForm(formOf(s));
        setSavedForm(formOf(s));
      }
      onSaved?.();
      showSnackbar?.({
        message: teamDirty ? 'Saved. The team setup is updated for everyone using it.' : 'Saved. New emails use this setup.',
        severity: 'success',
      });
    } catch (e) {
      const d = e?.response?.data;
      showSnackbar?.({ message: failMsg(e, 'Save failed'), severity: 'error' });
      if (d?.readiness) setSettings((prev) => ({ ...prev, readiness: d.readiness }));
    } finally { setSaving(false); }
  };

  const discard = () => {
    if (savedForm) setForm(savedForm);
    if (team) setTeamForm(contentOf(team));
  };

  // The admin's switches save at once: they change every team member's email.
  const setSharing = async (key, value) => {
    const stuck = usage?.need_own_mailbox || [];
    if (key === 'share_mailbox' && !value && stuck.length) {
      const ok = window.confirm(
        `${stuck.join(', ')} ${stuck.length === 1 ? 'has' : 'have'} no email of their own. `
        + 'Their emails will wait until they connect one. Stop sharing your email with the team?',
      );
      if (!ok) return;
    }
    setSharingBusy(key);
    try {
      const t = await patchTeamEmailSettings(api, agentId, { [key]: value });
      setTeam(t); // unsaved team-content edits stay in teamForm
      setSettings(await getAgentEmailSettings(api, agentId)); // fresh readiness; the form keeps its edits
      onSaved?.();
      showSnackbar?.({ message: value ? 'Shared with your team' : 'Stopped sharing with your team', severity: 'success' });
    } catch (e) {
      showSnackbar?.({ message: failMsg(e, 'Could not change sharing'), severity: 'error' });
    } finally { setSharingBusy(''); }
  };

  const copyFromTeam = async () => {
    setCopying(true);
    try {
      const s = await copyTeamEmailContent(api, agentId);
      setSettings(s);
      setForm((f) => ({ ...f, use_team_content: false, ...contentOf(s) }));
      setSavedForm(formOf(s));
      setOwnAssets(await listEmailAssets(api, agentId));
      onSaved?.();
      showSnackbar?.({ message: `Copied ${adminFirst}'s setup. Change anything you like, then save.`, severity: 'success' });
    } catch (e) {
      showSnackbar?.({ message: failMsg(e, 'Copy failed'), severity: 'error' });
    } finally { setCopying(false); }
  };

  const insertTag = (tag) => {
    const el = bodyRef.current;
    const text = content?.fixed_body || '';
    const at = el && typeof el.selectionStart === 'number' ? el.selectionStart : text.length;
    setContent('fixed_body', `${text.slice(0, at)}{{${tag}}}${text.slice(at)}`);
    requestAnimationFrame(() => { if (el) { el.focus(); el.selectionStart = el.selectionEnd = at + tag.length + 4; } });
  };

  const addTip = (tip) => {
    const current = (content?.playbook || '').trim();
    if (current.includes(tip)) return;
    setContent('playbook', current ? `${current}\n${tip}` : tip);
  };

  const refreshAssets = async (forTeam) => {
    if (forTeam) setTeamAssets(await listEmailAssets(api, agentId, { team: true }));
    else setOwnAssets(await listEmailAssets(api, agentId));
  };

  const upload = async (files) => {
    const list = Array.from(files || []);
    if (!list.length) return;
    const forTeam = usesTeamContent;
    setUploading(true);
    let added = 0;
    try {
      for (const file of list) {
        if (file.size > MAX_UPLOAD_MB * 1024 * 1024) {
          showSnackbar?.({ message: `${file.name} is larger than ${MAX_UPLOAD_MB} MB`, severity: 'error' });
          continue;
        }
        const fd = new FormData();
        fd.append('agent', String(agentId));
        fd.append('kind', 'file');
        fd.append('name', file.name.replace(/\.[^.]+$/, '') || file.name);
        fd.append('file', file);
        if (forTeam) fd.append('team', '1');
        try {
          await createEmailAsset(api, fd);
          added += 1;
        } catch (e) {
          showSnackbar?.({ message: `${file.name}: ${failMsg(e, 'upload failed')}`, severity: 'error' });
        }
      }
      if (added) {
        await refreshAssets(forTeam);
        onSaved?.();
        showSnackbar?.({ message: added === 1 ? 'File added' : `${added} files added`, severity: 'success' });
      }
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = '';
    }
  };

  const setAssetRows = (asset, fn) => (asset.is_team ? setTeamAssets : setOwnAssets)(fn);
  const toggleAsset = async (asset) => {
    try {
      const updated = await patchEmailAsset(api, asset.id, { is_active: !asset.is_active });
      setAssetRows(asset, (rows) => rows.map((r) => (r.id === asset.id ? updated : r)));
      onSaved?.();
    } catch (e) { showSnackbar?.({ message: failMsg(e, 'Update failed'), severity: 'error' }); }
  };
  const removeAsset = async (asset) => {
    const who = asset.is_team ? ' It will no longer be sent by anyone on your team.' : ' It will no longer be sent.';
    if (!window.confirm(`Remove "${asset.name}"?${who}`)) return;
    try {
      await deleteEmailAsset(api, asset.id);
      setAssetRows(asset, (rows) => rows.filter((r) => r.id !== asset.id));
      onSaved?.();
    } catch (e) { showSnackbar?.({ message: failMsg(e, 'Delete failed'), severity: 'error' }); }
  };

  // ── words for the switches ──────────────────────────────────────────────
  const teamOf = (n) => (usage ? ` · ${n} of ${usage.members} team ${usage.members === 1 ? 'agent uses' : 'agents use'} it` : '');
  const mailboxDetail = !shareMailbox
    ? `${adminName} has turned off sharing their email.`
    : !adminMailboxReady
      ? `${adminName} hasn't connected an email yet, so your own is used.`
      : form?.use_team_mailbox
        ? `${adminMailboxEmail}. Prospects see this address and replies go to it.`
        : 'Off: emails go from your own connected email.';
  const contentDetail = isAdminAgent
    ? 'This is what your team sends. Turn off to give this agent a setup of its own.'
    : !shareContent
      ? `${adminName} has turned off sharing their setup.`
      : form?.use_team_content
        ? `The email, files and button are set by ${adminName}.`
        : 'Off: this agent uses its own setup below.';
  const canUseAdminMailbox = Boolean(team) && !isAdminAgent && shareMailbox;
  const noMailboxText = !canUseAdminMailbox
    ? 'No email connected. Connect one to send.'
    : form?.use_team_mailbox
      ? `No email to send from: ${adminFirst} hasn't connected one and you have none of your own. Connect yours, or ask ${adminFirst}.`
      : `No email connected. Connect your own, or switch on ${adminFirst}'s email above.`;
  const sourceChip = usesTeamContent ? (isAdminAgent || canManage ? 'Team setup' : `${adminFirst}'s setup`) : 'Your setup';

  if (!agentId) return <Alert severity="info" sx={{ borderRadius: '10px' }}>Select an agent first.</Alert>;
  if (error && !form) return <Alert severity="error" sx={{ borderRadius: '10px' }}>{error}</Alert>;
  if (!form || !content) return loading ? <LinearProgress sx={{ borderRadius: 4 }} /> : null;

  const cap = Number(form.daily_cap) || CAP_MIN;
  const setCap = (n) => patch('daily_cap', Math.min(CAP_MAX, Math.max(CAP_MIN, Math.round(n) || CAP_MIN)));

  return (
    <Box>
      <Box sx={{ display: 'flex', gap: 2, alignItems: 'flex-start', flexWrap: 'wrap' }}>
        {/* ══ LEFT: the settings ══ */}
        <Box sx={{ flex: '999 1 520px', minWidth: 0, display: 'flex', flexDirection: 'column', gap: 2 }}>
          {error && <Alert severity="error" onClose={() => setError('')} sx={{ borderRadius: '10px' }}>{error}</Alert>}

          {/* ── The admin's switches: apply to the whole team at once ── */}
          {canManage && (
            <Card sx={{ p: 2.5, display: 'flex', flexDirection: 'column', gap: 1.5 }}>
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.25 }}>
                <Box sx={{ width: 34, height: 34, bgcolor: C.tint, borderRadius: '9px', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                  <RiTeamLine size={17} style={{ color: C.blueText }} />
                </Box>
                <CardTitle title="Share with your team" hint="These apply to everyone at once. Each person can still switch to their own." sx={{ flex: 1 }} />
              </Box>
              <SwitchRow
                title="My team sends from my email"
                detail={adminMailboxReady
                  ? `${adminMailboxEmail}${teamOf(usage?.on_mailbox ?? 0)}`
                  : 'Connect your email first. Until then your team sends from their own.'}
                checked={shareMailbox}
                busy={sharingBusy === 'share_mailbox'}
                onChange={(v) => setSharing('share_mailbox', v)}
              />
              <SwitchRow
                title="My team uses my email setup"
                detail={`The email, files and button below${teamOf(usage?.on_content ?? 0)}`}
                checked={shareContent}
                busy={sharingBusy === 'share_content'}
                onChange={(v) => setSharing('share_content', v)}
              />
              {shareMailbox && usage?.need_own_mailbox?.length > 0 && (
                <Typography sx={{ fontSize: '0.76rem', color: '#92400E' }}>
                  {usage.need_own_mailbox.join(', ')} rely on your email: they have none of their own.
                </Typography>
              )}
            </Card>
          )}

          {/* ── This agent's team choices ── */}
          {team && (
            <Card sx={{ p: 2.5, display: 'flex', flexDirection: 'column', gap: 1.25 }}>
              <CardTitle title="Team setup" hint={`${adminName} can share an email address and an email setup with the team.`} />
              {!isAdminAgent && (
                <SwitchRow
                  title={`Send from ${adminFirst}'s email`}
                  detail={mailboxDetail}
                  checked={shareMailbox && form.use_team_mailbox}
                  disabled={!shareMailbox}
                  onChange={(v) => patch('use_team_mailbox', v)}
                />
              )}
              <SwitchRow
                title={isAdminAgent ? 'Use the team setup' : `Use ${adminFirst}'s email setup`}
                detail={contentDetail}
                checked={(isAdminAgent || shareContent) && form.use_team_content}
                disabled={!isAdminAgent && !shareContent}
                onChange={(v) => patch('use_team_content', v)}
              />
            </Card>
          )}

          {/* ── 1. Send from ── */}
          <Card sx={{ p: 2.5, display: 'flex', flexDirection: 'column', gap: 2 }}>
            <CardTitle title="Send from" />
            {rd && (
              <Alert
                icon={sendingFrom.ok ? <RiCheckboxCircleLine size={18} /> : <RiErrorWarningLine size={18} />}
                severity={sendingFrom.ok ? (sendingFrom.shared ? 'info' : 'success') : 'warning'}
                sx={{ borderRadius: '10px', alignItems: 'center' }}
                action={!sendingFrom.ok
                  ? <Button size="small" href={rd.connect_url || '/agency/integration'} sx={{ textTransform: 'none', fontWeight: 600 }}>Connect email</Button>
                  : null}
              >
                {sendingFrom.ok && sendingFrom.shared
                  ? <><b>{sendingFrom.email}</b> ({adminFirst}&apos;s email), on behalf of {rd.owner_name || 'this agent'}. Replies go to that inbox.</>
                  : sendingFrom.ok
                    ? <><b>{sendingFrom.email}</b> is connected. Replies come back to this inbox.</>
                    : <>{noMailboxText}</>}
              </Alert>
            )}
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, flexWrap: 'wrap' }}>
              <Box sx={{ flex: '1 1 200px', minWidth: 0 }}>
                <Typography id="daily-cap-label" sx={{ fontSize: '0.88rem', fontWeight: 500, color: C.text }}>Daily limit</Typography>
                <Typography sx={{ fontSize: '0.78rem', color: C.muted }}>Keeps your mailbox safe from spam filters.</Typography>
              </Box>
              <Box sx={{ display: 'flex', alignItems: 'center', border: '1px solid #CBD5E1', borderRadius: '8px', overflow: 'hidden' }}>
                <IconButton aria-label="Fewer emails per day" onClick={() => setCap(cap - 5)} disabled={cap <= CAP_MIN} sx={{ borderRadius: 0, width: 42, height: 42 }}>
                  <RiSubtractLine size={16} />
                </IconButton>
                <TextField
                  size="small" type="number" value={form.daily_cap}
                  onChange={(e) => patch('daily_cap', e.target.value === '' ? '' : Number(e.target.value))}
                  onBlur={() => setCap(cap)}
                  inputProps={{ min: CAP_MIN, max: CAP_MAX, 'aria-labelledby': 'daily-cap-label', style: { textAlign: 'center', width: 48 } }}
                  sx={{ '& .MuiOutlinedInput-root': { borderRadius: 0, '& fieldset': { border: 0 } } }}
                />
                <Typography sx={{ fontSize: '0.8rem', color: C.muted, pr: 1 }}>a day</Typography>
                <IconButton aria-label="More emails per day" onClick={() => setCap(cap + 5)} disabled={cap >= CAP_MAX} sx={{ borderRadius: 0, width: 42, height: 42 }}>
                  <RiAddLine size={16} />
                </IconButton>
              </Box>
            </Box>
          </Card>

          {/* ── 2. The email ── */}
          <Card sx={{ p: 2.5, display: 'flex', flexDirection: 'column', gap: 2 }}>
            <CardTitle title="What should the email say?" hint="It always continues your LinkedIn conversation with that person." />

            {usesTeamContent && !canManage && (
              <Alert severity="info" sx={{ borderRadius: '10px' }}>
                Set by {adminName}. To change it, switch off &quot;Use {adminFirst}&apos;s email setup&quot; above and write your own.
              </Alert>
            )}
            {usesTeamContent && canManage && usage && usage.on_content > 0 && (
              <Alert severity="info" sx={{ borderRadius: '10px' }}>
                You are editing the team setup. Changes apply to everyone using it ({usage.on_content} team {usage.on_content === 1 ? 'agent' : 'agents'}).
              </Alert>
            )}
            {!usesTeamContent && team && ownIsEmpty && teamHasContent && (
              <Box>
                <Button size="small" variant="outlined" onClick={copyFromTeam} disabled={copying}
                  startIcon={copying ? <CircularProgress size={13} /> : <RiFileCopyLine size={14} />} sx={outlineBtnSx}>
                  Start from {adminFirst}&apos;s setup
                </Button>
              </Box>
            )}

            <Box role="radiogroup" aria-label="Who writes the email" sx={{ display: 'flex', gap: 1.5, flexWrap: 'wrap' }}>
              <OptionCard icon={RiSparklingLine} title="AI writes it" detail="A fresh email for each person, based on your chat."
                selected={!isFixed} disabled={!editable} onClick={() => setContent('body_mode', 'ai')} />
              <OptionCard icon={RiFileTextLine} title="Use my template" detail="The same words every time, with their details filled in."
                selected={isFixed} disabled={!editable} onClick={() => setContent('body_mode', 'fixed')} />
            </Box>

            {!isFixed && (
              <Box>
                <Label>Tips for the AI (optional)</Label>
                <TextField fullWidth multiline minRows={3} maxRows={12} size="small" sx={editable ? fieldSx : readOnlySx}
                  InputProps={{ readOnly: !editable }}
                  placeholder={editable ? 'Plain sentences work best, e.g. Keep it short. Never quote prices.' : 'No tips.'}
                  value={content.playbook} onChange={(e) => setContent('playbook', e.target.value)} />
                {editable && (
                  <Box sx={{ display: 'flex', gap: 0.75, flexWrap: 'wrap', mt: 1, alignItems: 'center' }}>
                    <Typography sx={{ fontSize: '0.74rem', color: C.muted, mr: 0.25 }}>Quick add:</Typography>
                    {TIP_CHIPS.map((tip) => (
                      <Chip key={tip} size="small" label={`+ ${tip.replace(/\.$/, '')}`} onClick={() => addTip(tip)}
                        sx={{ height: 26, fontSize: '0.72rem', bgcolor: C.tint, color: C.blueText, cursor: 'pointer', '&:hover': { bgcolor: '#DBEAFE' } }} />
                    ))}
                  </Box>
                )}
              </Box>
            )}

            {isFixed && (
              <>
                <Box>
                  <Label>Subject</Label>
                  <TextField fullWidth size="small" sx={editable ? fieldSx : readOnlySx} value={content.fixed_subject}
                    InputProps={{ readOnly: !editable }}
                    onChange={(e) => setContent('fixed_subject', e.target.value)}
                    placeholder="The details you asked for, {{first_name}}" />
                </Box>
                <Box>
                  <Label>Message</Label>
                  <TextField fullWidth multiline minRows={7} maxRows={16} size="small" sx={editable ? fieldSx : readOnlySx}
                    inputRef={bodyRef} value={content.fixed_body} InputProps={{ readOnly: !editable }}
                    onChange={(e) => setContent('fixed_body', e.target.value)}
                    placeholder={'Hi {{first_name}},\n\nGreat speaking on LinkedIn. As promised, the details are attached.'} />
                  {editable && (
                    <Box sx={{ display: 'flex', gap: 0.75, flexWrap: 'wrap', mt: 1, alignItems: 'center' }}>
                      <Typography sx={{ fontSize: '0.74rem', color: C.muted, mr: 0.25 }}>Insert:</Typography>
                      {mergeTags.map((tag) => (
                        <Chip key={tag} size="small" label={`{{${tag}}}`} onClick={() => insertTag(tag)}
                          sx={{ height: 24, fontSize: '0.7rem', fontFamily: 'monospace', bgcolor: C.tint, color: C.blueText, cursor: 'pointer', '&:hover': { bgcolor: '#DBEAFE' } }} />
                      ))}
                    </Box>
                  )}
                </Box>
              </>
            )}
            <Typography sx={{ fontSize: '0.74rem', color: C.muted }}>
              Names come from the CRM. The signature of whoever sends is added automatically.
            </Typography>
          </Card>

          {/* ── 3. Files ── */}
          <Card sx={{ p: 2.5, display: 'flex', flexDirection: 'column', gap: 1.25 }}>
            <CardTitle title="Files to include" hint="Switched-on files go with every email. Leave empty to send without one." sx={{ mb: 0.5 }} />
            {assets.map((a) => (
              <FileRow key={a.id} asset={a} readOnly={!editable} onToggle={toggleAsset} onDelete={removeAsset} />
            ))}
            {!editable && assets.length === 0 && (
              <Typography sx={{ fontSize: '0.8rem', color: C.muted }}>No files.</Typography>
            )}
            {editable && (
              <Box component="label"
                onDragOver={(e) => { e.preventDefault(); if (!uploading) setDragOver(true); }}
                onDragLeave={() => setDragOver(false)}
                onDrop={(e) => { e.preventDefault(); setDragOver(false); if (!uploading) upload(e.dataTransfer.files); }}
                sx={{
                  position: 'relative',
                  display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 1, minHeight: 56, px: 2, borderRadius: '10px',
                  border: `1.5px dashed ${dragOver ? C.blue : '#CBD5E1'}`, bgcolor: dragOver ? C.tint : '#FAFBFC',
                  cursor: uploading ? 'default' : 'pointer',
                  '&:hover': uploading ? {} : { borderColor: C.blue, bgcolor: C.tint },
                  '&:focus-within': { outline: `2px solid ${C.blue}`, outlineOffset: 2 },
                }}>
                {uploading ? <CircularProgress size={16} /> : <RiUploadCloud2Line size={18} style={{ color: C.muted }} />}
                <Typography sx={{ fontSize: '0.82rem', color: C.body, fontWeight: 500 }}>
                  {uploading ? 'Uploading…' : `Drop a PDF or document here, or browse (up to ${MAX_UPLOAD_MB} MB)`}
                </Typography>
                <input ref={fileRef} type="file" multiple accept={UPLOAD_ACCEPT} disabled={uploading}
                  onChange={(e) => upload(e.target.files)}
                  style={{ position: 'absolute', width: 1, height: 1, opacity: 0, pointerEvents: 'none' }} />
              </Box>
            )}
          </Card>

          {/* ── 4. Booking button ── */}
          <Card sx={{ p: 2.5, display: 'flex', flexDirection: 'column', gap: 2 }}>
            <CardTitle title="Booking button" hint="A button at the end of every email so they can book a call."
              action={<Switch checked={content.include_booking_link} disabled={!editable}
                onChange={(e) => setContent('include_booking_link', e.target.checked)} inputProps={{ 'aria-label': 'Add the booking button' }} />} />
            {content.include_booking_link && (
              <Box sx={{ display: 'flex', gap: 1.5, flexWrap: 'wrap' }}>
                <Box sx={{ flex: '1 1 200px', minWidth: 0 }}>
                  <Label>Button text</Label>
                  <TextField fullWidth size="small" sx={editable ? fieldSx : readOnlySx} value={content.cta_text}
                    inputProps={{ maxLength: 80, readOnly: !editable }} placeholder="Book a 15-minute call"
                    onChange={(e) => setContent('cta_text', e.target.value)} />
                </Box>
                <Box sx={{ flex: '2 1 260px', minWidth: 0 }}>
                  <Label>Link</Label>
                  <TextField fullWidth size="small" sx={editable ? fieldSx : readOnlySx} value={content.cta_url}
                    InputProps={{ readOnly: !editable }}
                    placeholder={ctaResolvedUrl && !content.cta_url ? ctaResolvedUrl : 'https://'}
                    onChange={(e) => setContent('cta_url', e.target.value)}
                    helperText={content.cta_url
                      ? 'Everyone using this setup links here.'
                      : usesTeamContent
                        ? "Empty = each agent's own booking link from the Identity tab."
                        : 'Empty = your booking link from the Identity tab.'} />
                </Box>
              </Box>
            )}
            {content.include_booking_link && preview && !ctaResolvedUrl && (
              <Alert severity="warning" sx={{ borderRadius: '10px' }}>
                There is no booking link yet, so no button will be added. Add one in the Identity tab or paste a link here.
              </Alert>
            )}
          </Card>
        </Box>

        {/* ══ RIGHT: live preview ══ */}
        <Box sx={{ flex: '1 1 340px', minWidth: 0, position: { lg: 'sticky' }, top: 16 }}>
          <PreviewPanel
            fromEmail={sendingFrom.ok ? sendingFrom.email : ''}
            subject={isFixed ? (preview?.subject ?? content?.fixed_subject) : ''}
            subjectNote={isFixed ? null : 'Written by the AI for each prospect'}
            bodyHtml={preview?.body_html ?? localPreviewHtml(pvBody)}
            attachments={activeAssets}
            source={team ? sourceChip : null}
            note={isFixed
              ? 'Sample values. Real names come from the CRM at send time.'
              : 'Example only. The AI writes a new email for every prospect.'}
          />
        </Box>
      </Box>

      {/* ── Save bar: only when something changed ── */}
      {(dirty || saving) && (
        <Box sx={{
          position: 'sticky', bottom: 12, zIndex: 5, mt: 2, px: 2.5, py: 1.5, borderRadius: '12px', bgcolor: C.text,
          display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap', boxShadow: '0 8px 24px rgba(15,23,42,.18)',
        }}>
          <Typography sx={{ flex: '1 1 220px', fontSize: '0.86rem', color: '#E2E8F0' }}>
            {teamDirty ? 'Unsaved changes. Saving updates the team setup for everyone using it.' : 'You have unsaved changes.'}
          </Typography>
          <Button onClick={discard} disabled={saving} sx={{ textTransform: 'none', fontWeight: 600, color: '#E2E8F0', borderRadius: '8px', border: '1px solid #475569', px: 2 }}>
            Discard
          </Button>
          <Button variant="contained" onClick={save} disabled={saving}
            startIcon={saving ? <CircularProgress size={13} color="inherit" /> : null} sx={{ ...primaryBtnSx, px: 2.5 }}>
            Save changes
          </Button>
        </Box>
      )}
    </Box>
  );
}
