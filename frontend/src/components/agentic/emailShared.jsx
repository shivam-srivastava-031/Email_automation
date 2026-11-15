"use client";
import React, { useEffect, useState } from "react";

// Shared by the deal-page EmailPanel and the Knowledge Base > Email review
// queue, so a status reads and a draft round-trips the same way in both.

export const STATUS_META = {
  awaiting_review: { label: "Awaiting your review", short: "Draft ready", bg: "#FEF3C7", fg: "#92400E" },
  held: { label: "Needs a human", short: "Needs you", bg: "#FEE2E2", fg: "#991B1B" },
  scheduled: { label: "Scheduled", short: "Scheduled", bg: "#E0F2FE", fg: "#0369A1" },
  sending: { label: "Sending", short: "Sending", bg: "#E0F2FE", fg: "#0369A1" },
  sent: { label: "Sent", short: "Sent", bg: "#DCFCE7", fg: "#166534" },
  cancelled: { label: "Cancelled", short: "Cancelled", bg: "#F1F5F9", fg: "#475569" },
  skipped: { label: "No email needed", short: "Skipped", bg: "#F1F5F9", fg: "#475569" },
  blocked: { label: "Blocked", short: "Blocked", bg: "#F1F5F9", fg: "#475569" },
  failed: { label: "Failed", short: "Failed", bg: "#FEE2E2", fg: "#991B1B" },
  evaluating: { label: "Thinking", short: "Thinking", bg: "#F1F5F9", fg: "#475569" },
};

export const when = (iso) => {
  if (!iso) return "";
  try { return new Date(iso).toLocaleString("en-IN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: true }); } catch { return ""; }
};

// The body is server-escaped HTML paragraphs; editing happens as plain text.
export const htmlToText = (html) => String(html || "")
  .replace(/<br\s*\/?>/gi, "\n").replace(/<\/p>\s*<p>/gi, "\n\n").replace(/<[^>]+>/g, "")
  .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&#x27;/g, "'").trim();
export const escapeHtml = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
export const textToHtml = (text) => String(text || "").split(/\n\s*\n/).filter((p) => p.trim())
  .map((p) => `<p>${escapeHtml(p.trim()).replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1">$1</a>').replace(/\n/g, "<br>")}</p>`).join("\n");

export function Countdown({ until }) {
  const [left, setLeft] = useState(0);
  useEffect(() => {
    const tick = () => setLeft(Math.max(0, Math.round((new Date(until).getTime() - Date.now()) / 1000)));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [until]);
  if (!until) return null;
  const m = Math.floor(left / 60); const s = left % 60;
  return <span>{left > 0 ? `sends in ${m}:${String(s).padStart(2, "0")}` : "sending now"}</span>;
}
