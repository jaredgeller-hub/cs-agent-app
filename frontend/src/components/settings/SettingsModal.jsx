import { useEffect, useRef, useState } from 'react';
import { useClerk } from '@clerk/clerk-react';
import { X } from 'lucide-react';
import api from '../../lib/api';
import NudgeCadenceSection from './NudgeCadenceSection';
import ConnectionsSection from './ConnectionsSection';

// ── Style constants (mirror SubscriptionGateModal tokens; textAlign:'left'
//    per Flag 2 — Settings has multi-paragraph body copy) ─────────────────
const OVERLAY_STYLE = {
  position: 'fixed', inset: 0, zIndex: 9999,
  background: 'rgba(0,0,0,0.45)',
  display: 'flex', alignItems: 'center', justifyContent: 'center',
  fontFamily: "'DM Sans', -apple-system, sans-serif",
};
const CARD_STYLE = {
  background: '#ffffff',
  border: '1px solid rgba(0,0,0,0.08)',
  borderRadius: 16,
  boxShadow: '0 2px 8px rgba(0,0,0,0.04), 0 8px 24px rgba(0,0,0,0.06)',
  padding: 40,
  maxWidth: 480,
  width: '100%',
  position: 'relative',
  textAlign: 'left',
};
const CLOSE_BTN_STYLE = {
  position: 'absolute', top: 16, right: 16,
  background: 'none', border: 'none', cursor: 'pointer',
  color: '#9ca3af', padding: 4, display: 'flex', alignItems: 'center',
};
const TITLE_STYLE = { fontSize: 20, fontWeight: 500, color: '#1a1a1a', margin: '0 0 24px' };
const SECTION_HEADER_STYLE = {
  fontSize: 12, fontWeight: 500, color: '#6b7280',
  textTransform: 'uppercase', letterSpacing: '0.05em', margin: '0 0 12px',
};
const TOGGLE_ROW_STYLE = {
  display: 'flex', alignItems: 'center', gap: 10,
  fontSize: 14, color: '#1a1a1a', fontWeight: 500,
  margin: '0 0 16px', cursor: 'pointer',
};
const BODY_STYLE = { fontSize: 14, color: '#6b7280', lineHeight: 1.55, margin: '0 0 12px' };
const ERROR_BODY_STYLE = {
  ...BODY_STYLE,
  color: '#dc2626', background: '#fef2f2',
  border: '1px solid #fee2e2', borderRadius: 8,
  padding: '12px 16px', margin: '0 0 20px',
};
const PRIMARY_BTN_STYLE = {
  display: 'block', width: '100%', padding: '12px 16px', borderRadius: 12,
  border: 'none', background: '#1a1a1a', color: '#ffffff',
  fontSize: 14, fontWeight: 500, cursor: 'pointer', fontFamily: 'inherit',
  marginTop: 8,
};
const SECONDARY_BTN_STYLE = {
  ...PRIMARY_BTN_STYLE, background: 'transparent', color: '#9ca3af',
};
const DONE_BTN_STYLE = { ...PRIMARY_BTN_STYLE, marginTop: 20 };
// Destructive button — reuses the ERROR_BODY_STYLE red (#dc2626) so the
// delete affordances read as dangerous, not as the neutral primary action.
const DANGER_BTN_STYLE = { ...PRIMARY_BTN_STYLE, background: '#dc2626' };


