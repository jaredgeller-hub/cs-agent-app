/**
 * Regression guards for NudgeCadenceSection (C.1 Commit 7).
 *
 * Mirrors postcall_draft_card.test.jsx: vitest + RTL + mocked api.
 *
 * Invariants:
 *  1. Renders four radio presets + helper copy.
 *  2. GET returns 30 → 30-minute radio is selected on mount.
 *  3. GET returns 15 (default) → 15-minute radio selected, label shows "(default)".
 *  4. Clicking a different preset calls POST with the exact value and
 *     the new preset becomes selected.
 *  5. POST 500 → selection reverts to prior, inline error visible, user
 *     can retry.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import NudgeCadenceSection from '../src/components/settings/NudgeCadenceSection';
import api from '../src/lib/api';

vi.mock('../src/lib/api', () => ({
  default: { post: vi.fn(), get: vi.fn() },
}));

afterEach(() => {
  vi.clearAllMocks();
});

function mockResponse(body, { ok = true, status = 200 } = {}) {
  return { ok, status, json: () => Promise.resolve(body) };
}


// ── Tests ────────────────────────────────────────────────────────────────────


describe('NudgeCadenceSection — structure', () => {
  it('test_renders_four_presets_and_helper_copy', async () => {
    api.get.mockResolvedValue(mockResponse({ cadence_minutes: 15 }));
    render(<NudgeCadenceSection />);
    // Wait for initial load
    await waitFor(() => {
      expect(screen.getAllByRole('radio').length).toBe(4);
    });
    expect(screen.getByRole('radio', { name: /^5 minutes$/ })).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: /15 minutes/ })).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: /30 minutes/ })).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: /60 minutes/ })).toBeInTheDocument();
    expect(screen.getByText(/How long CS Agent waits/)).toBeInTheDocument();
  });
});


describe('NudgeCadenceSection — initial load', () => {
  it('test_GET_returns_30_selects_30_radio', async () => {
    api.get.mockResolvedValue(mockResponse({ cadence_minutes: 30 }));
    render(<NudgeCadenceSection />);
    await waitFor(() => {
      expect(screen.getByRole('radio', { name: /30 minutes/ })).toBeChecked();
    });
    expect(screen.getByRole('radio', { name: /^5 minutes$/ })).not.toBeChecked();
    expect(screen.getByRole('radio', { name: /15 minutes/ })).not.toBeChecked();
    expect(screen.getByRole('radio', { name: /60 minutes/ })).not.toBeChecked();
  });

  it('test_GET_returns_15_default_label_shown', async () => {
    api.get.mockResolvedValue(mockResponse({ cadence_minutes: 15 }));
    render(<NudgeCadenceSection />);
    await waitFor(() => {
      expect(screen.getByRole('radio', { name: /15 minutes/ })).toBeChecked();
    });
    expect(screen.getByText(/\(default\)/)).toBeInTheDocument();
  });
});


describe('NudgeCadenceSection — saving', () => {
  it('test_click_different_preset_posts_and_updates_selection', async () => {
    api.get.mockResolvedValue(mockResponse({ cadence_minutes: 15 }));
    api.post.mockResolvedValue(mockResponse({ cadence_minutes: 60 }));
    render(<NudgeCadenceSection />);
    await waitFor(() => {
      expect(screen.getByRole('radio', { name: /15 minutes/ })).toBeChecked();
    });

    fireEvent.click(screen.getByRole('radio', { name: /60 minutes/ }));

    await waitFor(() => {
      expect(api.post).toHaveBeenCalledTimes(1);
    });
    expect(api.post).toHaveBeenCalledWith('/app/settings/nudge-cadence', {
      cadence_minutes: 60,
    });
    // After POST completes, 60 is selected and 15 is not.
    await waitFor(() => {
      expect(screen.getByRole('radio', { name: /60 minutes/ })).toBeChecked();
    });
    expect(screen.getByRole('radio', { name: /15 minutes/ })).not.toBeChecked();
  });
});


describe('NudgeCadenceSection — error UX', () => {
  it('test_POST_500_reverts_selection_and_shows_error', async () => {
    api.get.mockResolvedValue(mockResponse({ cadence_minutes: 15 }));
    api.post
      .mockResolvedValueOnce(mockResponse({ detail: 'boom' }, { ok: false, status: 500 }))
      .mockResolvedValueOnce(mockResponse({ cadence_minutes: 30 }));

    render(<NudgeCadenceSection />);
    await waitFor(() => {
      expect(screen.getByRole('radio', { name: /15 minutes/ })).toBeChecked();
    });

    fireEvent.click(screen.getByRole('radio', { name: /30 minutes/ }));

    // Selection reverts to 15, error is rendered.
    await waitFor(() => {
      expect(screen.getByText(/Could not save/)).toBeInTheDocument();
    });
    expect(screen.getByRole('radio', { name: /15 minutes/ })).toBeChecked();
    expect(screen.getByRole('radio', { name: /30 minutes/ })).not.toBeChecked();

    // Retry — second POST succeeds.
    fireEvent.click(screen.getByRole('radio', { name: /30 minutes/ }));
    await waitFor(() => {
      expect(screen.getByRole('radio', { name: /30 minutes/ })).toBeChecked();
    });
    expect(api.post).toHaveBeenCalledTimes(2);
  });
});
