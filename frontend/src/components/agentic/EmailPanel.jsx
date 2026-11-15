"use client";
import React, { useContext, useState } from "react";
import {
  Box, Button, Chip, CircularProgress, Dialog, DialogActions, DialogContent, DialogTitle,
  TextField, Tooltip, Typography,
} from "@mui/material";
import { RiAttachment2, RiCheckDoubleLine, RiLinkM, RiMailSendLine, RiTimeLine } from "@remixicon/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiContext } from "@/components/ApiContext";
import SnackbarContext from "@/components/SnackbarContext";
import {
  approveEmailDecision, cancelEmailDecision, listEmailDecisions, skipEmailDecision, updateEmailDecisionDraft,
} from "@/apis";

// The emails the AI SDR has written (or refused to write) for this person.
// A draft waits here in review mode; in auto mode a scheduled send shows a
// countdown and a Cancel. Every row carries the prospect's own words that
// justified it, so a rep can judge the decision, not just the prose.

import { Countdown, STATUS_META, htmlToText, textToHtml, when } from "./emailShared";

function DecisionCard({ row, onApprove, onSkip, onCancel, onEdit, busy }) {
  const meta = STATUS_META[row.status] || STATUS_META.evaluating;
  const actionable = row.status === "awaiting_review" || row.status === "held";
  return (
    <Box sx={{ border: "1px solid #E2E8F0", borderRadius: "12px", p: 2, bgcolor: "#fff" }}>
      <Box sx={{ display: "flex", alignItems: "center", gap: 1, flexWrap: "wrap" }}>
        <Chip size="small" label={meta.label} sx={{ bgcolor: meta.bg, color: meta.fg, fontWeight: 600, height: 22 }} />
        <Typography variant="caption" sx={{ color: "#64748B" }}>
          {row.status === "sent" ? when(row.sent_at) : row.status === "scheduled" ? <Countdown until={row.cancel_deadline_at} /> : when(row.created_at)}
        </Typography>
        {row.opened?.is_opened && (
          <Tooltip title={`Opened ${row.opened.open_count} time(s), first ${when(row.opened.opened_at)}`}>
            <Chip size="small" icon={<RiCheckDoubleLine size={13} />} label="Opened" sx={{ bgcolor: "#DCFCE7", color: "#166534", height: 22 }} />
          </Tooltip>
        )}
        <Box sx={{ ml: "auto", display: "flex", gap: 0.75 }}>
          {actionable && <Button size="small" variant="outlined" onClick={() => onEdit(row)} disabled={busy} sx={{ textTransform: "none", borderRadius: "8px" }}>Edit</Button>}
          {actionable && <Button size="small" variant="contained" onClick={() => onApprove(row)} disabled={busy || !row.recipient_email} startIcon={<RiMailSendLine size={13} />} sx={{ textTransform: "none", borderRadius: "8px", bgcolor: "#2563eb" }}>Send</Button>}
          {actionable && <Button size="small" color="inherit" onClick={() => onSkip(row)} disabled={busy} sx={{ textTransform: "none", borderRadius: "8px", color: "#64748B" }}>Skip</Button>}
          {row.status === "scheduled" && <Button size="small" color="error" variant="outlined" onClick={() => onCancel(row)} disabled={busy} sx={{ textTransform: "none", borderRadius: "8px" }}>Cancel</Button>}
        </Box>
      </Box>

      {(row.ask_summary || row.hold_reason) && (
        <Typography sx={{ fontSize: "0.8rem", color: "#334155", mt: 1 }}>
          {row.ask_summary && <>They asked for: <b>{row.ask_summary}</b>. </>}
          {row.hold_reason && <span style={{ color: "#991B1B" }}>{row.hold_reason}</span>}
        </Typography>
      )}
      {Array.isArray(row.evidence) && row.evidence.length > 0 && (
        <Box sx={{ mt: 1, pl: 1.5, borderLeft: "3px solid #CBD5E1" }}>
          {row.evidence.slice(0, 2).map((q, i) => (
            <Typography key={i} sx={{ fontSize: "0.78rem", color: "#475569", fontStyle: "italic" }}>“{q}”</Typography>
          ))}
        </Box>
      )}

      {(row.subject || row.body_html) && (
        <Box sx={{ mt: 1.5, p: 1.5, borderRadius: "10px", bgcolor: "#F8FAFC", border: "1px solid #F1F5F9" }}>
          <Typography variant="caption" sx={{ color: "#64748B" }}>To: {row.recipient_email || "(no address yet)"}{row.from_email ? ` · From: ${row.from_email}` : ""}</Typography>
          <Typography sx={{ fontSize: "0.85rem", fontWeight: 600, color: "#0F172A", mt: 0.25 }}>{row.subject}</Typography>
          <Box sx={{ fontSize: "0.82rem", color: "#334155", "& p": { m: 0, mb: 1 }, "& a": { color: "#2563eb" } }} dangerouslySetInnerHTML={{ __html: row.body_html || "" }} />
          {Array.isArray(row.assets) && row.assets.length > 0 && (
            <Box sx={{ display: "flex", gap: 0.75, flexWrap: "wrap", mt: 0.5 }}>
              {row.assets.map((a) => (
                <Chip key={a.id} size="small" icon={a.kind === "link" ? <RiLinkM size={12} /> : <RiAttachment2 size={12} />} label={a.name} sx={{ height: 22, bgcolor: "#EFF6FF", color: "#1D4ED8" }} />
              ))}
            </Box>
          )}
        </Box>
      )}
      {row.error && <Typography sx={{ fontSize: "0.75rem", color: "#991B1B", mt: 1 }}>{row.error}</Typography>}
    </Box>
  );
}