export default function SettingsModal({ isOpen, onClose }) {
  const { signOut } = useClerk();
  const [loading, setLoading] = useState(true);
  const [enabled, setEnabled] = useState(false);
  const [scopeGranted, setScopeGranted] = useState(false);
  const [view, setView] = useState('main');
  const [saving, setSaving] = useState(false);
  const [cancelText, setCancelText] = useState(null);
  const [deleting, setDeleting] = useState(false);

  // Refs for one-shot flags / timeout handles. useRef (not useState) so
  // setTimeout closures read the current value instead of capturing a
  // stale one at scheduling time.
  const retryAttemptedRef = useRef(false);
  const popupTimeoutRef = useRef(null);
  const cancelTimeoutRef = useRef(null);

  // True only while a WRITE-grant OAuth flow ("Continue to Google") is in
  // flight. The Connections section (Item 1) reuses the same popup +
  // oauth_complete/oauth_canceled postMessage to do a READ-scope Google
  // reconnect. Without this gate, that read-scope completion would trigger
  // attemptEnable (POST enabled:true → likely partial_scope_error) and a
  // read-scope cancel would flip the Actions toggle off. The listener acts
  // on a Google oauth message only when this is true.
  const writeGrantPendingRef = useRef(false);

  // ── Effects ────────────────────────────────────────────────────────────

  // Initial GET + window focus re-check (mirror SubscriptionGateModal).
  useEffect(() => {
    if (!isOpen) return;
    let cancelled = false;

    async function loadState() {
      try {
        const res = await api.get('/app/settings/google-actions');
        if (cancelled) return;
        if (!res.ok) { setLoading(false); return; }
        const data = await res.json();
        if (cancelled) return;
        setEnabled(!!data.enabled);
        setScopeGranted(!!data.scope_granted);
      } catch (err) {
        if (cancelled) return;
        if (err?.message === 'Unauthorized') setView('session_expired_error');
        // other errors: fail silent — keep loading=false below
      }
      finally { if (!cancelled) setLoading(false); }
    }

    loadState();
    function onFocus() { loadState(); }
    window.addEventListener('focus', onFocus);
    return () => {
      cancelled = true;
      window.removeEventListener('focus', onFocus);
    };
  }, [isOpen]);

  // postMessage listener — mirrors OnboardingWizard.jsx origin-validation
  // pattern exactly. Same-origin + oauth_complete/oauth_canceled + provider=google only.
  useEffect(() => {
    if (!isOpen) return;
    const handleMessage = (event) => {
      if (event.origin !== window.location.origin) return;
      if (event.data?.type === 'oauth_complete') {
        if (event.data.provider !== 'google') return;
        // Ignore unless a write-grant flow is pending — a read-scope
        // Google reconnect from the Connections section posts the same
        // message and must not touch the Actions toggle.
        if (!writeGrantPendingRef.current) return;
        setCancelText(null);
        handleOAuthComplete();
      } else if (event.data?.type === 'oauth_canceled') {
        if (event.data.provider !== 'google') return;
        if (!writeGrantPendingRef.current) return;
        if (popupTimeoutRef.current) {
          clearTimeout(popupTimeoutRef.current);
          popupTimeoutRef.current = null;
        }
        setEnabled(false);
        setView('main');
        if (cancelTimeoutRef.current) clearTimeout(cancelTimeoutRef.current);
        setCancelText('Connection canceled. Try again when ready.');
        cancelTimeoutRef.current = setTimeout(() => setCancelText(null), 3000);
        writeGrantPendingRef.current = false;
      }
    };
    window.addEventListener('message', handleMessage);
    return () => {
      window.removeEventListener('message', handleMessage);
      if (cancelTimeoutRef.current) clearTimeout(cancelTimeoutRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen]);

  // Escape key → close
  useEffect(() => {
    if (!isOpen) return;
    const handleKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [isOpen, onClose]);

  // Reset transient state when modal closes (pending timeout, retry flag,
  // view, loading indicator — so the next open starts clean).
  useEffect(() => {
    if (isOpen) return;
    if (popupTimeoutRef.current) {
      clearTimeout(popupTimeoutRef.current);
      popupTimeoutRef.current = null;
    }
    if (cancelTimeoutRef.current) {
      clearTimeout(cancelTimeoutRef.current);
      cancelTimeoutRef.current = null;
    }
    retryAttemptedRef.current = false;
    writeGrantPendingRef.current = false;
    setView('main');
    setLoading(true);
    setCancelText(null);
    setDeleting(false);
  }, [isOpen]);

  // ── Handlers ───────────────────────────────────────────────────────────

  async function handleToggleChange(checked) {
    setEnabled(checked); // optimistic visual update

    if (!checked) {
      // State 3: disable
      setSaving(true);
      try {
        const res = await api.post('/app/settings/google-actions', { enabled: false });
        if (!res.ok) setEnabled(true); // revert optimistic
      } catch (err) {
        if (err?.message === 'Unauthorized') {
          setEnabled(true); // revert optimistic (ON → OFF attempted)
          setView('session_expired_error');
          return;
        }
        throw err;
      } finally { setSaving(false); }
      return;
    }

    // checked === true
    if (!scopeGranted) {
      // State 1: no scope → intermediate view
      setView('intermediate');
      return;
    }

    // State 2: direct enable
    setSaving(true);
    try {
      const res = await api.post('/app/settings/google-actions', { enabled: true });
      if (!res.ok) {
        setEnabled(false);
        if (res.status === 409) setView('partial_scope_error');
      }
    } catch (err) {
      if (err?.message === 'Unauthorized') {
        setEnabled(false); // revert optimistic (OFF → ON attempted)
        setView('session_expired_error');
        return;
      }
      throw err;
    } finally { setSaving(false); }
  }

  function handleContinueToGoogle() {
    // Clear any stale abandonment timeout from a prior Continue click —
    // repeat clicks (close popup 1, Continue again, succeed in popup 2)
    // would otherwise leave the first timeout orphaned and fire 30s after
    // the first click, silently reverting enabled=false + view='main'
    // even though the user succeeded in popup 2.
    if (popupTimeoutRef.current) {
      clearTimeout(popupTimeoutRef.current);
      popupTimeoutRef.current = null;
    }
    retryAttemptedRef.current = false;
    // A write-grant flow is now in flight — the message listener may act
    // on the resulting Google oauth_complete/oauth_canceled.
    writeGrantPendingRef.current = true;

    // Identity comes from the verified Clerk JWT (__session cookie
    // attached on top-level navigation). The `scope=write` flag is
    // not identity — it tells the backend to request gmail.send.
    const authUrl = '/auth/google/start?scope=write';
    // OAuth flow opens in a popup window. The explicit features string
    // ('width=500,height=600,popup=yes') is required for modern Chrome to
    // treat this as a popup rather than a tab/same-tab navigation. Bare
    // window.open(url, '_blank') silently regressed to same-tab behavior
    // (G.2 Phase 7 verification, May 2026). The OAuth callback HTML in
    // main.py defensively handles both popup and same-tab cases, but the
    // popup path is the intended UX (user stays in-app, callback closes
    // itself, parent refreshes via postMessage).
    const popup = window.open(authUrl, '_blank', 'width=500,height=600,popup=yes');
    if (!popup) {
      setView('popup_blocked_error');
      return;
    }
    // 30-second abandonment timeout. Cancelled by handleOAuthComplete.
    popupTimeoutRef.current = setTimeout(() => {
      setEnabled(false);
      setView('main');
      popupTimeoutRef.current = null;
      writeGrantPendingRef.current = false;
    }, 30000);
  }

  function handleCancelIntermediate() {
    setEnabled(false);
    setView('main');
    writeGrantPendingRef.current = false;
  }

  async function handleOAuthComplete() {
    if (popupTimeoutRef.current) {
      clearTimeout(popupTimeoutRef.current);
      popupTimeoutRef.current = null;
    }
    await attemptEnable();
  }

  async function attemptEnable() {
    setSaving(true);
    try {
      const res = await api.post('/app/settings/google-actions', { enabled: true });
      if (res.ok) {
        setEnabled(true);
        setScopeGranted(true);
        setView('main');
        retryAttemptedRef.current = false;
        writeGrantPendingRef.current = false;
        return;
      }
      if (res.status === 409 && !retryAttemptedRef.current) {
        // Race: OAuth callback's DB write may not have committed yet.
        // Single retry with 500ms delay. useRef (not useState) so the
        // setTimeout closure reads the current retry flag value.
        retryAttemptedRef.current = true;
        setTimeout(() => { attemptEnable(); }, 500);
        return;
      }
      // Exhausted retry or non-409 error
      setEnabled(false);
      setView('partial_scope_error');
      writeGrantPendingRef.current = false;
    } catch (err) {
      if (err?.message === 'Unauthorized') {
        setEnabled(false); // revert optimistic
        setView('session_expired_error');
        return;
      }
      throw err;
    } finally { setSaving(false); }
  }

  function handleBackdropClick(e) {
    if (e.target === e.currentTarget) onClose();
  }

  async function handleConfirmDelete() {
    setDeleting(true);
    try {
      const res = await api.post('/app/settings/delete-my-data', { confirm: true });
      if (res.ok) {
        const data = await res.json().catch(() => ({}));
        // Both "deleted" and "data_deleted_clerk_failed" mean the user's
        // data is gone. The clerk-failed case is founder-alerted server-side;
        // from the user's side the account is deleted either way, so we don't
        // branch the experience — sign out and redirect for both.
        if (data.status === 'deleted' || data.status === 'data_deleted_clerk_failed') {
          signOut({ redirectUrl: '/' });
          return;
        }
        // Unexpected 2xx body — treat as a non-completion.
        setView('delete_error');
        return;
      }
      // Non-2xx (403 / 500 / 503) come back as a returned res, not a throw.
      // Backend guarantees nothing was deleted in all three cases.
      setView('delete_error');
    } catch (err) {
      if (err?.message === 'Unauthorized') {
        setView('session_expired_error');
        return;
      }
      throw err;
    } finally {
      setDeleting(false);
    }
  }

  // ── Render ─────────────────────────────────────────────────────────────

  if (!isOpen) return null;

  return (
    <div style={OVERLAY_STYLE} onClick={handleBackdropClick}>
      <div style={CARD_STYLE}>
        <button onClick={onClose} style={CLOSE_BTN_STYLE} aria-label="Close settings">
          <X size={18} />
        </button>
        <h2 style={TITLE_STYLE}>Settings</h2>

        {loading ? (
          <p style={BODY_STYLE}>Loading…</p>
        ) : (
          <>
            {view === 'main' && (
              <>
                <ConnectionsSection />

                <section style={{ marginTop: 28 }}>
                  <h3 style={SECTION_HEADER_STYLE}>Actions</h3>
                  {cancelText && (
                    <p style={{ ...BODY_STYLE, color: '#6b7280', fontSize: 13 }}>{cancelText}</p>
                  )}
                  <label style={TOGGLE_ROW_STYLE}>
                    <input
                      type="checkbox"
                      checked={enabled}
                      disabled={saving}
                      onChange={(e) => handleToggleChange(e.target.checked)}
                    />
                    <span>Let CS Agent take actions in Google for me</span>
                  </label>
                  <p style={BODY_STYLE}>
                    At signup you gave CS Agent permission to read your Google data.
                    Turning this on grants permission to take actions.
                  </p>
                  <p style={BODY_STYLE}>
                    When enabled, CS Agent can send emails from your Gmail, create
                    and update events on your Calendar, and create documents in
                    your Drive - always after you approve each action. CS Agent
                    will never take an action you haven't approved.
                  </p>
                </section>

                <NudgeCadenceSection />

                <section style={{ marginTop: 28 }}>
                  <h3 style={SECTION_HEADER_STYLE}>Data and privacy</h3>
                  <p style={BODY_STYLE}>
                    Permanently delete all of your CS Agent data and your
                    account. This cannot be undone.
                  </p>
                  <button
                    onClick={() => setView('confirm_delete')}
                    style={DANGER_BTN_STYLE}
                  >
                    Delete my data
                  </button>
                </section>

                <button onClick={onClose} style={DONE_BTN_STYLE}>Done</button>
              </>
            )}

            {view === 'confirm_delete' && (
              <section>
                <h3 style={SECTION_HEADER_STYLE}>Delete my data</h3>
                <p style={BODY_STYLE}>
                  This permanently deletes all of your CS Agent data
                  <strong> and your login</strong>. You will be signed out and
                  will not be able to log back in. This cannot be undone.
                </p>
                <button
                  onClick={handleConfirmDelete}
                  disabled={deleting}
                  style={{ ...DANGER_BTN_STYLE, opacity: deleting ? 0.6 : 1 }}
                >
                  {deleting ? 'Deleting…' : 'Permanently delete everything'}
                </button>
                <button
                  onClick={() => setView('main')}
                  disabled={deleting}
                  style={SECONDARY_BTN_STYLE}
                >
                  Cancel
                </button>
              </section>
            )}

            {view === 'delete_error' && (
              <section>
                <h3 style={SECTION_HEADER_STYLE}>Data and privacy</h3>
                <p style={ERROR_BODY_STYLE}>
                  The deletion didn't complete. Nothing was deleted — you can
                  try again.
                </p>
                <button onClick={() => setView('main')} style={PRIMARY_BTN_STYLE}>
                  Back to settings
                </button>
              </section>
            )}

            {view === 'intermediate' && (
              <section>
                <h3 style={SECTION_HEADER_STYLE}>Google</h3>
                <p style={BODY_STYLE}>
                  Google will ask for permission to send emails and manage
                  calendar events on your behalf. Approve both to finish turning
                  this on.
                </p>
                <button onClick={handleContinueToGoogle} style={PRIMARY_BTN_STYLE}>
                  Continue to Google
                </button>
                <button onClick={handleCancelIntermediate} style={SECONDARY_BTN_STYLE}>
                  Cancel
                </button>
              </section>
            )}

            {view === 'partial_scope_error' && (
              <section>
                <h3 style={SECTION_HEADER_STYLE}>Google</h3>
                <p style={ERROR_BODY_STYLE}>
                  Google didn't grant full permission. Try again and approve
                  both: "Send email on your behalf" and "View and edit events on
                  all your calendars."
                </p>
                <button onClick={() => setView('intermediate')} style={PRIMARY_BTN_STYLE}>
                  Try again
                </button>
              </section>
            )}

            {view === 'popup_blocked_error' && (
              <section>
                <h3 style={SECTION_HEADER_STYLE}>Google</h3>
                <p style={ERROR_BODY_STYLE}>
                  Your browser blocked the Google popup. Please allow popups
                  for cs-agent.app and try again.
                </p>
                <button onClick={() => setView('intermediate')} style={PRIMARY_BTN_STYLE}>
                  Try again
                </button>
              </section>
            )}

            {view === 'session_expired_error' && (
              <section>
                <p style={BODY_STYLE}>
                  Your session has expired. Please refresh the page to continue.
                </p>
                <button onClick={() => window.location.reload()} style={PRIMARY_BTN_STYLE}>
                  Refresh
                </button>
              </section>
            )}
          </>
        )}
      </div>
    </div>
  );
}
