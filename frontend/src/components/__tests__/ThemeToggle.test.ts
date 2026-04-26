/**
 * ThemeToggle.test.ts
 *
 * Mounts ThemeToggle, clicks it, and asserts:
 *   1. appStore.darkMode flips to the opposite value.
 *   2. The new value is persisted in localStorage.
 *
 * Runs under vitest with jsdom environment (DOM is required to mount
 * Vue components and exercise localStorage).
 *
 * @vitest-environment jsdom
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { useAppStore } from '../../store/app';
import ThemeToggle from '../ThemeToggle.vue';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
const STORAGE_KEY = 'argus.ui';

function readStoredDarkMode(): boolean | undefined {
  const raw = localStorage.getItem(STORAGE_KEY);
  if (!raw) return undefined;
  try {
    return (JSON.parse(raw) as { darkMode?: boolean }).darkMode;
  } catch {
    return undefined;
  }
}

// ---------------------------------------------------------------------------
// Global stubs for Ant Design components.
// ATooltip: renders children as-is.
// AButton: renders a real <button> element and forwards its native click to
//          the onClick attr so ThemeToggle's @click handler fires.
// Icon stubs: trivial spans.
// ---------------------------------------------------------------------------
const globalStubs = {
  ATooltip: {
    inheritAttrs: false,
    template: '<slot />',
  },
  AButton: {
    inheritAttrs: true,
    template: '<button v-bind="$attrs"><slot /><slot name="icon" /></button>',
  },
  BulbFilled:   { template: '<span data-testid="icon-filled" />' },
  BulbOutlined: { template: '<span data-testid="icon-outlined" />' },
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------
describe('ThemeToggle component', () => {
  beforeEach(() => {
    // Fresh pinia + clear localStorage before each test.
    setActivePinia(createPinia());
    localStorage.clear();
  });

  it('flips appStore.darkMode from its initial value when clicked', async () => {
    const appStore = useAppStore();
    const initialDark = appStore.darkMode;

    const wrapper = mount(ThemeToggle, { global: { stubs: globalStubs } });

    await wrapper.find('button').trigger('click');

    expect(appStore.darkMode).toBe(!initialDark);
  });

  it('persists the toggled darkMode value to localStorage', async () => {
    const appStore = useAppStore();
    const initialDark = appStore.darkMode;

    const wrapper = mount(ThemeToggle, { global: { stubs: globalStubs } });

    await wrapper.find('button').trigger('click');

    const stored = readStoredDarkMode();
    expect(stored).toBe(!initialDark);
  });

  it('flips back to original value on a second click', async () => {
    const appStore = useAppStore();
    const initialDark = appStore.darkMode;

    const wrapper = mount(ThemeToggle, { global: { stubs: globalStubs } });

    await wrapper.find('button').trigger('click');
    await wrapper.find('button').trigger('click');

    expect(appStore.darkMode).toBe(initialDark);
    expect(readStoredDarkMode()).toBe(initialDark);
  });
});
