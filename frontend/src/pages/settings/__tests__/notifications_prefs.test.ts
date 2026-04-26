/**
 * notifications_prefs.test.ts
 *
 * Pins the per-user "Email preferences" section on Notifications.vue
 * (#108). Verifies:
 *
 *   1. On mount the page calls ``getNotificationPrefs`` and the
 *      returned values populate the five toggles.
 *   2. The save button is disabled until at least one toggle differs
 *      from the loaded baseline.
 *   3. Clicking save calls ``putNotificationPrefs`` with the dirty
 *      payload AND the success toast fires.
 *
 * The five toggles are stubbed as plain ``<input type="checkbox">`` so
 * v-model:checked round-trips against jsdom without dragging in real
 * Ant Design markup.
 *
 * @vitest-environment jsdom
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createI18n } from 'vue-i18n';

// ---------------------------------------------------------------------------
// Module mocks — must come before the import of the SUT (Vue component).
// ---------------------------------------------------------------------------
const mockGetPrefs = vi.fn();
const mockPutPrefs = vi.fn();
const mockGetSubs = vi.fn(async () => []);
const mockPatchSubs = vi.fn();
const mockListProjects = vi.fn(async () => []);

vi.mock('../../../api/me', () => ({
  getNotificationPrefs: (...args: unknown[]) => mockGetPrefs(...args),
  putNotificationPrefs: (...args: unknown[]) => mockPutPrefs(...args),
}));

vi.mock('../../../api/email', () => ({
  getMySubscriptions: (...args: unknown[]) => mockGetSubs(...args),
  patchSubscriptions: (...args: unknown[]) => mockPatchSubs(...args),
}));

vi.mock('../../../api/projects', () => ({
  listProjects: (...args: unknown[]) => mockListProjects(...args),
}));

vi.mock('ant-design-vue', () => ({
  notification: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}));

// Skip the heavy ProjectRecipientsPanel — we don't exercise it here.
vi.mock('../../../components/ProjectRecipientsPanel.vue', () => ({
  default: { template: '<div />' },
}));

import Notifications from '../Notifications.vue';

// ---------------------------------------------------------------------------
// Minimal Ant stubs — keep the bindings ``checked`` + ``onUpdate:checked``
// flowing for v-model; ignore loading/disabled visuals.
// ---------------------------------------------------------------------------
const stubs = {
  ABreadcrumb: { template: '<div><slot /></div>' },
  ABreadcrumbItem: { template: '<span><slot /></span>' },
  ACard: { template: '<section><slot /></section>' },
  AAlert: { template: '<div><slot /></div>' },
  ASpace: { template: '<div><slot /></div>' },
  ADivider: { template: '<hr />' },
  ASelect: { template: '<select><slot /></select>' },
  ASelectOption: { template: '<option><slot /></option>' },
  ATag: { template: '<span><slot /></span>' },
  AButton: {
    inheritAttrs: true,
    template: '<button v-bind="$attrs"><slot /></button>',
  },
  ACheckbox: {
    props: { checked: { type: Boolean, default: false } },
    emits: ['update:checked', 'change'],
    methods: {
      onChange(this: { $emit: (n: string, v: unknown) => void }, e: Event) {
        const checked = (e.target as HTMLInputElement).checked;
        this.$emit('update:checked', checked);
        this.$emit('change', e);
      },
    },
    template:
      '<input type="checkbox" :checked="checked" v-bind="$attrs" @change="onChange" />',
  },
  ReloadOutlined: { template: '<span />' },
  SaveOutlined: { template: '<span />' },
  InfoCircleOutlined: { template: '<span />' },
};

function makeI18n() {
  return createI18n({
    legacy: false,
    locale: 'en-US',
    fallbackLocale: 'en-US',
    // The SUT calls many keys; we register only the ones the prefs
    // section reads + a handful for the rest of the page so vue-i18n
    // doesn't spew warnings. Missing keys fall back to the key string,
    // which is harmless for assertions.
    messages: { 'en-US': {}, 'zh-CN': {} },
    missingWarn: false,
    fallbackWarn: false,
  });
}

const SERVER_PREFS = {
  notify_batch_done: true,
  notify_batch_failed: true,
  notify_job_failed: false,
  notify_diverged: true,
  notify_job_idle: false,
};

beforeEach(() => {
  mockGetPrefs.mockReset();
  mockPutPrefs.mockReset();
  mockGetSubs.mockReset();
  mockGetSubs.mockResolvedValue([]);
  mockPatchSubs.mockReset();
  mockListProjects.mockReset();
  mockListProjects.mockResolvedValue([]);
});

describe('Notifications.vue — Email preferences (#108)', () => {
  it('loads preferences and populates the five toggles on mount', async () => {
    mockGetPrefs.mockResolvedValueOnce({ ...SERVER_PREFS });
    const wrapper = mount(Notifications, {
      global: { plugins: [makeI18n()], stubs },
    });
    await flushPromises();

    expect(mockGetPrefs).toHaveBeenCalledTimes(1);

    // Each pref renders one checkbox with a stable test id.
    for (const [k, v] of Object.entries(SERVER_PREFS)) {
      const input = wrapper.find(`[data-testid="pref-${k}"]`);
      expect(input.exists(), `missing pref-${k}`).toBe(true);
      expect((input.element as HTMLInputElement).checked).toBe(v);
    }
  });

  it('disables save until a toggle differs from the loaded value', async () => {
    mockGetPrefs.mockResolvedValueOnce({ ...SERVER_PREFS });
    const wrapper = mount(Notifications, {
      global: { plugins: [makeI18n()], stubs },
    });
    await flushPromises();

    const saveBtn = wrapper.get('[data-testid="prefs-save-btn"]');
    expect(saveBtn.attributes('disabled')).toBeDefined();

    // Flip ``notify_job_failed`` from false → true; save should enable.
    const cb = wrapper.get('[data-testid="pref-notify_job_failed"]');
    (cb.element as HTMLInputElement).checked = true;
    await cb.trigger('change');

    expect(saveBtn.attributes('disabled')).toBeUndefined();
  });

  it('saves the dirty payload and folds it into baseline', async () => {
    mockGetPrefs.mockResolvedValueOnce({ ...SERVER_PREFS });
    mockPutPrefs.mockImplementationOnce(async (body: typeof SERVER_PREFS) => body);

    const wrapper = mount(Notifications, {
      global: { plugins: [makeI18n()], stubs },
    });
    await flushPromises();

    // Flip one toggle, then save.
    const cb = wrapper.get('[data-testid="pref-notify_job_failed"]');
    (cb.element as HTMLInputElement).checked = true;
    await cb.trigger('change');

    await wrapper.get('[data-testid="prefs-save-btn"]').trigger('click');
    await flushPromises();

    expect(mockPutPrefs).toHaveBeenCalledTimes(1);
    expect(mockPutPrefs).toHaveBeenCalledWith({
      ...SERVER_PREFS,
      notify_job_failed: true,
    });

    // After save the baseline matches the draft, so save disables again.
    const saveBtn = wrapper.get('[data-testid="prefs-save-btn"]');
    expect(saveBtn.attributes('disabled')).toBeDefined();
  });
});
