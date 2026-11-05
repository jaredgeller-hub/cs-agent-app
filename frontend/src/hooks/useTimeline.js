import { useState, useEffect, useCallback } from 'react';
import api from '../lib/api';

export default function useTimeline() {
  const [events, setEvents] = useState([]);

  const refresh = useCallback(async () => {
    try {
      const res = await api.get('/app/timeline');
      if (!res || !res.ok) return;
      const data = await res.json();
      setEvents(data.events || []);
    } catch { /* fail silently */ }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  return { events, refresh };
}
