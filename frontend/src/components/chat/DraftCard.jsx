import { useContext, useState, useRef } from 'react';
import api from '../../lib/api';
import SettingsContext from '../../contexts/SettingsContext';

export default function DraftCard({ label, content, actionType, actionPayload, accountName }) {
  const { openSettings } = useContext(SettingsContext);
  const bodyRef = useRef(null);
  const originalTextRef = useRef(content);
  const [editing, setEditing] = useState(false);
  const [approving, setApproving] = useState(false);
  const [result, setResult] = useState(null); // {ok, message, kind?}

  function getEditedText() {
    return bodyRef.current?.textContent || content;
  }

  const isEmailDraft = actionType === 'email_draft' || (actionType || '').startsWith('email');
  const isSlackDraft = actionType === 'slack_message';

  async function handleApprove() {
    if (approving || result?.ok) return;
    setApproving(true);
    const editedText = getEditedText();

    // Slack send: approve-triggered, hits the dedicated /app/slack/send
    // endpoint (not /app/actions/execute). The chat tool only drafts; this
    // approval is what actually sends, via the CSM's own Slack token.
    if (isSlackDraft) {
      try {
        const res = await api.post('/app/slack/send', {
          recipient: actionPayload?.recipient || '',
          message: editedText,
        });
        let data = null;
        try { data = await res.json(); } catch { data = null; }
        if (data && data.status === 'sent') {
          setResult({ ok: true, message: `Sent to ${data.to || actionPayload?.recipient || 'Slack'}` });
        } else {
          setResult({ ok: false, message: _failureMessage(actionType, data?.error) });
        }
      } catch (err) {
        setResult({ ok: false, message: _failureMessage(actionType, err?.message) });
      } finally {
        setApproving(false);
      }
      return;
    }

    // Clipboard copy BEFORE async POST — user gesture required.
    // Harmless if the backend also sent the email; user has a copy either way.
    if (isEmailDraft) {
      try {
        await navigator.clipboard.writeText(editedText);
      } catch { /* clipboard may fail in non-secure contexts */ }
    }

    try {
      const res = await api.post('/app/actions/execute', {
        action_type: actionType,
        payload: { ...actionPayload, body: editedText },
        original_text: originalTextRef.current,
        edited_text: editedText,
        was_edited: originalTextRef.current !== editedText,
      });

      let data = null;
      try {
        data = await res.json();
      } catch {
        data = null;
      }

      // HTTP-level failure with unparseable body, or no body at all.
      if (!data) {
        setResult({ ok: false, message: _failureMessage(actionType) });
        return;
      }

      // Branch on body-level status field (7A contract).
      if (data.status === 'success') {
        setResult({
          ok: true,
          message: isEmailDraft
            ? 'Copied to clipboard!'
            : _successMessage(actionType, actionPayload, accountName),
        });
      } else if (data.status === 'sent') {
        const to = data.to || actionPayload?.to || 'recipient';
        setResult({ ok: true, message: `Sent to ${to}` });
      } else if (data.status === 'scope_revoked') {
        setResult({
          ok: false,
          kind: 'scope_revoked',
          message: 'Send failed — Google permission expired',
        });
      } else {
        setResult({ ok: false, message: _failureMessage(actionType, data.message) });
      }
    } catch (err) {
      setResult({ ok: false, message: _failureMessage(actionType, err?.message) });
    } finally {
      setApproving(false);
    }
  }

  return (
    <div className="msg-row msg-agent">
      <div className="draft-block">
        <div className="draft-label">{label || 'Draft'}</div>
        {isSlackDraft && actionPayload?.recipient && (
          <div className="draft-meta">
            <div className="draft-meta-row">
              <span className="draft-meta-label">TO</span>
              <span className="draft-meta-value">{actionPayload.recipient}</span>
            </div>
          </div>
        )}
        {isEmailDraft && actionPayload?.to && (
          <div className="draft-meta">
            <div className="draft-meta-row">
              <span className="draft-meta-label">TO</span>
              <span className="draft-meta-value">{actionPayload.to}</span>
            </div>
            {actionPayload.subject && (
              <div className="draft-meta-row">
                <span className="draft-meta-label">SUBJECT</span>
                <span className="draft-meta-value">{actionPayload.subject}</span>
              </div>
            )}
          </div>
        )}
        <div
          className="draft-body"
          ref={bodyRef}
          contentEditable={editing}
          suppressContentEditableWarning
        >
          {content}
        </div>
        <div className="draft-actions">
          <button
            onClick={() => setEditing(e => !e)}
            disabled={!!result?.ok}
          >
            {editing ? 'Done editing' : 'Edit draft'}
          </button>
          {actionType && !result && (
            <button
              className="draft-approve-btn"
              onClick={handleApprove}
              disabled={approving}
            >
              {approving ? 'Saving…' : 'Approve'}
            </button>
          )}
          {result && (
            <span
              className={`draft-result ${result.ok ? 'draft-result-ok' : 'draft-result-err'}`}
            >
              {result.ok ? `✓ ${result.message}` : `✗ ${result.message}`}
              {result.kind === 'scope_revoked' && (
                <button
                  type="button"
                  className="draft-reconnect-btn"
                  onClick={() => openSettings()}
                >
                  Reconnect
                </button>
              )}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

function _successMessage(actionType, payload, accountName) {
  const acct = accountName || payload?.company_id || 'account';
  switch (actionType) {
    case 'log_note':    return `Note logged to ${acct}`;
    case 'create_task': return `Task created: ${payload?.title || acct}`;
    case 'update_deal': return `Deal updated`;
    case 'create_contact': return `Contact created: ${payload?.email || ''}`;
    default:            return 'Action saved';
  }
}

function _failureMessage(actionType, message) {
  const isEmail = actionType === 'email_draft' || (actionType || '').startsWith('email');
  if (isEmail) {
    return message ? `Failed to save draft: ${message}` : 'Failed to save draft';
  }
  if (actionType === 'slack_message') {
    return message ? `Failed to send to Slack: ${message}` : 'Failed to send to Slack';
  }
  if (['log_note', 'create_task', 'update_deal', 'create_contact'].includes(actionType)) {
    return message ? `Failed to write to HubSpot: ${message}` : 'Failed to write to HubSpot';
  }
  return message ? `Action failed: ${message}` : 'Action failed';
}
