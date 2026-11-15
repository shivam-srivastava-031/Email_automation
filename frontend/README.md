# Email automation UI (frontend)

React / MUI screens for this Django app, built for the sales-tech Next.js frontend.
Paths under `src/` mirror where each file lives in that app; copy them into the
same paths there.

| File | What it is |
|---|---|
| `src/app/agency/newton-ai/components/KnowledgeBase/EmailSection.jsx` | Knowledge Base › Email: the shell with three tabs |
| `.../KnowledgeBase/Email/EmailOverview.jsx` | Overview: sending mode (off / review / auto), queue counts, what needs attention, setup at a glance |
| `.../KnowledgeBase/Email/EmailReviewQueue.jsx` | Review queue: every email the agent wrote; send, edit, skip, cancel |
| `.../KnowledgeBase/Email/EmailSetup.jsx` | Setup: send-from, what the email says, files, booking button, live preview, team sharing |
| `.../KnowledgeBase/Email/ui.jsx` | Shared cards, switches, option cards and status chips |
| `src/components/agentic/emailShared.jsx` | Status labels, countdown and HTML/text helpers shared with the deal page |
| `src/components/agentic/EmailPanel.jsx` | Deal-page Emails panel (now uses `emailShared.jsx`) |

The screens call the `/email-orchestrator/...` endpoints through the helpers
already in the frontend's `src/apis.js` (`getAgentEmailSettings`,
`listEmailDecisions`, `approveEmailDecision`, and so on).
