import { useEffect, useRef } from 'react';
import { useAuth } from '@clerk/clerk-react';
import { getAuthToken } from '../lib/api';

// Per-user localStorage namespacing prevents conflicts if multiple CSMs
// share a browser (e.g. recording-day Chrome profile vs. day-to-day profile).
const ALERT_STORAGE_PREFIX = 'cs_agent_alerts_last_seen_';

// First-ever load default lookback. Catches alerts created while the CSM
// was away (overnight, in meetings, laptop closed). After the first
// successful poll, the stored timestamp narrows this window to only
// genuinely unseen alerts.
const INITIAL_LOOKBACK_MS = 24 * 60 * 60 * 1000;

function readInitialSince(userId) {
  const defaultSince = new Date(Date.now() - INITIAL_LOOKBACK_MS).toISOString();
  if (!userId) return defaultSince;
  try {
    const stored = localStorage.getItem(ALERT_STORAGE_PREFIX + userId);
    return stored || defaultSince;
  } catch {
    // localStorage unavailable (private browsing, quota exceeded, etc.)
    return defaultSince;
  }
}

export default function useAlerts(appendMessage, onNewAlert) {
  const { getToken, userId } = useAuth();

  // Initialize lastCheckRef once. useRef.current is undefined on first
  // render, then this assignment runs once. Subsequent renders skip it
  // because useRef preserves the value across renders.
  const lastCheckRef = useRef();
  if (lastCheckRef.current === undefined) {
    lastCheckRef.current = readInitialSince(userId);
  }

  useEffect(() => {
    const controller = new AbortController();

    async function poll() {
      try {
        // Always fetch a fresh token before polling so we never use an expired one
        const token = await getToken().catch(() => getAuthToken());
        const headers = token ? { Authorization: `Bearer ${token}` } : {};
        const res = await fetch(
          '/app/alerts?since=' + encodeURIComponent(lastCheckRef.current),
          { credentials: 'include', headers, signal: controller.signal }
        );
        if (!res || !res.ok) return;
        const data = await res.json();
        const newCheckTime = new Date().toISOString();
        lastCheckRef.current = newCheckTime;
        // Persist per-user so refreshes within a session do not re-render
        // the same alerts. Cross-session: stored value still bounds the
        // lookback so users only see what's actually new since last visit.
        if (userId) {
          try {
            localStorage.setItem(ALERT_STORAGE_PREFIX + userId, newCheckTime);
          } catch {
            // localStorage write failed (quota, private browsing); fall through
          }
        }
        const alerts = data.alerts || [];
        alerts.forEach(alert => {
          if (alert.text) appendMessage('assistant', alert.text);
          // Post-call timeline entry alert — rich object. Commit 5's
          // scheduler populates this; until then, this branch is dormant.
          if (alert.postcall_draft) {
            appendMessage('postcall_draft', alert.postcall_draft);
          } else if (alert.draft) {
            // Legacy / email draft — bare string.
            appendMessage('draft', alert.draft, { label: 'Suggested draft — review before sending' });
          }
        });
        if (alerts.length > 0 && onNewAlert) onNewAlert();
      } catch { /* fail silently (includes AbortError on cleanup) */ }
    }

    // Fire an initial poll immediately on mount so the user sees pending
    // alerts as soon as the dashboard loads. Without this, the first poll
    // fires 60s after mount with `since = mount_time`, which means alerts
    // created before page load are never fetched. This was the bug.
    poll();

    const id = setInterval(poll, 60000);
    return () => {
      controller.abort();
      clearInterval(id);
    };
  }, [appendMessage, onNewAlert, getToken, userId]);
}
