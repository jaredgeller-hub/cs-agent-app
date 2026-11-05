import api from '../../lib/api';

/**
 * Click handler for the "Manage subscription" menu item. Hits
 * /app/billing/portal-session and opens the returned Stripe Billing Portal
 * URL in a new tab.
 *
 * Separated from the JSX because Clerk's <UserButton.MenuItems /> does
 * shallow child-type matching — wrapping <UserButton.Action /> inside a
 * custom component is silently filtered out (Clerk logs a dev warning).
 * The menu item JSX lives inline in AppLayout.jsx; this module provides
 * the testable click-behavior piece plus the visibility predicate.
 */
export async function openBillingPortal(opener = (url) => window.open(url, '_blank', 'noopener')) {
  // opener injectable for tests — default opens in new tab with noopener
  // (best-practice isolation between the opened tab and our origin).
  try {
    const res = await api.post('/app/billing/portal-session');
    if (!res || !res.ok) return { ok: false, reason: `http_${res?.status ?? 'unknown'}` };
    const data = await res.json();
    if (data?.url) {
      opener(data.url);
      return { ok: true };
    }
    return { ok: false, reason: 'no_url' };
  } catch (err) {
    return { ok: false, reason: err?.message || 'exception' };
  }
}

/**
 * Visibility predicate for the "Manage subscription" menu item. Hidden for
 * users who have never paid (no stripe_customer_id). The Portal endpoint
 * would 400 for them anyway; hiding the item avoids a dead click.
 */
export function isManageSubscriptionVisible(hasStripeCustomer) {
  return hasStripeCustomer === true;
}
