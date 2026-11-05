import { useCallback, useEffect, useRef, useState } from 'react';
import { useUser, useClerk, UserButton } from '@clerk/clerk-react';
import ChatWindow from '../chat/ChatWindow';
import ChatInput from '../chat/ChatInput';
import Sidebar from './Sidebar';
import Timeline from './Timeline';
import ConnectionStatus from '../connections/ConnectionStatus';
import ConnectionExpiryBanner from '../connections/ConnectionExpiryBanner';
import SettingsModal from '../settings/SettingsModal';
import { CreditCard, Settings } from 'lucide-react';
import { openBillingPortal, isManageSubscriptionVisible } from '../billing/ManageSubscriptionMenuItem';
import api from '../../lib/api';
import SettingsContext from '../../contexts/SettingsContext';
import { useAuthReady } from '../auth/AuthTokenProvider';
import useChat from '../../hooks/useChat';
import useDigest from '../../hooks/useDigest';
import useAlerts from '../../hooks/useAlerts';
import useAccounts from '../../hooks/useAccounts';
import useTimeline from '../../hooks/useTimeline';
import { COMPANY_NAME } from '../../lib/branding';

// ── Subscription gate modal ──────────────────────────────────────────────────

function SubscriptionGateModal() {
  const { signOut } = useClerk();
  const [checkoutLoading, setCheckoutLoading] = useState(false);

  async function handleCheckout() {
    setCheckoutLoading(true);
    try {
      const res = await api.post('/app/billing/checkout-session');
      const data = await res.json();
      // 409 already_subscribed — the user shouldn't have reached the gate
      // modal while active, but the backend defensively returns a portal_url.
      // Redirect to Portal instead of showing a hard error.
      if (res.status === 409 && data?.portal_url) {
        window.location.href = data.portal_url;
        return;
      }
      if (data.url) {
        window.location.href = data.url;
        return;
      }
    } catch { /* fall through */ }
    setCheckoutLoading(false);
  }

  const btnStyle = {
    display: 'block', width: '100%', padding: '12px 16px',
    borderRadius: 12, border: 'none',
    background: '#1a1a1a', color: '#ffffff',
    fontSize: 14, fontWeight: 500,
    textDecoration: 'none', textAlign: 'center',
    cursor: 'pointer', fontFamily: 'inherit',
    boxSizing: 'border-box',
  };

  return (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 9999,
      background: 'rgba(0,0,0,0.45)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      fontFamily: "'DM Sans', -apple-system, sans-serif",
    }}>
      <div style={{
        background: '#ffffff',
        border: '1px solid rgba(0,0,0,0.08)',
        borderRadius: 16,
        boxShadow: '0 2px 8px rgba(0,0,0,0.04), 0 8px 24px rgba(0,0,0,0.06)',
        padding: 40,
        maxWidth: 440,
        width: '100%',
        textAlign: 'center',
      }}>
        <div style={{ marginBottom: 24 }}>
          <p style={{ fontSize: 22, fontWeight: 600, color: '#1a1a1a', margin: '0 0 4px', letterSpacing: '-0.01em' }}>CS Agent</p>
          <p style={{ fontSize: 12, color: '#9ca3af', margin: 0, letterSpacing: '0.02em' }}>by {COMPANY_NAME}</p>
        </div>

        <h2 style={{ fontSize: 20, fontWeight: 500, color: '#1a1a1a', margin: '0 0 12px' }}>
          Your trial has ended
        </h2>
        <p style={{ fontSize: 14, color: '#6b7280', lineHeight: 1.55, margin: '0 0 28px' }}>
          Add a payment method to keep CS Agent running. Your accounts, connections, and data are all preserved.
        </p>

        <button
          onClick={handleCheckout}
          disabled={checkoutLoading}
          style={{ ...btnStyle, opacity: checkoutLoading ? 0.6 : 1 }}
        >
          {checkoutLoading ? 'Redirecting to Stripe...' : 'Add payment method — $39/mo'}
        </button>

        <button
          onClick={() => signOut({ redirectUrl: '/' })}
          style={{
            display: 'block', width: '100%', marginTop: 16,
            padding: 8, border: 'none', background: 'none',
            fontSize: 13, color: '#9ca3af', cursor: 'pointer',
            fontFamily: 'inherit',
          }}
        >
          Sign out
        </button>
      </div>
    </div>
  );
}

// ── Main layout ──────────────────────────────────────────────────────────────

