import { useState, useEffect, useRef } from 'react';
import { useUser } from '@clerk/clerk-react';
import api from '../../lib/api';
import { COMPANY_NAME } from '../../lib/branding';

// ── Style constants ──────────────────────────────────────────────────────────

const PAGE_BG = '#F7F6F3';
const CARD_STYLE = {
  background: '#ffffff',
  border: '1px solid rgba(0,0,0,0.08)',
  borderRadius: 16,
  boxShadow: '0 2px 8px rgba(0,0,0,0.04), 0 8px 24px rgba(0,0,0,0.06)',
  padding: 40,
};
const HEADING_STYLE = { fontSize: 20, fontWeight: 500, color: '#1a1a1a', margin: '0 0 8px' };
const DESC_STYLE = { fontSize: 14, color: '#6b7280', lineHeight: 1.55, margin: '0 0 28px' };
const PRIMARY_BTN = {
  width: '100%', padding: '12px 16px', borderRadius: 12, border: 'none',
  background: '#1a1a1a', color: '#ffffff', fontSize: 14, fontWeight: 500,
  cursor: 'pointer', fontFamily: 'inherit', transition: 'background 0.15s ease',
};
const SKIP_BTN = {
  width: '100%', padding: 8, border: 'none', background: 'none',
  fontSize: 13, color: '#9ca3af', cursor: 'pointer', fontFamily: 'inherit', marginTop: 4,
};

// ── Shared components ────────────────────────────────────────────────────────

function Brand() {
  return (
    <div style={{ textAlign: 'center', marginBottom: 32 }}>
      <p style={{ fontSize: 22, fontWeight: 600, color: '#1a1a1a', margin: '0 0 4px', letterSpacing: '-0.01em' }}>CS Agent</p>
      <p style={{ fontSize: 12, color: '#9ca3af', margin: 0, letterSpacing: '0.02em' }}>by {COMPANY_NAME}</p>
    </div>
  );
}

