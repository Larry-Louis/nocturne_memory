import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, vi } from 'vitest';

import i18n from '../i18n/index.js';

// Private builds default to zh for the product UI, but the shared test suite
// asserts English strings. Pin tests to en so they stay deterministic.
await i18n.changeLanguage('en');

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.restoreAllMocks();
});
