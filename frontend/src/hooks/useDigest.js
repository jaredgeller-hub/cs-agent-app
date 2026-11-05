import { useEffect } from 'react';
import api from '../lib/api';

export default function useDigest(appendMessage) {
  useEffect(() => {
    let cancelled = false;

    async function pollForDigest(attemptsLeft) {
      if (cancelled || attemptsLeft <= 0) {
        if (!cancelled) {
          appendMessage('assistant', '⚠ Today\'s briefing ran into an issue. Try refreshing in a bit, or ask me anything below.', { isError: true });
        }
        return;
      }
      try {
        const res = await api.get('/app/today-digest');
        if (!res || !res.ok || cancelled) return;
        const data = await res.json();
        if (data.digest) {
          appendMessage('assistant', data.digest);
          if (Array.isArray(data.chips) && data.chips.length) {
            appendMessage('chips', '', { chips: data.chips });
          }
        } else if (data.status === 'generating') {
          setTimeout(() => pollForDigest(attemptsLeft - 1), 5000);
        } else {
          appendMessage('assistant', '⚠ Today\'s briefing ran into an issue. Try refreshing in a bit, or ask me anything below.', { isError: true });
        }
      } catch {
        /* fail silently */
      }
    }

    async function load() {
      try {
        const res = await api.get('/app/today-digest');
        if (!res || !res.ok || cancelled) return;
        const data = await res.json();
        if (data.digest) {
          appendMessage('assistant', data.digest);
          if (Array.isArray(data.chips) && data.chips.length) {
            appendMessage('chips', '', { chips: data.chips });
          }
        } else if (data.status === 'generating') {
          setTimeout(() => pollForDigest(11), 5000); // poll up to 12 times (~60s)
        } else {
          appendMessage('assistant', 'Your daily briefing will be ready shortly. In the meantime, ask me about any account — I\'m here to help.');
        }
      } catch {
        /* fail silently */
      }
    }

    load();
    return () => { cancelled = true; };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
}