function ProgressDots({ current }) {
  const steps = [1, 2];
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 0, marginBottom: 32 }}>
      {steps.map((s, i) => {
        const completed = s < current;
        const active = s === current;
        const dotStyle = completed
          ? { width: 28, height: 28, borderRadius: '50%', background: '#16a34a', display: 'flex', alignItems: 'center', justifyContent: 'center' }
          : active
            ? { width: 28, height: 28, borderRadius: '50%', background: '#1a1a1a', color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 12, fontWeight: 500 }
            : { width: 28, height: 28, borderRadius: '50%', border: '1.5px solid #e5e7eb', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 12, color: '#9ca3af' };
        return (
          <div key={s} style={{ display: 'flex', alignItems: 'center' }}>
            {i > 0 && (
              <div style={{ width: 64, height: 1, background: completed ? '#16a34a' : '#e5e7eb' }} />
            )}
            <div style={dotStyle}>
              {completed ? (
                <svg style={{ width: 14, height: 14 }} fill="none" viewBox="0 0 24 24" stroke="#fff" strokeWidth={3}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                </svg>
              ) : s}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function StatusDot({ connected }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 12 }}>
      <div style={{ width: 7, height: 7, borderRadius: '50%', background: connected ? '#1D9E75' : '#D1D5DB' }} />
      <span style={{ fontSize: 14, color: connected ? '#15803d' : '#9ca3af', fontWeight: connected ? 500 : 400 }}>
        {connected ? 'Connected' : 'Not connected'}
      </span>
    </div>
  );
}

function Spinner({ size = 16 }) {
  return (
    <svg className="animate-spin" width={size} height={size} fill="none" viewBox="0 0 24 24">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  );
}

const GoogleLogo = () => (
  <svg style={{ width: 16, height: 16 }} viewBox="0 0 24 24">
    <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
    <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
    <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/>
    <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/>
  </svg>
);

// ── Step 1: Connect Google ───────────────────────────────────────────────────

function StepGoogle({ userId, connected, cancelText, onContinue, onSkip }) {
  return (
    <div>
      <ProgressDots current={1} />
      <h2 style={HEADING_STYLE}>Connect Google</h2>
      <p style={DESC_STYLE}>
        CS Agent reads your Gmail and Calendar to detect stakeholder silence, prep you for meetings,
        and draft follow-ups. Email content is processed in memory and never stored.
      </p>

      {/*
        OAuth flow opens in a popup window. The explicit features string
        ('width=500,height=600,popup=yes') is required for modern Chrome to
        treat this as a popup rather than a tab/same-tab navigation. Bare
        window.open(url, '_blank') silently regressed to same-tab behavior
        (G.2 Phase 7 verification, May 2026). The OAuth callback HTML in
        main.py defensively handles both popup and same-tab cases, but the
        popup path is the intended UX (user stays in-app, callback closes
        itself, parent refreshes via postMessage).
      */}
      <button
        onClick={() => window.open('/auth/google/start', '_blank', 'width=500,height=600,popup=yes')}
        style={{
          width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center',
          gap: 10, padding: '12px 16px', border: '1px solid #e5e7eb', borderRadius: 12,
          background: '#ffffff', fontSize: 14, fontWeight: 500, color: '#1a1a1a',
          cursor: 'pointer', fontFamily: 'inherit',
          boxShadow: '0 1px 3px rgba(0,0,0,0.06)', transition: 'all 0.15s ease',
        }}
      >
        <GoogleLogo />
        Connect with Google
      </button>

      <StatusDot connected={connected} />

      {cancelText && (
        <p style={{ fontSize: 12, color: '#6b7280', marginTop: 12 }}>{cancelText}</p>
      )}

      {!connected && !cancelText && (
        <p style={{ fontSize: 12, color: '#d97706', marginTop: 12 }}>
          Your daily briefing won't work without this connected.
        </p>
      )}

      <div style={{ marginTop: 28, display: 'flex', flexDirection: 'column' }}>
        <button onClick={onContinue} style={PRIMARY_BTN}>Continue</button>
        <button onClick={onSkip} style={SKIP_BTN}>Skip for now</button>
      </div>
    </div>
  );
}

// ── Step 2: Connect CRM ──────────────────────────────────────────────────────

const TILE_STYLE = {
  flex: 1, border: '1px solid #e5e7eb', borderRadius: 8, padding: 16,
  cursor: 'pointer', textAlign: 'center',
  boxShadow: '0 1px 2px rgba(0,0,0,0.04)', transition: 'all 0.15s ease',
};

function StepCRM({ userId, crmConnected, cancelText, onContinue, onSkip }) {
  return (
    <div>
      <ProgressDots current={2} />
      <h2 style={HEADING_STYLE}>Connect your CRM</h2>
      <p style={DESC_STYLE}>
        CS Agent surfaces renewal risks, health changes, and overdue CTAs alongside your email and calendar signals.
      </p>

      {/*
        OAuth flow opens in a popup window. The explicit features string
        ('width=500,height=600,popup=yes') is required for modern Chrome to
        treat this as a popup rather than a tab/same-tab navigation. Bare
        window.open(url, '_blank') silently regressed to same-tab behavior
        (G.2 Phase 7 verification, May 2026). The OAuth callback HTML in
        main.py defensively handles both popup and same-tab cases, but the
        popup path is the intended UX (user stays in-app, callback closes
        itself, parent refreshes via postMessage).
      */}
      <div style={{ display: 'flex', gap: 12, marginBottom: 16 }}>
        {/* Gainsight tile */}
        <div onClick={() => window.open('/auth/gainsight/start', '_blank', 'width=500,height=600,popup=yes')} style={TILE_STYLE}>
          <div style={{ width: 36, height: 36, borderRadius: 8, display: 'flex', alignItems: 'center', justifyContent: 'center', margin: '0 auto 10px', background: '#F0F4FF' }}>
            <svg style={{ width: 20, height: 20 }} viewBox="0 0 24 24" fill="none">
              <circle cx="12" cy="12" r="9" stroke="#4F6AFF" strokeWidth="2" />
              <path d="M8 12l3 3 5-5" stroke="#4F6AFF" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
          <p style={{ fontSize: 14, fontWeight: 500, color: '#1a1a1a', margin: '0 0 4px' }}>Gainsight</p>
          <p style={{ fontSize: 12, color: '#9ca3af', margin: 0 }}>Health, CTAs, success plans</p>
          {crmConnected === 'gainsight' && <StatusDot connected />}
        </div>

        {/* HubSpot tile */}
        <div onClick={() => window.open('/auth/hubspot/start', '_blank', 'width=500,height=600,popup=yes')} style={TILE_STYLE}>
          <div style={{ width: 36, height: 36, borderRadius: 8, display: 'flex', alignItems: 'center', justifyContent: 'center', margin: '0 auto 10px', background: '#FFF2ED' }}>
            <svg style={{ width: 20, height: 20 }} viewBox="0 0 24 24" fill="none">
              <circle cx="12" cy="12" r="11" fill="#FF7A59"/>
              <path d="M14.5 7.5v3.2a2 2 0 1 0 2 0V7.5h-2zM9.5 11.5a3 3 0 1 0 3 3 3 3 0 0 0-3-3z" fill="#fff"/>
            </svg>
          </div>
          <p style={{ fontSize: 14, fontWeight: 500, color: '#1a1a1a', margin: '0 0 4px' }}>HubSpot</p>
          <p style={{ fontSize: 12, color: '#9ca3af', margin: 0 }}>Contacts, deals, tickets</p>
          {crmConnected === 'hubspot' && <StatusDot connected />}
        </div>
      </div>

      <p style={{ fontSize: 12, color: '#9ca3af', textAlign: 'center', margin: '0 0 24px' }}>
        More CRMs coming soon. You can also connect later from settings.
      </p>

      {cancelText && (
        <p style={{ fontSize: 12, color: '#6b7280', textAlign: 'center', margin: '0 0 16px' }}>{cancelText}</p>
      )}

      <div style={{ marginTop: 28, display: 'flex', flexDirection: 'column' }}>
        <button onClick={onContinue} style={PRIMARY_BTN}>Continue</button>
        <button onClick={onSkip} style={SKIP_BTN}>Skip — I'll connect later</button>
      </div>
    </div>
  );
}

// ── Loading screen ───────────────────────────────────────────────────────────

function LoadingScreen() {
  return (
    <div style={{ textAlign: 'center', padding: '32px 0' }}>
      <Brand />
      <div style={{ display: 'flex', justifyContent: 'center', marginBottom: 16 }}>
        <Spinner size={32} />
      </div>
      <p style={{ fontSize: 16, fontWeight: 500, color: '#1a1a1a', margin: '24px 0 8px' }}>Setting up your workspace...</p>
      <p style={{ fontSize: 14, color: '#9ca3af', margin: 0 }}>Loading your accounts and generating your first briefing.</p>
    </div>
  );
}

// ── Fallback: Manual account entry ───────────────────────────────────────────

function StepManual({ userId, onComplete }) {
  const [textValue, setTextValue] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  async function handleLaunch() {
    const accounts = textValue.split('\n').map(s => s.trim()).filter(Boolean);
    if (accounts.length === 0) {
      setError('Please add at least one account.');
      return;
    }
    setLoading(true);
    setError('');
    try {
      const res = await api.post('/app/onboarding/complete', { accounts, user_id: userId });
      if (!res || !res.ok) {
        const data = res ? await res.json().catch(() => ({})) : {};
        throw new Error(data.error || 'Something went wrong');
      }
      localStorage.setItem('onboarding_complete', 'true');
      onComplete();
    } catch (e) {
      setError(e.message || 'Failed to save. Please try again.');
    } finally {
      setLoading(false);
    }
  }

  return (
    <div>
      <h2 style={HEADING_STYLE}>Which accounts do you manage?</h2>
      <p style={DESC_STYLE}>
        We couldn't match accounts to your email automatically.
        Enter your key accounts below and CS Agent will start monitoring them right away.
      </p>

      <textarea
        value={textValue}
        onChange={e => setTextValue(e.target.value)}
        placeholder={'Enter one account name per line\n\ne.g.\nMeridian Health\nBeacon Financial\nNorthgate Logistics'}
        rows={6}
        style={{
          width: '100%', border: '1px solid #e5e7eb', borderRadius: 12,
          padding: '12px 16px', fontSize: 14, color: '#1a1a1a', resize: 'none',
          fontFamily: 'inherit', outline: 'none', boxSizing: 'border-box',
        }}
      />
      <p style={{ fontSize: 12, color: '#9ca3af', margin: '6px 0 20px' }}>You can update these anytime from settings.</p>

      {error && (
        <p style={{
          fontSize: 14, color: '#dc2626', background: '#fef2f2',
          border: '1px solid #fee2e2', borderRadius: 8, padding: '10px 16px', marginBottom: 16,
        }}>
          {error}
        </p>
      )}

      <button
        onClick={handleLaunch}
        disabled={loading}
        style={{ ...PRIMARY_BTN, opacity: loading ? 0.6 : 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8 }}
      >
        {loading ? <><Spinner /> Launching...</> : 'Launch CS Agent'}
      </button>
    </div>
  );
}

// ── Main wizard ──────────────────────────────────────────────────────────────

export default function OnboardingWizard({ onComplete }) {
  const { user } = useUser();
  const [step, setStep] = useState(1);               // 1, 2, "loading", "fallback"
  const [googleConnected, setGoogleConnected] = useState(false);
  const [crmConnected, setCrmConnected] = useState(null);  // null | "hubspot" | "gainsight"
  const [cancelMessage, setCancelMessage] = useState(null); // null | { provider, text }
  const timeoutRef = useRef(null);
  const cancelTimeoutRef = useRef(null);

  // Listen for OAuth completion and cancel messages
  useEffect(() => {
    const handleMessage = (event) => {
      if (event.origin !== window.location.origin) return;
      if (event.data?.type === "oauth_complete") {
        if (event.data.provider === "google") setGoogleConnected(true);
        else if (event.data.provider === "hubspot") setCrmConnected("hubspot");
        else if (event.data.provider === "gainsight") setCrmConnected("gainsight");
        setCancelMessage(null);
      } else if (event.data?.type === "oauth_canceled") {
        const provider = event.data.provider;
        if (provider === "unknown") return;
        if (cancelTimeoutRef.current) clearTimeout(cancelTimeoutRef.current);
        setCancelMessage({ provider, text: "Connection canceled. Try again when ready." });
        cancelTimeoutRef.current = setTimeout(() => setCancelMessage(null), 3000);
      }
    };
    window.addEventListener("message", handleMessage);
    return () => {
      window.removeEventListener("message", handleMessage);
      if (cancelTimeoutRef.current) clearTimeout(cancelTimeoutRef.current);
    };
  }, []);

  // Auto-resolve accounts when entering loading state
  useEffect(() => {
    if (step !== 'loading') return;
    let cancelled = false;

    // 15-second timeout → fallback
    timeoutRef.current = setTimeout(() => {
      if (!cancelled) setStep('fallback');
    }, 15000);

    async function resolve() {
      try {
        const res = await api.post('/app/onboarding/complete', {
          user_id: user?.id,
          accounts: 'auto',
        });
        if (cancelled) return;
        if (!res || !res.ok) { setStep('fallback'); return; }
        const data = await res.json();
        if (data.accounts_resolved > 0) {
          localStorage.setItem('onboarding_complete', 'true');
          onComplete();
        } else {
          setStep('fallback');
        }
      } catch {
        if (!cancelled) setStep('fallback');
      }
    }

    resolve();
    return () => {
      cancelled = true;
      clearTimeout(timeoutRef.current);
    };
  }, [step, user?.id, onComplete]);

  const userId = user?.id || '';

  return (
    <div
      className="min-h-screen flex flex-col items-center justify-center p-6"
      style={{ fontFamily: "'DM Sans', -apple-system, sans-serif", background: PAGE_BG }}
    >
      <div className="w-full" style={{ maxWidth: 520 }}>
        {step !== 'loading' && <Brand />}
        <div style={CARD_STYLE}>
          {step === 1 && (
            <StepGoogle
              userId={userId}
              connected={googleConnected}
              cancelText={cancelMessage?.provider === 'google' ? cancelMessage.text : null}
              onContinue={() => setStep(2)}
              onSkip={() => setStep(2)}
            />
          )}
          {step === 2 && (
            <StepCRM
              userId={userId}
              crmConnected={crmConnected}
              cancelText={
                cancelMessage && (cancelMessage.provider === 'hubspot' || cancelMessage.provider === 'gainsight')
                  ? cancelMessage.text
                  : null
              }
              onContinue={() => setStep('loading')}
              onSkip={() => setStep('fallback')}
            />
          )}
          {step === 'loading' && <LoadingScreen />}
          {step === 'fallback' && (
            <StepManual userId={userId} onComplete={onComplete} />
          )}
        </div>
      </div>
    </div>
  );
}
