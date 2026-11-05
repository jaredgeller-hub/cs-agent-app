import { useState } from 'react';
import { SignedIn, SignedOut, SignIn } from '@clerk/clerk-react';
import AppLayout from './components/layout/AppLayout';
import AuthTokenProvider from './components/auth/AuthTokenProvider';
import OnboardingWizard from './components/onboarding/OnboardingWizard';
import { COMPANY_NAME } from './lib/branding';

function AuthedApp() {
  const [onboarded, setOnboarded] = useState(
    localStorage.getItem('onboarding_complete') === 'true'
  );

  if (!onboarded) {
    return <OnboardingWizard onComplete={() => setOnboarded(true)} />;
  }
  return <AppLayout />;
}

export default function App() {
  return (
    <>
      <SignedOut>
        <div style={{
          minHeight: '100vh',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          background: '#ffffff',
          fontFamily: "'DM Sans', sans-serif",
          padding: '24px',
        }}>
          <header style={{ textAlign: 'center', marginBottom: 32 }}>
            <h1 style={{
              fontFamily: "'DM Sans', sans-serif",
              fontSize: 32,
              fontWeight: 700,
              color: '#1a1a1a',
              margin: '0 0 8px',
            }}>
              CS Agent
            </h1>
            <p style={{ fontSize: 14, color: '#6b7280', margin: 0, fontWeight: 400 }}>
              by {COMPANY_NAME}
            </p>
            <p style={{
              fontSize: 15,
              color: '#4a4a4a',
              margin: '12px 0 0',
              fontWeight: 400,
            }}>
              Your AI colleague for Customer Success
            </p>
          </header>

          <main id="main" style={{
            width: '100%',
            maxWidth: 400,
            background: '#fff',
            borderRadius: 12,
            border: '1px solid #e5e7eb',
            boxShadow: '0 2px 12px rgba(0,0,0,0.06)',
            overflow: 'hidden',
          }}>
            <SignIn routing="hash" forceRedirectUrl="/app" />
          </main>

          <footer style={{
            marginTop: 32,
            fontSize: 13,
            color: '#6b7280',
            fontFamily: "'DM Sans', sans-serif",
            textAlign: 'center',
          }}>
            <a
              href="/privacy"
              style={{ color: '#6b7280', textDecoration: 'underline' }}
            >
              Privacy Policy
            </a>
            <span style={{ margin: '0 10px', color: '#d1d5db' }}>·</span>
            <a
              href="/terms"
              style={{ color: '#6b7280', textDecoration: 'underline' }}
            >
              Terms of Service
            </a>
            <span style={{ margin: '0 10px', color: '#d1d5db' }}>·</span>
            <a
              href="/accessibility"
              style={{ color: '#6b7280', textDecoration: 'underline' }}
            >
              Accessibility
            </a>
          </footer>
        </div>
      </SignedOut>

      <SignedIn>
        <AuthTokenProvider>
          <AuthedApp />
        </AuthTokenProvider>
      </SignedIn>
    </>
  );
}
