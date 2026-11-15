"use client";
import React, { useCallback, useContext, useEffect, useState } from 'react';
import { Alert, Box, Tab, Tabs } from '@mui/material';
import { RiDashboardLine, RiInboxLine, RiSettings3Line } from '@remixicon/react';
import { ApiContext } from '@/components/ApiContext';
import { getEmailQueueCounts } from '@/apis';
import EmailOverview from './Email/EmailOverview';
import EmailReviewQueue from './Email/EmailReviewQueue';
import EmailSetup from './Email/EmailSetup';
import { C } from './Email/ui';

/* Knowledge Base > Email: three tabs.
 *
 *  Overview      - on/off (the sending mode), what needs you, setup at a glance
 *  Review queue  - every email this agent wrote: send, edit, skip, cancel
 *  Setup         - send-from, what it says, files, booking button + preview
 *
 * Setup stays mounted once opened, so unsaved edits survive a tab switch.
 * `version` bumps whenever settings or the queue change, so Overview and the
 * tab counts refresh.
 *
 * Effects depend on `api?.axiosInstance`, never on `api`: the context value is
 * a new object on every provider render (see Email/EmailSetup.jsx).
 */

const TAB_SX = {
  minHeight: 44, textTransform: 'none', fontSize: '0.86rem', fontWeight: 500, color: C.body, px: 1.75,
  '&.Mui-selected': { color: C.blueText, fontWeight: 600 },
};

function TabLabel({ icon: Icon, text, badge }) {
  return (
    <Box component="span" sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.75 }}>
      <Icon size={15} />
      {text}
      {badge > 0 && (
        <Box component="span" sx={{ bgcolor: '#FEF3C7', color: '#92400E', fontSize: '0.68rem', fontWeight: 700, px: 0.9, py: 0.1, borderRadius: '999px' }}>
          {badge}
        </Box>
      )}
    </Box>
  );
}

export default function EmailSection({ agentId }) {
  const api = useContext(ApiContext);
  const axiosInstance = api?.axiosInstance;

  const [tab, setTab] = useState('overview');
  const [focus, setFocus] = useState(null);   // { id?, filter? } for the review queue
  const [version, setVersion] = useState(0);
  const [counts, setCounts] = useState(null);
  const [setupOpened, setSetupOpened] = useState(false);

  // A different agent starts fresh on the Overview.
  useEffect(() => { setTab('overview'); setFocus(null); setSetupOpened(false); setCounts(null); }, [agentId]);

  useEffect(() => {
    if (!axiosInstance || !agentId) return undefined;
    let cancelled = false;
    getEmailQueueCounts(api, { agent: agentId }).then((d) => { if (!cancelled) setCounts(d); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [axiosInstance, agentId, version]);

  const bump = useCallback(() => setVersion((v) => v + 1), []);
  const go = (next) => { setTab(next); if (next === 'setup') setSetupOpened(true); };
  const openReview = (f) => { setFocus({ ...f, at: Date.now() }); go('review'); };

  if (!agentId) return <Alert severity="info" sx={{ borderRadius: '10px' }}>Select an agent first.</Alert>;

  const toReview = (counts?.awaiting_review || 0) + (counts?.held || 0);

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      <Tabs
        value={tab}
        onChange={(_, v) => go(v)}
        variant="scrollable"
        allowScrollButtonsMobile
        aria-label="Email automation"
        sx={{ borderBottom: `1px solid ${C.border}`, minHeight: 44, '& .MuiTabs-indicator': { bgcolor: C.blue, height: 2 } }}
      >
        <Tab value="overview" sx={TAB_SX} label={<TabLabel icon={RiDashboardLine} text="Overview" />} />
        <Tab value="review" sx={TAB_SX} label={<TabLabel icon={RiInboxLine} text="Review queue" badge={toReview} />} />
        <Tab value="setup" sx={TAB_SX} label={<TabLabel icon={RiSettings3Line} text="Setup" />} />
      </Tabs>

      {tab === 'overview' && (
        <EmailOverview
          agentId={agentId}
          version={version}
          counts={counts}
          onOpenReview={openReview}
          onOpenSetup={() => go('setup')}
          onChanged={bump}
        />
      )}

      {tab === 'review' && (
        <EmailReviewQueue agentId={agentId} counts={counts} focus={focus} onChanged={bump} />
      )}

      {(tab === 'setup' || setupOpened) && (
        <Box sx={{ display: tab === 'setup' ? 'block' : 'none' }}>
          <EmailSetup key={agentId} agentId={agentId} onSaved={bump} />
        </Box>
      )}
    </Box>
  );
}
