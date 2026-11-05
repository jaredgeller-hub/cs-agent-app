// G.2 Stage 2 commit 3: helpers that drive ConnectionExpiryBanner copy.
//
// Kept in their own module (not inside ConnectionExpiryBanner.jsx) so
// the component file only exports the component — required by the
// react-refresh/only-export-components lint rule.

const PROVIDER_DISPLAY_NAMES = {
  google:    'Google',
  hubspot:   'HubSpot',
  gainsight: 'Gainsight',
  slack:     'Slack',
  zoom:      'Zoom',
};

// Rule: Google is expired if ANY of gmail/calendar shows expired.
// One Google OAuth grant covers both scopes — a sub-provider expiry
// means the grant itself is compromised, and one /auth/google/start
// popup heals both. Returning a single "google" key keeps the banner
// copy honest about the user's actual reconnect action.
//
// Order is deterministic: Google → HubSpot → Gainsight. Stable
// regardless of how the wire dict happens to iterate.
export function getExpiredProviderKeys(providers) {
  if (!providers) return [];
  const expired = [];
  if (providers.gmail === 'expired' || providers.calendar === 'expired') {
    expired.push('google');
  }
  if (providers.hubspot === 'expired') expired.push('hubspot');
  if (providers.gainsight === 'expired') expired.push('gainsight');
  return expired;
}

export function formatExpiryMessage(expiredKeys) {
  const names = expiredKeys.map((k) => PROVIDER_DISPLAY_NAMES[k]).filter(Boolean);
  if (names.length === 0) return '';
  if (names.length === 1) {
    return `Your ${names[0]} connection has expired.`;
  }
  if (names.length === 2) {
    return `Your ${names[0]} and ${names[1]} connections have expired.`;
  }
  // 3+: Oxford comma — "A, B, and C"
  const allButLast = names.slice(0, -1).join(', ');
  const last = names[names.length - 1];
  return `Your ${allButLast}, and ${last} connections have expired.`;
}

export function getProviderDisplayName(key) {
  return PROVIDER_DISPLAY_NAMES[key];
}

// ── Connection token-state color map ─────────────────────────────────────────
// Single source for the four token-state dot colors. Lifted here (out of
// ConnectionStatus.jsx) so the header dots and the Settings Connections
// section share one map instead of duplicating it a third/fourth time.
export const STATE_COLORS = {
  ok:      '#2E7D52',
  expired: '#C0392B',
  missing: '#9CA3AF',
  error:   '#D4A017',
};

// Google group state = worst-of(gmail, calendar). One Google OAuth grant
// covers both sub-scopes, so the group surfaces a single dot reflecting the
// most severe sub-state. Severity order: expired, then missing, then error,
// then ok. Mirrors getExpiredProviderKeys' "any sub-provider expired means
// the grant is compromised" logic, generalized to all four states.
export function getGoogleGroupState(providers) {
  if (!providers) return 'error';
  const states = [providers.gmail, providers.calendar];
  if (states.includes('expired')) return 'expired';
  if (states.includes('missing')) return 'missing';
  if (states.includes('error'))   return 'error';
  return 'ok';
}

// ── OAuth reconnect popup ────────────────────────────────────────────────────
// Maps a provider key to its /auth/{provider}/start route. "google" (not
// gmail/calendar) because one /auth/google/start popup heals both sub-
// providers in a single OAuth flow.
//
// NOTE: the google route here is the READ/DATA grant — no scope=write. The
// write grant lives exclusively behind the SettingsModal Actions toggle's
// "Continue to Google" flow (which appends ?scope=write itself). A reconnect
// from the banner or the Connections section must never request write.
const RECONNECT_URLS = {
  google:    '/auth/google/start',
  hubspot:   '/auth/hubspot/start',
  gainsight: '/auth/gainsight/start',
  slack:     '/auth/slack/start',
  zoom:      '/auth/zoom/start',
};

export function openProviderPopup(key) {
  const url = RECONNECT_URLS[key];
  if (!url) return;
  // OAuth flow opens in a popup window. The explicit features string
  // ('width=500,height=600,popup=yes') is required for modern Chrome to
  // treat this as a popup rather than a tab/same-tab navigation. Bare
  // window.open(url, '_blank') silently regressed to same-tab behavior
  // (G.2 Phase 7 verification, May 2026). The OAuth callback HTML in
  // main.py defensively handles both popup and same-tab cases, but the
  // popup path is the intended UX (user stays in-app, callback closes
  // itself, parent refreshes via postMessage).
  window.open(url, '_blank', 'width=500,height=600,popup=yes');
}
