import { createContext, useContext, useEffect, useState } from 'react';
import { useAuth } from '@clerk/clerk-react';
import { setAuthToken } from '../../lib/api';

const AuthReadyContext = createContext(false);

/**
 * Hook for child components to check if the Clerk auth token has been
 * resolved at least once. Returns true after the first getToken() call
 * completes (whether it returned a token or null).
 */
export function useAuthReady() {
  return useContext(AuthReadyContext);
}

export default function AuthTokenProvider({ children }) {
  const { getToken } = useAuth();
  const [isReady, setIsReady] = useState(false);

  useEffect(() => {
    let mounted = true;

    const updateToken = async () => {
      try {
        const token = await getToken();
        if (mounted) setAuthToken(token);
      } catch (e) {
        console.error('Failed to get Clerk token:', e);
      }
      if (mounted && !isReady) setIsReady(true);
    };

    updateToken();
    // Refresh every 50s — Clerk tokens expire in 60s
    const interval = setInterval(updateToken, 50000);

    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, [getToken]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <AuthReadyContext.Provider value={isReady}>
      {children}
    </AuthReadyContext.Provider>
  );
}
