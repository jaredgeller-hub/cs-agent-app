/**
 * Regression guard: OnboardingWizard's postMessage listener must reject
 * messages from any origin other than window.location.origin.
 *
 * The fix landed Apr 16 in commit f9225ee (origin check added to listener
 * + popup-side targetOrigin changed from "*" to window.location.origin).
 * This test exists so the listener-side check can't be silently dropped.
 *
 * Pair test: tests/test_oauth_postmessage.py guards the popup side.
 */

import { describe, expect, it, beforeEach, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import OnboardingWizard from '../src/components/onboarding/OnboardingWizard';

// Stub Clerk's useUser hook
vi.mock('@clerk/clerk-react', () => ({
  useUser: () => ({ user: { id: 'user_test_123' } }),
}));

// Stub the api module — onboarding wizard's "loading" effect calls it
vi.mock('../src/lib/api', () => ({
  default: {
    get: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }),
    post: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }),
  },
}));

function dispatchOAuthMessage({ origin, data }) {
  const event = new MessageEvent('message', {
    origin,
    data,
    source: window,
  });
  act(() => {
    window.dispatchEvent(event);
  });
}

function advanceToCRMStep() {
  // Step 1 → Step 2 by clicking "Continue"
  const continueBtn = screen.getAllByRole('button').find((b) => b.textContent === 'Continue');
  act(() => {
    fireEvent.click(continueBtn);
  });
}

describe('OnboardingWizard postMessage listener — origin validation', () => {
  beforeEach(() => {
    // Each test gets a fresh window
  });

  it('rejects message from unauthorized origin (Google)', () => {
    render(<OnboardingWizard onComplete={() => {}} />);
    expect(screen.getByText('Not connected')).toBeInTheDocument();

    dispatchOAuthMessage({
      origin: 'https://evil.example.com',
      data: { type: 'oauth_complete', provider: 'google' },
    });

    // State must NOT have flipped — still showing "Not connected"
    expect(screen.getByText('Not connected')).toBeInTheDocument();
    expect(screen.queryByText('Connected')).toBeNull();
  });

  it('accepts message from same origin (Google)', () => {
    render(<OnboardingWizard onComplete={() => {}} />);
    expect(screen.getByText('Not connected')).toBeInTheDocument();

    dispatchOAuthMessage({
      origin: window.location.origin,
      data: { type: 'oauth_complete', provider: 'google' },
    });

    // googleConnected should now be true → status flips to "Connected"
    expect(screen.getByText('Connected')).toBeInTheDocument();
  });

  it('accepts message from same origin (HubSpot) on step 2', () => {
    render(<OnboardingWizard onComplete={() => {}} />);
    advanceToCRMStep();

    // Step 2 visible — HubSpot tile present, no "Connected" badge yet
    expect(screen.getByText('HubSpot')).toBeInTheDocument();
    expect(screen.queryByText('Connected')).toBeNull();

    dispatchOAuthMessage({
      origin: window.location.origin,
      data: { type: 'oauth_complete', provider: 'hubspot' },
    });

    expect(screen.getByText('Connected')).toBeInTheDocument();
  });

  it('accepts message from same origin (Gainsight) on step 2', () => {
    render(<OnboardingWizard onComplete={() => {}} />);
    advanceToCRMStep();

    expect(screen.getByText('Gainsight')).toBeInTheDocument();
    expect(screen.queryByText('Connected')).toBeNull();

    dispatchOAuthMessage({
      origin: window.location.origin,
      data: { type: 'oauth_complete', provider: 'gainsight' },
    });

    expect(screen.getByText('Connected')).toBeInTheDocument();
  });

  it('rejects same-origin message with missing data.type', () => {
    render(<OnboardingWizard onComplete={() => {}} />);
    expect(screen.getByText('Not connected')).toBeInTheDocument();

    dispatchOAuthMessage({
      origin: window.location.origin,
      data: { provider: 'google' }, // no `type`
    });

    expect(screen.getByText('Not connected')).toBeInTheDocument();
    expect(screen.queryByText('Connected')).toBeNull();
  });

  it('rejects same-origin message with wrong data.type', () => {
    render(<OnboardingWizard onComplete={() => {}} />);
    expect(screen.getByText('Not connected')).toBeInTheDocument();

    dispatchOAuthMessage({
      origin: window.location.origin,
      data: { type: 'something_else', provider: 'google' },
    });

    expect(screen.getByText('Not connected')).toBeInTheDocument();
    expect(screen.queryByText('Connected')).toBeNull();
  });

  it('rejects malicious origin even with correct shape (HubSpot, step 2)', () => {
    render(<OnboardingWizard onComplete={() => {}} />);
    advanceToCRMStep();

    dispatchOAuthMessage({
      origin: 'https://attacker.example.com',
      data: { type: 'oauth_complete', provider: 'hubspot' },
    });

    // HubSpot should NOT show connected
    expect(screen.queryByText('Connected')).toBeNull();
  });
});
