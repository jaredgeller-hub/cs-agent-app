import '@testing-library/jest-dom/vitest';

// jsdom doesn't implement scrollIntoView; stub it so components calling it
// during effects don't crash the test renderer.
if (typeof Element !== 'undefined' && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}
