import { useState, useCallback, useRef } from 'react';
import api, { ApiTimeoutError, ApiNetworkError } from '../lib/api';

export default function useChat() {
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(false);
  const historyRef = useRef([]); // [{role, content}]

  const appendMessage = useCallback((role, content, extra = {}) => {
    const msg = { id: Date.now() + Math.random(), role, content, ...extra };
    setMessages(prev => [...prev, msg]);
    return msg;
  }, []);

  const sendMessage = useCallback(async (text) => {
    if (!text.trim() || loading) return;

    appendMessage('user', text);
    historyRef.current = [...historyRef.current, { role: 'user', content: text }];
    if (historyRef.current.length > 20) historyRef.current = historyRef.current.slice(-20);

    setLoading(true);
    let res;
    let requestId;
    try {
      res = await api.post('/chat', {
        message: text,
        history: historyRef.current.slice(-20),
      });
      requestId = res.requestId || res.headers?.get?.('x-request-id') || undefined;
      const data = await res.json();
      const reply = data.reply || '';
      if (reply) appendMessage('assistant', reply);
      if (data.drafts && data.drafts.length > 0) {
        for (const draft of data.drafts) {
          appendMessage('draft', draft.content || '', {
            label: draft.label,
            actionType: draft.action_type || undefined,
            actionPayload: draft.payload || {},
            accountName: draft.account_name || undefined,
          });
        }
      }
      historyRef.current = [...historyRef.current, { role: 'assistant', content: reply }];
      if (historyRef.current.length > 20) historyRef.current = historyRef.current.slice(-20);
    } catch (err) {
      // Fall back through typed errors first, then string-matched
      // 'Unauthorized' (kept that way for back-compat with api.js),
      // then SyntaxError from res.json on a non-JSON body, then the
      // catch-all. Each branch surfaces requestId when available so
      // bug reports can include it.
      const rid = err?.requestId || requestId;
      const ridSuffix = rid ? ` (request id: ${rid})` : '';
      let userMsg;
      let errorType;

      if (err instanceof ApiTimeoutError) {
        errorType = 'timeout';
        userMsg = `This is taking longer than expected. Please retry.${ridSuffix}`;
      } else if (err?.message === 'Unauthorized') {
        errorType = 'unauthorized';
        userMsg = 'Your session has expired. Please refresh the page.';
      } else if (err instanceof SyntaxError) {
        errorType = 'parse_error';
        userMsg = `We could not read the server's response. Please retry.${ridSuffix}`;
      } else if (err instanceof ApiNetworkError) {
        errorType = 'network';
        userMsg = `Network error. Please check your connection and retry.${ridSuffix}`;
      } else {
        errorType = 'unknown';
        userMsg = `Something unexpected happened. Please retry.${ridSuffix}`;
      }

      console.error('[chat] request failed', {
        requestId: rid,
        errorType,
        errorName: err?.name,
        errorMessage: err?.message,
      });
      appendMessage('assistant', userMsg);
    } finally {
      setLoading(false);
    }
  }, [loading, appendMessage]);

  return { messages, loading, sendMessage, appendMessage };
}
