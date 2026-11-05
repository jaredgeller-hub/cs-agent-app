import { useState, useEffect } from 'react';
import api from '../lib/api';

export default function useAccounts() {
  const [accounts, setAccounts] = useState([]);

  async function refresh() {
    try {
      const res = await api.get('/app/accounts');
      if (!res || !res.ok) return;
      const data = await res.json();
      setAccounts(data.accounts || []);
    } catch { /* fail silently */ }
  }

  useEffect(() => { refresh(); }, []);

  return { accounts, refresh };
}
