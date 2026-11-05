import { useState } from 'react';
import useConnectionStatus from '../../lib/useConnectionStatus';
import {
  getExpiredProviderKeys,
  formatExpiryMessage,
  getProviderDisplayName,
  openProviderPopup,
} from '../../lib/connectionExpiry';

export default function ConnectionExpiryBanner({ onOpenSettings }) {
  const { providers } = useConnectionStatus();
  // Dismiss is keyed to the SET of currently expired providers. When
  // the set changes (a provider reconnects, a new one expires), the
  // stored key no longer matches the current set and the banner
  // automatically re-shows. Dismiss is a UI suppression keyed to
  // current state, not a permanent user preference — so it's derived,
  // not synchronized via useEffect.
  const [dismissedSetKey, setDismissedSetKey] = useState(null);

  const expiredKeys = getExpiredProviderKeys(providers);
  const hasExpired = expiredKeys.length > 0;
  const expiredSetKey = expiredKeys.join(',');
  const dismissed = dismissedSetKey !== null && dismissedSetKey === expiredSetKey;

  function dismiss() {
    setDismissedSetKey(expiredSetKey);
  }

  if (!hasExpired || dismissed) return null;

  const message = formatExpiryMessage(expiredKeys);

  return (
    <div
      role="alert"
      aria-live="polite"
      style={{
        background: '#FEF3C7',
        borderBottom: '1px solid #F59E0B',
        color: '#92400E',
        padding: '10px 16px',
        fontSize: 13,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: 12,
        fontFamily: 'inherit',
      }}
    >
      <span>
        {message}
        {expiredKeys.map((key) => (
          <span key={key}>
            {' '}
            <button
              onClick={() => openProviderPopup(key)}
              style={{
                background: 'none', border: 'none', padding: 0,
                color: '#92400E', textDecoration: 'underline',
                cursor: 'pointer', fontSize: 'inherit', fontFamily: 'inherit',
                fontWeight: 600,
              }}
            >
              Reconnect {getProviderDisplayName(key)}
            </button>
          </span>
        ))}
        {onOpenSettings && (
          <>
            {' or '}
            <button
              onClick={onOpenSettings}
              style={{
                background: 'none', border: 'none', padding: 0,
                color: '#92400E', textDecoration: 'underline',
                cursor: 'pointer', fontSize: 'inherit', fontFamily: 'inherit',
              }}
            >
              open settings to manage all connections
            </button>
          </>
        )}
      </span>
      <button
        onClick={dismiss}
        aria-label="Dismiss"
        style={{
          background: 'none', border: 'none', padding: '2px 6px',
          color: '#92400E', cursor: 'pointer', fontSize: 16, fontFamily: 'inherit',
        }}
      >
        ×
      </button>
    </div>
  );
}
