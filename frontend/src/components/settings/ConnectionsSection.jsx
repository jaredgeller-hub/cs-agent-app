import { useState } from 'react';
import useConnectionStatus from '../../lib/useConnectionStatus';
import api from '../../lib/api';
import {
  getGoogleGroupState,
  getProviderDisplayName,
  openProviderPopup,
  STATE_COLORS,
} from '../../lib/connectionExpiry';

/**
 * Settings v1 (A): the Connections section.
 *
 * The single place a user manages their data connections, now that
 * Clerk's own Connected-accounts section is hidden (AppLayout). It must
 * read as "this is where connections live" even when everything is
 * healthy and shows no action links.
 *
 * Renders:
 *   1. Google group — one dot for worst-of(gmail, calendar), label
 *      "Google", sub-copy naming Gmail, Calendar, and Drive. There is no
 *      independent Drive signal (the wire dict has no drive key), so the
 *      copy never implies Drive has its own status. One dot, one grant,
 *      one reconnect action.
 *   2. CRM rows — hubspot and gainsight, but only those whose state is
 *      not "missing". Whenever ANY CRM is missing, a single "Connect a CRM"
 *      control appears below the rows; it expands to offer the missing CRMs
 *      (so a user can always reconnect after a disconnect, or add a second
 *      CRM). With zero CRMs connected, a muted lead line states the fact
 *      above the control.
 *
 * Reconnect is degraded-only: the affordance shows only when a row is
 * "expired" or "missing". A healthy row shows the dot and "Connected"
 * with no action. The Google reconnect hits /auth/google/start with NO
 * scope=write — the read/data grant only. The write grant stays behind
 * the Actions toggle's separate "Continue to Google" flow.
 *
 * Self-contained like NudgeCadenceSection — consumes the shared
 * useConnectionStatus hook, takes no props. After any reconnect popup
 * completes, the hook refetches on its own (oauth_complete postMessage),
 * so the dots heal without extra wiring here.
 */

const SECTION_HEADER_STYLE = {
  fontSize: 12, fontWeight: 500, color: '#6b7280',
  textTransform: 'uppercase', letterSpacing: '0.05em', margin: '0 0 12px',
};
const BODY_STYLE = { fontSize: 14, color: '#6b7280', lineHeight: 1.55, margin: '0 0 12px' };
const ROW_STYLE = {
  display: 'flex', alignItems: 'center', gap: 8,
  fontSize: 14, color: '#1a1a1a', margin: '0 0 4px',
};
const LABEL_STYLE = { fontWeight: 500 };
const SUBCOPY_STYLE = { fontSize: 13, color: '#9ca3af', margin: '0 0 16px', lineHeight: 1.5 };
const MUTED_LINE_STYLE = { fontSize: 14, color: '#9ca3af', margin: '0 0 4px' };
const RECONNECT_BTN_STYLE = {
  marginLeft: 'auto', background: 'none', border: 'none', padding: 0,
  color: '#1a1a1a', textDecoration: 'underline', cursor: 'pointer',
  fontSize: 13, fontWeight: 600, fontFamily: 'inherit',
};
const STATUS_TEXT_STYLE = { marginLeft: 'auto', fontSize: 13, color: '#9ca3af' };
// Secondary/muted on purpose — disconnect is reversible, so it must NOT read
// like the destructive red of Delete-my-data. Sits after RowAction's
// auto-pushed status/Reconnect with a small fixed gap.
const DISCONNECT_BTN_STYLE = {
  marginLeft: 12, background: 'none', border: 'none', padding: 0,
  color: '#9ca3af', textDecoration: 'underline', cursor: 'pointer',
  fontSize: 13, fontWeight: 500, fontFamily: 'inherit',
};
const CONFIRM_BOX_STYLE = {
  background: '#f9fafb', border: '1px solid #e5e7eb', borderRadius: 8,
  padding: '10px 12px', margin: '4px 0 12px',
};
const CONFIRM_COPY_STYLE = { fontSize: 13, color: '#4b5563', lineHeight: 1.5, margin: '0 0 10px' };
const CONFIRM_ACTIONS_STYLE = { display: 'flex', alignItems: 'center', gap: 16 };
const CONFIRM_DISCONNECT_STYLE = {
  background: 'none', border: 'none', padding: 0, color: '#4b5563',
  textDecoration: 'underline', cursor: 'pointer', fontSize: 13,
  fontWeight: 600, fontFamily: 'inherit',
};
const CONFIRM_CANCEL_STYLE = {
  background: 'none', border: 'none', padding: 0, color: '#9ca3af',
  cursor: 'pointer', fontSize: 13, fontWeight: 500, fontFamily: 'inherit',
};
const DISCONNECT_ERROR_STYLE = {
  fontSize: 13, color: '#dc2626', margin: '0 0 12px',
};
// Connect-a-CRM control — same muted underlined-link language as the other
// secondary actions in this file (deliberately not a loud button). Collapsed
// is a single standalone link; expanded reuses the confirm-box shell.
const CONNECT_LINK_STYLE = {
  display: 'block', background: 'none', border: 'none', padding: 0,
  color: '#9ca3af', textDecoration: 'underline', cursor: 'pointer',
  fontSize: 13, fontWeight: 500, fontFamily: 'inherit', margin: '4px 0 0',
};
const CONNECT_OPTIONS_STYLE = {
  display: 'flex', flexDirection: 'column', alignItems: 'flex-start',
  gap: 8, margin: '0 0 10px',
};
const CONNECT_OPTION_STYLE = {
  background: 'none', border: 'none', padding: 0, color: '#4b5563',
  textDecoration: 'underline', cursor: 'pointer', fontSize: 13,
  fontWeight: 600, fontFamily: 'inherit', textAlign: 'left',
};

