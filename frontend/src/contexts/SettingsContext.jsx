import { createContext } from 'react';

// Default no-op so components work in isolation / tests without a Provider.
const SettingsContext = createContext({
  openSettings: () => {},
});

export default SettingsContext;