export default function AppLayout() {
  const { user } = useUser();
  const { messages, loading, sendMessage, appendMessage } = useChat();
  const { accounts } = useAccounts();
  const { events, refresh: refreshTimeline } = useTimeline();

  useDigest(appendMessage);
  useAlerts(appendMessage, refreshTimeline);

  const handleSend = useCallback(async (text) => {
    await sendMessage(text);
    refreshTimeline();
  }, [sendMessage, refreshTimeline]);

  function handleAccountClick(name) {
    handleSend(`Tell me about ${name}`);
  }

  const displayName = user?.firstName || user?.emailAddresses?.[0]?.emailAddress || '';

  // Sync Clerk profile name + browser timezone to backend (once per session)
  const profileSynced = useRef(false);
  useEffect(() => {
    if (profileSynced.current || !user?.firstName) return;
    profileSynced.current = true;
    const body = { name: user.firstName };
    try {
      const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
      if (tz) body.timezone = tz;
    } catch { /* browser doesn't support Intl — skip */ }
    api.post('/app/profile', body).catch(() => {});
  }, [user?.firstName]);

  // Subscription gate check — waits for auth token to be ready
  const isAuthReady = useAuthReady();
  const [gated, setGated] = useState(false);
  const [hasStripeCustomer, setHasStripeCustomer] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);

  useEffect(() => {
    if (!isAuthReady) return;

    async function checkSubscription() {
      try {
        const res = await api.get('/app/subscription-status');
        // api.get throws on 401 — if we reach here, the request succeeded
        if (!res || !res.ok) {
          // Non-401 HTTP error (e.g. 500) — fail open for availability
          return;
        }
        const data = await res.json();
        setGated(!!data.is_gated);
        setHasStripeCustomer(!!data.has_stripe_customer);
      } catch (err) {
        // 401 here is an auth/session issue (e.g. Clerk JWT just expired,
        // token not yet attached on initial mount), not a billing decision.
        // Leave gated state untouched: Clerk's token-refresh flow will
        // recover and the focus listener will retry. Conflating 401 with
        // "trial ended" sent comp accounts to the gate modal.
        if (err?.message === 'Unauthorized') {
          console.warn('[subscription-status] 401 ignored; auth not ready yet');
          return;
        }
        // Network error / other exception: fail OPEN (availability > enforcement)
      }
    }
    checkSubscription();

    // Re-check on window focus (user may have just paid in the Stripe portal tab)
    function onFocus() { checkSubscription(); }
    // Re-check on 402 from any gated route — api.js dispatches this event
    // immediately before throwing ApiSubscriptionGatedError so the modal
    // flips via the existing checkSubscription() path. Defense-in-depth:
    // the focus listener is the background-liveness layer, this is the
    // active-action layer.
    function onGated() { checkSubscription(); }
    window.addEventListener('focus', onFocus);
    window.addEventListener('subscription:gated-detected', onGated);
    return () => {
      window.removeEventListener('focus', onFocus);
      window.removeEventListener('subscription:gated-detected', onGated);
    };
  }, [isAuthReady]);

  return (
    <SettingsContext.Provider value={{ openSettings: () => setSettingsOpen(true) }}>
    <div id="shell">
      {gated && <SubscriptionGateModal />}
      {settingsOpen && <SettingsModal isOpen={settingsOpen} onClose={() => setSettingsOpen(false)} />}

      <ConnectionExpiryBanner onOpenSettings={() => setSettingsOpen(true)} />

      <header id="header">
        <span className="brand">CS Agent</span>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <ConnectionStatus />
          {displayName && <span className="csm-name">{displayName}</span>}
          <UserButton
            afterSignOutUrl="/"
            userProfileProps={{
              // Hide Clerk's own Connected-accounts section in Manage
              // account. Connections are now managed solely in our Settings
              // modal (ConnectionsSection). This element key is Clerk-
              // version-dependent (@clerk/clerk-react v5) - verified by DOM
              // inspection, not by prop presence.
              appearance: {
                elements: {
                  profileSection__connectedAccounts: { display: 'none' },
                },
              },
            }}
          >
            <UserButton.MenuItems>
              {isManageSubscriptionVisible(hasStripeCustomer) && (
                <UserButton.Action
                  label="Manage subscription"
                  labelIcon={<CreditCard size={16} />}
                  onClick={() => openBillingPortal()}
                />
              )}
              <UserButton.Action
                label="Settings"
                labelIcon={<Settings size={16} />}
                onClick={() => setSettingsOpen(true)}
              />
            </UserButton.MenuItems>
          </UserButton>
        </div>
      </header>

      <div id="body-row">
        <aside id="left-sidebar">
          <Timeline events={events} />
        </aside>

        <div id="chat-col">
          <ChatWindow messages={messages} loading={loading} onChipClick={handleSend} />
          <ChatInput onSend={handleSend} disabled={loading} />
        </div>

        <Sidebar accounts={accounts} onAccountClick={handleAccountClick} />
      </div>
    </div>
    </SettingsContext.Provider>
  );
}