function Dot({ state, okColor }) {
  // okColor overrides the connected (ok) color for brand-specific rows
  // (e.g. Slack purple). All other states use the shared STATE_COLORS map.
  const background =
    state === 'ok' && okColor
      ? okColor
      : STATE_COLORS[state] || STATE_COLORS.error;
  return (
    <span
      data-state={state}
      style={{
        width: 8, height: 8, borderRadius: '50%',
        background,
        flexShrink: 0, display: 'inline-block',
      }}
    />
  );
}

// Degraded-only action. Reconnect shows for "expired" or "missing";
// "ok" shows a muted "Connected"; "error" (transient check failure)
// shows a muted "Checking..." and no action.
function RowAction({ state, onReconnect }) {
  if (state === 'expired' || state === 'missing') {
    return (
      <button type="button" onClick={onReconnect} style={RECONNECT_BTN_STYLE}>
        Reconnect
      </button>
    );
  }
  const text = state === 'ok' ? 'Connected' : state === 'error' ? 'Checking...' : '';
  return text ? <span style={STATUS_TEXT_STYLE}>{text}</span> : null;
}

// One CRM row with its own Disconnect affordance + in-section confirm step.
// Disconnect shows only when a connection row exists (state "ok" or "expired");
// never on "missing" (that row isn't even rendered) or "error". Confirm copy is
// deliberately reversible — disconnect deletes a stored token, not the grant.
//
// On success we call onDisconnected() (the hook's refetch) because a disconnect
// emits NO oauth_complete postMessage; without an explicit refetch the dot
// would keep showing "Connected" until the next window focus.
function CrmRow({ providerKey, state, onDisconnected, okColor, allowDisconnect = true }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  // allowDisconnect lets a row opt out of the Disconnect affordance (e.g.
  // Slack, which has no self-service disconnect endpoint). Such rows are
  // reconnect-only, mirroring the degraded-only Reconnect pattern.
  const canDisconnect = allowDisconnect && (state === 'ok' || state === 'expired');
  const name = getProviderDisplayName(providerKey);

  async function handleConfirm() {
    setBusy(true);
    setError(null);
    try {
      const res = await api.post('/app/settings/disconnect-crm', {
        provider: providerKey,
        confirm: true,
      });
      if (!res.ok) {
        setError('Could not disconnect, please try again.');
        setBusy(false);
        return;
      }
      // Success — collapse the confirm step and heal the dots immediately.
      setConfirming(false);
      setBusy(false);
      onDisconnected();
    } catch (err) {
      setBusy(false);
      // 401 routes to the existing session-expired handling (same wording as
      // NudgeCadenceSection); any other failure leaves the row unchanged.
      if (err?.message === 'Unauthorized') {
        setError('Your session has expired. Please refresh the page.');
      } else {
        setError('Could not disconnect, please try again.');
      }
    }
  }

  return (
    <div>
      <div style={ROW_STYLE}>
        <Dot state={state} okColor={okColor} />
        <span style={LABEL_STYLE}>{name}</span>
        <RowAction state={state} onReconnect={() => openProviderPopup(providerKey)} />
        {canDisconnect && !confirming && (
          <button
            type="button"
            onClick={() => { setConfirming(true); setError(null); }}
            style={DISCONNECT_BTN_STYLE}
          >
            Disconnect
          </button>
        )}
      </div>

      {confirming && (
        <div style={CONFIRM_BOX_STYLE}>
          <p style={CONFIRM_COPY_STYLE}>
            CS Agent will stop using your {name} data. You can reconnect anytime.
          </p>
          <div style={CONFIRM_ACTIONS_STYLE}>
            <button
              type="button"
              onClick={handleConfirm}
              disabled={busy}
              style={CONFIRM_DISCONNECT_STYLE}
            >
              Disconnect
            </button>
            <button
              type="button"
              onClick={() => { setConfirming(false); setError(null); }}
              disabled={busy}
              style={CONFIRM_CANCEL_STYLE}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {error && <p style={DISCONNECT_ERROR_STYLE} role="alert">{error}</p>}
    </div>
  );
}

// Single "Connect a CRM" control that expands to reveal the CRMs that are
// missing (state === "missing"). One control rather than a button per CRM so
// it scales to Salesforce/Planhat later without growing a row each. It shows
// whenever ANY CRM is missing, so a user with one CRM connected can still add
// the second from Settings.
//
// No backend call: each option reuses openProviderPopup(key) — the same
// /auth/{provider}/start OAuth popup the Reconnect path uses. After the popup
// completes the hook auto-refetches on the oauth_complete postMessage, so the
// dots heal on their own; unlike disconnect, connect needs no manual refetch.
function ConnectCrmControl({ missingKeys }) {
  const [expanded, setExpanded] = useState(false);

  if (!missingKeys || missingKeys.length === 0) return null;

  if (!expanded) {
    return (
      <button type="button" onClick={() => setExpanded(true)} style={CONNECT_LINK_STYLE}>
        Connect a CRM
      </button>
    );
  }

  return (
    <div style={CONFIRM_BOX_STYLE}>
      <div style={CONNECT_OPTIONS_STYLE}>
        {missingKeys.map((k) => (
          <button
            key={k}
            type="button"
            onClick={() => openProviderPopup(k)}
            style={CONNECT_OPTION_STYLE}
          >
            Connect {getProviderDisplayName(k)}
          </button>
        ))}
      </div>
      <button
        type="button"
        onClick={() => setExpanded(false)}
        style={CONFIRM_CANCEL_STYLE}
      >
        Cancel
      </button>
    </div>
  );
}

export default function ConnectionsSection() {
  const { providers, refetch } = useConnectionStatus();

  if (!providers) {
    return (
      <section>
        <h3 style={SECTION_HEADER_STYLE}>Connections</h3>
        <p style={BODY_STYLE}>Loading…</p>
      </section>
    );
  }

  const googleState = getGoogleGroupState(providers);

  // CRM rows: only those actually connected (state !== "missing"). An
  // "error" CRM (transient check failure) still renders a CrmRow — it is not
  // a confirmed absence, so it is NOT offered in the Connect control.
  const crmKeys = ['hubspot', 'gainsight'].filter(
    (k) => providers[k] && providers[k] !== 'missing',
  );
  // Connectable-absent: only the literal "missing" state. An "expired" CRM
  // already renders a CrmRow with Reconnect, so it is excluded here too.
  const missingCrmKeys = ['hubspot', 'gainsight'].filter(
    (k) => providers[k] === 'missing',
  );
  const noCrm = crmKeys.length === 0;

  return (
    <section>
      <h3 style={SECTION_HEADER_STYLE}>Connections</h3>

      <div style={ROW_STYLE}>
        <Dot state={googleState} />
        <span style={LABEL_STYLE}>Google</span>
        <RowAction state={googleState} onReconnect={() => openProviderPopup('google')} />
      </div>
      <p style={SUBCOPY_STYLE}>Gmail, Calendar, and Drive</p>

      {crmKeys.map((k) => (
        <CrmRow
          key={k}
          providerKey={k}
          state={providers[k]}
          onDisconnected={refetch}
        />
      ))}

      {/* Zero-connected: still state the fact, with the Connect control under
          it. With at least one CRM connected, no lead line — just the control. */}
      {noCrm && <p style={MUTED_LINE_STYLE}>No CRM connected yet.</p>}
      {missingCrmKeys.length > 0 && (
        <ConnectCrmControl missingKeys={missingCrmKeys} />
      )}

      {/* Communications - Slack. A separate group from the CRM rows because
          Slack is a communications connector, not a CRM. Always rendered so a
          disconnected (missing) Slack still offers a Connect/Reconnect path.
          Reconnect-only: no self-service disconnect endpoint for Slack yet. */}
      <h3 style={{ ...SECTION_HEADER_STYLE, margin: '20px 0 12px' }}>Communications</h3>
      <CrmRow
        providerKey="slack"
        state={providers.slack || 'missing'}
        onDisconnected={refetch}
        okColor="#4A154B"
        allowDisconnect={false}
      />
      <CrmRow
        providerKey="zoom"
        state={providers.zoom || 'missing'}
        onDisconnected={refetch}
        okColor="#2D8CFF"
        allowDisconnect={false}
      />
    </section>
  );
}
