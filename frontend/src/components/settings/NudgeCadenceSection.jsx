import { useEffect, useState } from 'react';
import api from '../../lib/api';

/**
 * C.1 Commit 7: post-call nudge cadence preference.
 *
 * Controls when the transparent nudge (flow C in the scheduler) fires —
 * i.e. how long CS Agent waits after a call before asking for notes
 * when no transcript is available in Drive. Flow A (transcript found)
 * and flow B-skip (CRM already logged the call) are not gated by this.
 *
 * Standalone section so SettingsModal stays focused on the Google flow.
 */

const OPTIONS = [
  { value: 5, label: '5 minutes' },
  { value: 15, label: '15 minutes', isDefault: true },
  { value: 30, label: '30 minutes' },
  { value: 60, label: '60 minutes' },
];

const DEFAULT_CADENCE = 15;

const SECTION_HEADER_STYLE = {
  fontSize: 12,
  fontWeight: 500,
  color: '#6b7280',
  textTransform: 'uppercase',
  letterSpacing: '0.05em',
  margin: '24px 0 8px',
};
const BODY_STYLE = {
  fontSize: 14,
  color: '#6b7280',
  lineHeight: 1.55,
  margin: '0 0 16px',
};
const RADIO_ROW_STYLE = {
  display: 'flex',
  alignItems: 'center',
  gap: 10,
  fontSize: 14,
  color: '#1a1a1a',
  margin: '0 0 8px',
  cursor: 'pointer',
};
const STATUS_STYLE = {
  fontSize: 12,
  color: '#6b7280',
  margin: '8px 0 0',
};
const ERROR_STYLE = {
  fontSize: 13,
  color: '#dc2626',
  background: '#fef2f2',
  border: '1px solid #fee2e2',
  borderRadius: 8,
  padding: '8px 12px',
  margin: '8px 0 0',
};


export default function NudgeCadenceSection() {
  const [cadence, setCadence] = useState(DEFAULT_CADENCE);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  // Initial load
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const res = await api.get('/app/settings/nudge-cadence');
        if (cancelled) return;
        if (!res.ok) { setLoading(false); return; }
        const data = await res.json();
        if (cancelled) return;
        if (OPTIONS.some(o => o.value === data.cadence_minutes)) {
          setCadence(data.cadence_minutes);
        }
      } catch {
        // Silent — keep default
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => { cancelled = true; };
  }, []);

  async function handleChange(next) {
    if (saving || next === cadence) return;
    const previous = cadence;
    setCadence(next);  // optimistic
    setSaving(true);
    setError(null);
    try {
      const res = await api.post('/app/settings/nudge-cadence', { cadence_minutes: next });
      if (!res.ok) {
        setCadence(previous);
        let msg = 'Could not save — please try again.';
        if (res.status === 400) msg = "That's not a valid cadence — please refresh.";
        setError(msg);
      }
    } catch (err) {
      setCadence(previous);
      if (err?.message === 'Unauthorized') {
        setError('Your session has expired. Please refresh the page.');
      } else {
        setError('Could not save — please try again.');
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <section>
      <h3 style={SECTION_HEADER_STYLE}>Post-call nudge cadence</h3>
      <p style={BODY_STYLE}>
        How long CS Agent waits after a call before prompting you for
        notes when no transcript is available.
      </p>
      {loading ? (
        <p style={BODY_STYLE}>Loading…</p>
      ) : (
        <fieldset
          style={{ border: 'none', padding: 0, margin: 0 }}
          aria-label="Post-call nudge cadence"
        >
          {OPTIONS.map(opt => (
            <label key={opt.value} style={RADIO_ROW_STYLE}>
              <input
                type="radio"
                name="nudge-cadence"
                value={opt.value}
                checked={cadence === opt.value}
                disabled={saving}
                onChange={() => handleChange(opt.value)}
              />
              <span>
                {opt.label}
                {opt.isDefault && (
                  <span style={{ color: '#9ca3af', marginLeft: 6 }}>(default)</span>
                )}
              </span>
            </label>
          ))}
        </fieldset>
      )}
      {saving && <p style={STATUS_STYLE}>Saving…</p>}
      {error && <p style={ERROR_STYLE}>{error}</p>}
    </section>
  );
}
