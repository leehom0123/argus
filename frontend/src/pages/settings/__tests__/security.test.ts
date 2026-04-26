/**
 * security.test.ts
 *
 * Pins the Settings → Admin → Security page (#109). Verifies:
 *
 *   1. On mount the page calls ``getJwtRotationStatus`` and renders
 *      "Never rotated" when ``rotated_at`` is null.
 *   2. When a previous secret is held, the countdown reflects the
 *      ``previous_expires_at`` timestamp (we drive ``Date.now``
 *      deterministically).
 *   3. The Rotate button calls ``rotateJwtSecret`` after confirmation
 *      and re-fetches status. The success toast fires.
 *
 * The Ant Design ``Modal.confirm`` is stubbed to auto-accept so the
 * test exercises the full rotate path without rendering a real modal.
 *
 * @vitest-environment jsdom
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createI18n } from 'vue-i18n';

// ---------------------------------------------------------------------------
// Module mocks — must come before the import of the SUT (Vue component).
// ---------------------------------------------------------------------------
const mockGetStatus = vi.fn();
const mockRotate = vi.fn();

vi.mock('../../../api/admin', () => ({
  getJwtRotationStatus: (...args: unknown[]) => mockGetStatus(...args),
  rotateJwtSecret: (...args: unknown[]) => mockRotate(...args),
}));

// Auto-accept Modal.confirm so the test doesn't have to interact with
// a real overlay. We capture the args so we can assert the confirm
// flow ran. ``notification`` is also mocked so success toasts don't
// hit the DOM.
const mockModalConfirm = vi.fn(
  (opts: { onOk?: () => void | Promise<void> }) => {
    void opts.onOk?.();
    return { destroy: vi.fn(), update: vi.fn() };
  },
);
const mockNotificationSuccess = vi.fn();

vi.mock('ant-design-vue', () => ({
  Modal: {
    confirm: (...args: unknown[]) => mockModalConfirm(...(args as [never])),
  },
  notification: {
    success: (...args: unknown[]) => mockNotificationSuccess(...args),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}));

import Security from '../admin/Security.vue';

// ---------------------------------------------------------------------------
// Minimal Ant stubs — keep slot rendering + button click events flowing.
// ---------------------------------------------------------------------------
const stubs = {
  ABreadcrumb: { template: '<div><slot /></div>' },
  ABreadcrumbItem: { template: '<span><slot /></span>' },
  ACard: { template: '<section><slot /></section>' },
  AAlert: { template: '<div><slot /></div>' },
  ASpace: { template: '<div><slot /></div>' },
  ADescriptions: { template: '<dl><slot /></dl>' },
  ADescriptionsItem: {
    props: { label: { type: String, default: '' } },
    template: '<div><dt>{{ label }}</dt><dd><slot /></dd></div>',
  },
  // ``inheritAttrs: true`` means the parent's ``@click`` listener
  // attaches as a native ``onclick`` on the rendered <button>, so we
  // do NOT also ``$emit('click')`` — that would double-fire and break
  // call-count assertions.
  AButton: {
    inheritAttrs: true,
    template: '<button v-bind="$attrs"><slot /></button>',
  },
  ReloadOutlined: { template: '<span />' },
  SafetyCertificateOutlined: { template: '<span />' },
  ThunderboltOutlined: { template: '<span />' },
};

function makeI18n() {
  return createI18n({
    legacy: false,
    locale: 'en-US',
    fallbackLocale: 'en-US',
    messages: {
      'en-US': {
        nav: { settings: 'Settings', admin: 'Admin' },
        settings: {
          admin: {
            common: { reload: 'Reload' },
            security: {
              title: 'Security',
              rotate: {
                hint_title: 'Rotate JWT secret',
                hint_desc: 'desc',
                last_rotated: 'Last rotated',
                never_rotated: 'Never rotated',
                previous_label: 'Previous secret',
                previous_remaining: '{hours}h {mins}m {secs}s left',
                previous_expired: 'Expired',
                no_previous: 'No previous secret',
                button: 'Rotate now',
                confirm_title: 'Rotate?',
                confirm_body: 'body',
                confirm_ok: 'Yes',
                confirm_cancel: 'No',
                success_title: 'Done',
                success_desc: 'Rotated at {rotated_at}',
                no_logout_note: 'No logout',
              },
            },
          },
        },
      },
    },
    missingWarn: false,
    fallbackWarn: false,
    datetimeFormats: {
      'en-US': {
        long: { year: 'numeric', month: 'short', day: 'numeric' },
      },
    },
  });
}

// Pin the wall clock so the countdown computed property is deterministic.
const FIXED_NOW = new Date('2026-04-26T12:00:00Z').getTime();

beforeEach(() => {
  mockGetStatus.mockReset();
  mockRotate.mockReset();
  mockModalConfirm.mockClear();
  mockNotificationSuccess.mockReset();
  vi.useFakeTimers();
  vi.setSystemTime(FIXED_NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

describe('Security.vue — JWT secret rotation (#109)', () => {
  it('renders "Never rotated" + "No previous secret" on a clean install', async () => {
    mockGetStatus.mockResolvedValueOnce({
      rotated_at: null,
      has_previous: false,
      previous_expires_at: null,
      grace_seconds: 86400,
    });
    const wrapper = mount(Security, {
      global: { plugins: [makeI18n()], stubs },
    });
    await flushPromises();

    expect(mockGetStatus).toHaveBeenCalledTimes(1);
    expect(wrapper.get('[data-testid="rotated-at"]').text()).toBe('Never rotated');
    expect(wrapper.get('[data-testid="previous-countdown"]').text()).toBe(
      'No previous secret',
    );
  });

  it('renders the live countdown when a previous secret is held', async () => {
    // Previous expires 2h:30m:15s after FIXED_NOW.
    const expIso = new Date(FIXED_NOW + (2 * 3600 + 30 * 60 + 15) * 1000)
      .toISOString();
    mockGetStatus.mockResolvedValueOnce({
      rotated_at: '2026-04-25T12:00:00Z',
      has_previous: true,
      previous_expires_at: expIso,
      grace_seconds: 86400,
    });
    const wrapper = mount(Security, {
      global: { plugins: [makeI18n()], stubs },
    });
    await flushPromises();

    const countdown = wrapper.get('[data-testid="previous-countdown"]').text();
    // The mins/secs are zero-padded; the hours field is bare.
    expect(countdown).toBe('2h 30m 15s left');
  });

  it('rotates the secret and re-fetches status on success', async () => {
    mockGetStatus
      .mockResolvedValueOnce({
        rotated_at: null,
        has_previous: false,
        previous_expires_at: null,
        grace_seconds: 86400,
      })
      .mockResolvedValueOnce({
        rotated_at: '2026-04-26T12:00:00Z',
        has_previous: false,
        previous_expires_at: null,
        grace_seconds: 86400,
      });
    mockRotate.mockResolvedValueOnce({
      rotated_at: '2026-04-26T12:00:00Z',
      grace_seconds: 86400,
    });

    const wrapper = mount(Security, {
      global: { plugins: [makeI18n()], stubs },
    });
    await flushPromises();

    await wrapper.get('[data-testid="rotate-jwt-btn"]').trigger('click');
    await flushPromises();

    // Modal.confirm fired AND its onOk callback ran (auto-accepted).
    expect(mockModalConfirm).toHaveBeenCalledTimes(1);
    expect(mockRotate).toHaveBeenCalledTimes(1);
    // Status re-fetched after rotation.
    expect(mockGetStatus).toHaveBeenCalledTimes(2);
    // Success toast fired with the rotated_at echoed back.
    expect(mockNotificationSuccess).toHaveBeenCalledTimes(1);
    const toastArg = mockNotificationSuccess.mock.calls[0][0] as {
      message: string;
      description: string;
    };
    expect(toastArg.message).toBe('Done');
    expect(toastArg.description).toContain('2026-04-26T12:00:00Z');
  });
});
