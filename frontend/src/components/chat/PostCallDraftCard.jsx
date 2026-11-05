import { useContext, useRef, useState } from 'react';
import api from '../../lib/api';
import SettingsContext from '../../contexts/SettingsContext';

/**
 * Dual-field DraftCard variant for post-call timeline entries (C.1 Commit 4).
 *
 * Top section = read-only recap for the CSM to scan.
 * Bottom section = editable timeline entry text; Approve commits it to the
 * connected CRM via /app/actions/execute action_type=log_timeline_entry.
 *
 * Expected props shape matches the structured `alert.postcall_draft` object
 * that Commit 5's scheduler will populate.
 */
export default function PostCallDraftCard({
  recap,
  draftText,
  accountName,
  accountCrmId,
  crmType,
  draftedEventId,
}) {
  const { openSettings } = useContext(SettingsContext);
  const bodyRef = useRef(null);
  const originalTextRef = useRef(draftText);
  const [editing, setEditing] = useState(false);
  const [approving, setApproving] = useState(false);
  const [result, setResult] = useState(null); // {ok, message, errorType?}

  function getEditedText() {
    return bodyRef.current?.textContent || draftText;
  }

  function buildRequestBody() {
    const editedText = getEditedText();
    return {
      action_type: 'log_timeline_entry',
      payload: {
        drafted_event_id: draftedEventId || '',
        account_name: accountName || '',
        account_crm_id: accountCrmId || '',
        crm_type: crmType || '',
      },
      original_text: originalTextRef.current,
      edited_text: editedText,
      was_edited: originalTextRef.current !== editedText,
    };
  }

  async function submit() {
    setApproving(true);
    try {
      const res = await api.post('/app/actions/execute', buildRequestBody());
      let data = null;
      try { data = await res.json(); } catch { data = null; }

      if (!data) {
        setResult({
          ok: false,
          message: 'no response body',
          errorType: 'unknown',
        });
        return;
      }

      if (data.status === 'written') {
        setResult({
          ok: true,
          message: `Timeline entry logged to ${accountName || 'account'}`,
        });
      } else if (data.status === 'error') {
        setResult({
          ok: false,
          message: data.message || 'Write failed',
          errorType: data.error_type || 'unknown',
        });
      } else {
        setResult({
          ok: false,
          message: data.message || 'Write failed',
          errorType: 'unknown',
        });
      }
    } catch (err) {
      setResult({
        ok: false,
        message: err?.message || 'Network error',
        errorType: 'network',
      });
    } finally {
      setApproving(false);
    }
  }

  async function handleApprove() {
    if (approving || result?.ok) return;
    await submit();
  }

  async function handleRetry() {
    if (approving) return;
    setResult(null);
    await submit();
  }

  const approveLabel =
    crmType === 'hubspot' ? 'Approve and log to HubSpot' :
    crmType === 'gainsight' ? 'Approve and log to Gainsight' :
    'Approve and log';

  const errorType = result?.errorType;
  const showRetry = !!result && !result.ok && (errorType === 'network' || errorType === 'unknown');
  const showReconnect = !!result && !result.ok && errorType === 'auth';
  const showSupportHint = !!result && !result.ok && errorType === 'invalid_payload';

  return (
    <div className="msg-row msg-agent">
      <div className="draft-block">
        <div className="draft-label">
          {accountName ? `POST-CALL RECAP — ${accountName}` : 'POST-CALL RECAP'}
        </div>

        <div className="postcall-section">
          <div className="postcall-section-label">Recap</div>
          <div
            className="postcall-recap-body"
            role="region"
            aria-label="Call recap (read-only)"
          >
            {recap}
          </div>
        </div>

        <div className="postcall-section">
          <div className="postcall-section-label">Timeline entry</div>
          <div
            className="draft-body"
            ref={bodyRef}
            contentEditable={editing}
            suppressContentEditableWarning
            role={editing ? 'textbox' : 'region'}
            aria-multiline={editing ? 'true' : undefined}
            aria-label={editing ? 'Timeline entry (editable)' : 'Timeline entry'}
          >
            {draftText}
          </div>
        </div>

        <div className="draft-actions">
          <button
            onClick={() => setEditing(e => !e)}
            disabled={approving || !!result?.ok}
          >
            {editing ? 'Done editing' : 'Edit'}
          </button>
          {!result && (
            <button
              className="draft-approve-btn"
              onClick={handleApprove}
              disabled={approving}
            >
              {approving ? 'Saving…' : approveLabel}
            </button>
          )}
          {result && (
            <span
              className={`draft-result ${result.ok ? 'draft-result-ok' : 'draft-result-err'}`}
            >
              {result.ok ? `✓ ${result.message}` : `✗ Write failed: ${result.message}`}
              {showReconnect && (
                <button
                  type="button"
                  className="draft-reconnect-btn"
                  onClick={() => openSettings()}
                >
                  Reconnect CRM
                </button>
              )}
              {showRetry && (
                <button
                  type="button"
                  className="draft-reconnect-btn"
                  onClick={handleRetry}
                  disabled={approving}
                >
                  {approving ? 'Retrying…' : 'Retry'}
                </button>
              )}
              {showSupportHint && (
                <span className="postcall-support-hint">
                  Something's wrong with this draft. Please refresh and try again.
                </span>
              )}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
