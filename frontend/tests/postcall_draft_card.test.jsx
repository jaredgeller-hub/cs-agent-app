/**
 * Regression guards for PostCallDraftCard (C.1 Commit 4).
 *
 * Mirrors tests/draft_card.test.jsx: Vitest + @testing-library/react +
 * mocked `api` module. No e2e; no real fetch.
 *
 * Invariants under test:
 *   1. Renders recap section + timeline-entry section + Approve button.
 *   2. Recap body is read-only (role=region). Timeline entry flips to
 *      role=textbox with contenteditable=true after clicking Edit.
 *   3. Happy path (HubSpot): api.post called with exact body; confirmation
 *      "Timeline entry logged to {account}" rendered.
 *   4. Approve with edit: was_edited=true and edited_text carries the change.
 *   5. Approve without edit: was_edited=false, edited_text === original_text.
 *   6. 500 with error_type=auth → Reconnect CRM button → openSettings().
 *   7. 500 with error_type=network → Retry button → re-POST with same body.
 *   8. 500 with error_type=invalid_payload → support hint, no retry button.
 *   9. 500 with error_type=unknown → Retry button (same treatment as network).
 *  10. crmType="hubspot" → "Approve and log to HubSpot" label.
 *  11. crmType="gainsight" → "Approve and log to Gainsight" label.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import PostCallDraftCard from '../src/components/chat/PostCallDraftCard';
import SettingsContext from '../src/contexts/SettingsContext';
import api from '../src/lib/api';

vi.mock('../src/lib/api', () => ({
  default: { post: vi.fn(), get: vi.fn() },
}));

afterEach(() => {
  vi.clearAllMocks();
});

function renderWithSettings(ui, { openSettings = vi.fn() } = {}) {
  const result = render(
    <SettingsContext.Provider value={{ openSettings }}>
      {ui}
    </SettingsContext.Provider>
  );
  return { openSettings, ...result };
}

function mockResponse(body, { ok = true, status = 200 } = {}) {
  return { ok, status, json: () => Promise.resolve(body) };
}

const DEFAULT_PROPS = {
  recap: 'I met with Marcus on the QBR. Renewal committed.',
  draftText: 'QBR held with Marcus Reyes. Meridian confirmed renewal intent.',
  accountName: 'Meridian Health',
  accountCrmId: 'hs-55',
  crmType: 'hubspot',
  draftedEventId: 'evt-drafted-abc',
};


// ── Structure ────────────────────────────────────────────────────────────────

describe('PostCallDraftCard — structure', () => {
  it('test_renders_recap_and_timeline_entry_and_approve_button', () => {
    renderWithSettings(<PostCallDraftCard {...DEFAULT_PROPS} />);
    expect(screen.getByText(/POST-CALL RECAP — Meridian Health/)).toBeInTheDocument();
    expect(screen.getByText('Recap')).toBeInTheDocument();
    expect(screen.getByText('Timeline entry')).toBeInTheDocument();
    expect(screen.getByText(/I met with Marcus on the QBR/)).toBeInTheDocument();
    expect(screen.getByText(/QBR held with Marcus Reyes/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Approve and log to HubSpot/ })).toBeInTheDocument();
  });

  it('test_recap_readonly_timeline_entry_becomes_editable_after_click', () => {
    renderWithSettings(<PostCallDraftCard {...DEFAULT_PROPS} />);
    // Recap body: always role=region, never contenteditable
    const recapRegion = screen.getByLabelText('Call recap (read-only)');
    expect(recapRegion.getAttribute('contenteditable')).not.toBe('true');
    // Timeline entry starts as 'Timeline entry' (region). No textbox yet.
    expect(screen.queryByRole('textbox')).toBeNull();
    // Click Edit
    fireEvent.click(screen.getByRole('button', { name: /^Edit$/ }));
    // Now role=textbox appears
    const textbox = screen.getByRole('textbox');
    expect(textbox).toHaveAttribute('contenteditable', 'true');
    expect(textbox).toHaveAttribute('aria-multiline', 'true');
    expect(textbox).toHaveAttribute('aria-label', 'Timeline entry (editable)');
  });
});


// ── Happy path ──────────────────────────────────────────────────────────────

describe('PostCallDraftCard — happy path', () => {
  it('test_approve_hubspot_success_calls_api_and_shows_confirmation', async () => {
    api.post.mockResolvedValue(mockResponse({
      status: 'written',
      external_system_id: 'hs-note-99',
      crm_type: 'hubspot',
      approved_event_id: 'evt-approved-1',
    }));

    renderWithSettings(<PostCallDraftCard {...DEFAULT_PROPS} />);
    fireEvent.click(screen.getByRole('button', { name: /Approve and log to HubSpot/ }));

    await waitFor(() => {
      expect(screen.getByText(/✓ Timeline entry logged to Meridian Health/)).toBeInTheDocument();
    });

    expect(api.post).toHaveBeenCalledTimes(1);
    expect(api.post).toHaveBeenCalledWith('/app/actions/execute', {
      action_type: 'log_timeline_entry',
      payload: {
        drafted_event_id: 'evt-drafted-abc',
        account_name: 'Meridian Health',
        account_crm_id: 'hs-55',
        crm_type: 'hubspot',
      },
      original_text: DEFAULT_PROPS.draftText,
      edited_text: DEFAULT_PROPS.draftText,
      was_edited: false,
    });
  });

  it('test_approve_with_edit_sends_was_edited_true', async () => {
    api.post.mockResolvedValue(mockResponse({
      status: 'written',
      external_system_id: 'hs-note-100',
      crm_type: 'hubspot',
    }));

    renderWithSettings(<PostCallDraftCard {...DEFAULT_PROPS} />);

    // Enter edit mode
    fireEvent.click(screen.getByRole('button', { name: /^Edit$/ }));
    const textbox = screen.getByRole('textbox');
    // Directly mutate the contentEditable DOM node's text — this is what
    // a real browser would produce on user keystrokes.
    textbox.textContent = 'New edited body text.';

    fireEvent.click(screen.getByRole('button', { name: /Approve and log to HubSpot/ }));

    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    const callArgs = api.post.mock.calls[0][1];
    expect(callArgs.was_edited).toBe(true);
    expect(callArgs.edited_text).toBe('New edited body text.');
    expect(callArgs.original_text).toBe(DEFAULT_PROPS.draftText);
  });

  it('test_approve_without_edit_sends_was_edited_false', async () => {
    api.post.mockResolvedValue(mockResponse({
      status: 'written',
      external_system_id: 'hs-1',
      crm_type: 'hubspot',
    }));

    renderWithSettings(<PostCallDraftCard {...DEFAULT_PROPS} />);
    fireEvent.click(screen.getByRole('button', { name: /Approve and log to HubSpot/ }));

    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    const callArgs = api.post.mock.calls[0][1];
    expect(callArgs.was_edited).toBe(false);
    expect(callArgs.edited_text).toBe(DEFAULT_PROPS.draftText);
  });
});


// ── Error UX ────────────────────────────────────────────────────────────────

describe('PostCallDraftCard — error UX', () => {
  it('test_auth_error_shows_reconnect_button_and_calls_openSettings', async () => {
    api.post.mockResolvedValue(mockResponse(
      {
        status: 'error',
        error_type: 'auth',
        message: 'hubspot write failed: 401 unauthorized',
        approved_event_id: 'evt-approved-err',
      },
      { ok: false, status: 500 },
    ));

    const { openSettings } = renderWithSettings(<PostCallDraftCard {...DEFAULT_PROPS} />);
    fireEvent.click(screen.getByRole('button', { name: /Approve and log to HubSpot/ }));

    await waitFor(() => {
      expect(screen.getByText(/✗ Write failed/)).toBeInTheDocument();
    });

    const reconnect = screen.getByRole('button', { name: /Reconnect CRM/ });
    expect(reconnect).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Retry/ })).toBeNull();

    fireEvent.click(reconnect);
    expect(openSettings).toHaveBeenCalledTimes(1);
  });

  it('test_network_error_shows_retry_button_and_reposts_same_payload', async () => {
    api.post
      .mockResolvedValueOnce(mockResponse(
        {
          status: 'error',
          error_type: 'network',
          message: 'hubspot write failed: 503 bad gateway',
          approved_event_id: 'evt-1',
        },
        { ok: false, status: 500 },
      ))
      .mockResolvedValueOnce(mockResponse({
        status: 'written',
        external_system_id: 'hs-note-retry',
        crm_type: 'hubspot',
      }));

    renderWithSettings(<PostCallDraftCard {...DEFAULT_PROPS} />);
    fireEvent.click(screen.getByRole('button', { name: /Approve and log to HubSpot/ }));

    await waitFor(() => expect(screen.getByText(/✗ Write failed/)).toBeInTheDocument());

    const retry = screen.getByRole('button', { name: /Retry/ });
    expect(retry).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Reconnect/ })).toBeNull();

    fireEvent.click(retry);

    await waitFor(() => {
      expect(screen.getByText(/✓ Timeline entry logged to Meridian Health/)).toBeInTheDocument();
    });
    expect(api.post).toHaveBeenCalledTimes(2);
    // Retry re-POSTs the same payload.
    expect(api.post.mock.calls[1][1]).toEqual(api.post.mock.calls[0][1]);
  });

  it('test_invalid_payload_error_shows_support_hint_and_no_retry_button', async () => {
    api.post.mockResolvedValue(mockResponse(
      {
        status: 'error',
        error_type: 'invalid_payload',
        message: "approved event missing required fields: ['account_crm_id']",
      },
      { ok: false, status: 500 },
    ));

    renderWithSettings(<PostCallDraftCard {...DEFAULT_PROPS} />);
    fireEvent.click(screen.getByRole('button', { name: /Approve and log to HubSpot/ }));

    await waitFor(() => expect(screen.getByText(/✗ Write failed/)).toBeInTheDocument());

    expect(screen.queryByRole('button', { name: /Retry/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /Reconnect/ })).toBeNull();
    expect(screen.getByText(/Something's wrong with this draft/)).toBeInTheDocument();
  });

  it('test_unknown_error_shows_retry_button', async () => {
    api.post.mockResolvedValue(mockResponse(
      {
        status: 'error',
        error_type: 'unknown',
        message: 'weird unclassified error',
      },
      { ok: false, status: 500 },
    ));

    renderWithSettings(<PostCallDraftCard {...DEFAULT_PROPS} />);
    fireEvent.click(screen.getByRole('button', { name: /Approve and log to HubSpot/ }));

    await waitFor(() => expect(screen.getByText(/✗ Write failed/)).toBeInTheDocument());

    expect(screen.getByRole('button', { name: /Retry/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Reconnect/ })).toBeNull();
  });
});


// ── CRM-specific Approve copy ────────────────────────────────────────────────

describe('PostCallDraftCard — CRM-specific Approve button copy', () => {
  it('test_hubspot_button_says_log_to_hubspot', () => {
    renderWithSettings(<PostCallDraftCard {...DEFAULT_PROPS} crmType="hubspot" />);
    expect(screen.getByRole('button', { name: 'Approve and log to HubSpot' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Gainsight/ })).toBeNull();
  });

  it('test_gainsight_button_says_log_to_gainsight', () => {
    renderWithSettings(<PostCallDraftCard {...DEFAULT_PROPS} crmType="gainsight" />);
    expect(screen.getByRole('button', { name: 'Approve and log to Gainsight' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /HubSpot/ })).toBeNull();
  });
});
