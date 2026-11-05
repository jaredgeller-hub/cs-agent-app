import useConnectionStatus from '../../lib/useConnectionStatus';
import { STATE_COLORS, openProviderPopup } from '../../lib/connectionExpiry';

const PROVIDER_LABELS = {
  gmail:     'Gmail',
  calendar:  'Calendar',
  hubspot:   'HubSpot',
  gainsight: 'Gainsight',
};

// Maps a dot's provider key to the openProviderPopup key. gmail/calendar
// both heal via one /auth/google/start popup (a single Google grant covers
// both scopes), so both map to 'google'.
const POPUP_PROVIDER = {
  gmail:     'google',
  calendar:  'google',
  hubspot:   'hubspot',
  gainsight: 'gainsight',
};

function dotStyle(color) {
  return {
    width: 6, height: 6, borderRadius: '50%',
    background: color, flexShrink: 0,
  };
}

export default function ConnectionStatus() {
  const { providers: connections } = useConnectionStatus();

  if (!connections) return null;

  // Render Gmail, Calendar, plus whichever CRM has a non-missing state.
  // If both CRMs are missing, render nothing for CRM (mirrors prior
  // behavior where the CRM dot only appeared when connected).
  const items = [
    { key: 'gmail',    label: PROVIDER_LABELS.gmail,    state: connections.gmail },
    { key: 'calendar', label: PROVIDER_LABELS.calendar, state: connections.calendar },
  ];

  const gainsightState = connections.gainsight;
  const hubspotState = connections.hubspot;
  if (gainsightState && gainsightState !== 'missing') {
    items.push({ key: 'gainsight', label: PROVIDER_LABELS.gainsight, state: gainsightState });
  } else if (hubspotState && hubspotState !== 'missing') {
    items.push({ key: 'hubspot', label: PROVIDER_LABELS.hubspot, state: hubspotState });
  }

  return (
    <div style={{ display: 'flex', gap: 10, padding: '4px 0', fontSize: 11, color: 'var(--text-muted)' }}>
      {items.map(({ key, label, state }) => {
        const color = STATE_COLORS[state] || STATE_COLORS.error;
        const isExpired = state === 'expired';
        const isMissing = state === 'missing';
        const isError = state === 'error';
        const showLink = isExpired || isMissing;
        const linkText = isExpired ? 'Reconnect' : (isMissing ? 'Connect' : '');

        return (
          <span
            key={key}
            style={{ display: 'flex', alignItems: 'center', gap: 4 }}
            title={
              isExpired ? 'Reconnect required' :
              isMissing ? 'Not connected' :
              isError   ? 'Checking...' : 'Connected'
            }
          >
            <span style={dotStyle(color)} />
            {label}
            {showLink && (
              // Popup (not same-tab nav): a plain <a href> navigated the
              // current tab to /auth/<provider>/start, leaving the app and
              // stranding the user on the callback close-me screen with no
              // window.opener for postMessage (F-4). openProviderPopup opens
              // the standard popup; the shared useConnectionStatus hook
              // refetches on the oauth_complete message, updating this dot.
              <button
                type="button"
                onClick={() => openProviderPopup(POPUP_PROVIDER[key])}
                style={{
                  marginLeft: 4,
                  background: 'none', border: 'none', padding: 0,
                  color: isExpired ? '#C0392B' : 'var(--text-muted)',
                  textDecoration: 'underline',
                  cursor: 'pointer',
                  fontSize: 'inherit', fontFamily: 'inherit',
                }}
              >
                {linkText}
              </button>
            )}
          </span>
        );
      })}
    </div>
  );
}