export default function EmailPanel({ contactId, dealId, theirName = "Lead" }) {
  const api = useContext(ApiContext);
  const { showSnackbar } = useContext(SnackbarContext) || {};
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(null);
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [recipient, setRecipient] = useState("");

  const params = contactId ? { contact_id: contactId } : dealId ? { deal_id: dealId } : null;
  const key = ["email-decisions", contactId || null, dealId || null];
  const { data: rows = [], isLoading } = useQuery({
    queryKey: key,
    queryFn: () => listEmailDecisions(api, params),
    enabled: Boolean(api && params),
    staleTime: 10_000,
    refetchInterval: 15_000,
  });

  const refresh = () => queryClient.invalidateQueries({ queryKey: key });
  const fail = (e) => showSnackbar?.({ message: e?.response?.data?.error || e?.message || "Action failed", severity: "error" });

  const approve = useMutation({ mutationFn: ({ id, payload }) => approveEmailDecision(api, id, payload || {}), onSuccess: () => { refresh(); showSnackbar?.({ message: "Email sent", severity: "success" }); }, onError: fail });
  const skip = useMutation({ mutationFn: (id) => skipEmailDecision(api, id, "skipped by rep"), onSuccess: refresh, onError: fail });
  const cancel = useMutation({ mutationFn: (id) => cancelEmailDecision(api, id, "cancelled by rep"), onSuccess: () => { refresh(); showSnackbar?.({ message: "Send cancelled", severity: "success" }); }, onError: fail });
  const update = useMutation({ mutationFn: ({ id, payload }) => updateEmailDecisionDraft(api, id, payload), onSuccess: () => { refresh(); setEditing(null); }, onError: fail });
  const busy = approve.isPending || skip.isPending || cancel.isPending || update.isPending;

  const openEdit = (row) => { setEditing(row); setSubject(row.subject || ""); setBody(htmlToText(row.body_html)); setRecipient(row.recipient_email || ""); };
  const saveEdit = () => update.mutate({ id: editing.id, payload: { subject, body_html: textToHtml(body), ...(recipient ? { recipient_email: recipient } : {}) } });
  const sendEdited = () => approve.mutate({ id: editing.id, payload: { subject, body_html: textToHtml(body), ...(recipient ? { recipient_email: recipient } : {}) } }, { onSuccess: () => setEditing(null) });

  const open = rows.filter((r) => ["awaiting_review", "held", "scheduled", "sending", "evaluating"].includes(r.status));
  const done = rows.filter((r) => !open.includes(r));

  if (!params) return <Typography variant="caption" color="text.disabled">No person linked to this record.</Typography>;

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 1.5 }}>
      {isLoading && <Box sx={{ display: "flex", justifyContent: "center", py: 3 }}><CircularProgress size={20} /></Box>}
      {!isLoading && rows.length === 0 && (
        <Box sx={{ textAlign: "center", py: 4, color: "#94A3B8" }}>
          <RiMailSendLine size={26} style={{ opacity: 0.5 }} />
          <Typography sx={{ fontSize: "0.85rem", mt: 1 }}>No AI emails for {theirName} yet.</Typography>
          <Typography variant="caption">When they ask for material over LinkedIn, the draft appears here.</Typography>
        </Box>
      )}
      {open.length > 0 && (
        <>
          <Typography sx={{ fontSize: "0.75rem", fontWeight: 700, color: "#64748B", textTransform: "uppercase", letterSpacing: 0.4 }}>Needs attention</Typography>
          {open.map((r) => (
            <DecisionCard key={r.id} row={r} busy={busy}
              onApprove={(row) => approve.mutate({ id: row.id })}
              onSkip={(row) => skip.mutate(row.id)}
              onCancel={(row) => cancel.mutate(row.id)}
              onEdit={openEdit} />
          ))}
        </>
      )}
      {done.length > 0 && (
        <>
          <Typography sx={{ fontSize: "0.75rem", fontWeight: 700, color: "#64748B", textTransform: "uppercase", letterSpacing: 0.4, mt: open.length ? 1 : 0 }}>History</Typography>
          {done.map((r) => (
            <DecisionCard key={r.id} row={r} busy={busy} onApprove={() => {}} onSkip={() => {}} onCancel={() => {}} onEdit={() => {}} />
          ))}
        </>
      )}

      <Dialog open={Boolean(editing)} onClose={() => setEditing(null)} fullWidth maxWidth="sm">
        <DialogTitle sx={{ fontSize: "1rem" }}>Edit email to {theirName}</DialogTitle>
        <DialogContent sx={{ display: "flex", flexDirection: "column", gap: 1.5, pt: 1 }}>
          <TextField size="small" label="To" value={recipient} onChange={(e) => setRecipient(e.target.value)} />
          <TextField size="small" label="Subject" value={subject} onChange={(e) => setSubject(e.target.value)} />
          <TextField size="small" label="Body" multiline minRows={8} value={body} onChange={(e) => setBody(e.target.value)} helperText="Attachments and links chosen by the agent are kept. The signature is added on send." />
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button onClick={() => setEditing(null)} sx={{ textTransform: "none" }}>Close</Button>
          <Button onClick={saveEdit} disabled={busy} sx={{ textTransform: "none" }}>Save draft</Button>
          <Button variant="contained" onClick={sendEdited} disabled={busy || !recipient} startIcon={<RiMailSendLine size={14} />} sx={{ textTransform: "none", bgcolor: "#2563eb" }}>Send now</Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
