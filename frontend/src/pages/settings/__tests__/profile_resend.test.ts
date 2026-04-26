/**
 * profile_resend.test.ts
 *
 * Pins the email-verify resend banner on Settings/Profile.vue (#108).
 * Verifies:
 *
 *   1. Banner is hidden when ``email_verified === true``.
 *   2. Banner shows + clicking ``Resend`` calls the API and toasts.
 *   3. After a successful click the button enters a 60-second cooldown
 *      and is disabled.
 *
 * The auth store is initialised with a Pinia instance set per-test so
 * the ``email_verified`` flag is mutable. Network APIs (``api/auth``,
 * ``api/me``) are vi.mock'd up front so the SUT runs entirely in
 * memory — no axios or http instance touched.
 *
 * @vitest-environment jsdom
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createI18n } from 'vue-i18n';
import { createPinia, setActivePinia } from 'pinia';

// ---------------------------------------------------------------------------
// Module mocks
// ---------------------------------------------------------------------------
const mockResendVerification = vi.fn();
vi.mock('../../../api/me', () => ({
  resendVerification: (...args: unknown[]) => mockResendVerification(...args),
}));

vi.mock('../../../api/auth', () => ({
  // The SUT imports several auth helpers; the resend test only triggers
  // ``resendVerification`` so the rest can be no-op stubs.
  changeEmail: vi.fn(),
  githubLinkStart: vi.fn(),
  githubSetPassword: vi.fn(),
  githubUnlink: vi.fn(),
}));

vi.mock('ant-design-vue', () => ({
  notification: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}));

import Profile from '../Profile.vue';
import { useAuthStore } from '../../../store/auth';
import { notification } from 'ant-design-vue';

// ---------------------------------------------------------------------------
// Stubs for Ant Design — keep ``disabled`` propagated on the button so
// the cooldown-disable assertion can read it from the DOM.
// ---------------------------------------------------------------------------
const stubs = {
  ABreadcrumb: { template: '<div><slot /></div>' },
  ABreadcrumbItem: { template: '<span><slot /></span>' },
  ATabs: { template: '<div><slot /></div>' },
  ATabPane: { template: '<div><slot /></div>' },
  ACard: { template: '<section><slot /></section>' },
  ADescriptions: { template: '<div><slot /></div>' },
  ADescriptionsItem: { template: '<div><slot /></div>' },
  ATag: { template: '<span><slot /></span>' },
  ASpace: { template: '<div><slot /></div>' },
  ADivider: { template: '<hr />' },
  AAlert: {
    // Keep the action slot (the resend button lives there).
    template: '<div><slot /><slot name="action" /></div>',
  },
  AButton: {
    inheritAttrs: false,
    props: { loading: { type: Boolean, default: false }, disabled: { type: Boolean, default: false } },
    template:
      '<button v-bind="$attrs" :disabled="disabled || loading"><slot /></button>',
  },
  AInput: { template: '<input />' },
  AInputPassword: { template: '<input type="password" />' },
  AModal: { template: '<div><slot /></div>' },
  AForm: { template: '<form><slot /></form>' },
  AFormItem: { template: '<div><slot /></div>' },
  APopconfirm: { template: '<div><slot /></div>' },
  RouterLink: { template: '<a><slot /></a>' },
};

function makeI18n() {
  return createI18n({
    legacy: false,
    locale: 'en-US',
    fallbackLocale: 'en-US',
    messages: { 'en-US': {}, 'zh-CN': {} },
    missingWarn: false,
    fallbackWarn: false,
  });
}

beforeEach(() => {
  setActivePinia(createPinia());
  mockResendVerification.mockReset();
  vi.clearAllTimers();
  vi.useRealTimers();
  (notification.success as ReturnType<typeof vi.fn>).mockReset();
  (notification.error as ReturnType<typeof vi.fn>).mockReset();
  (notification.warning as ReturnType<typeof vi.fn>).mockReset();
  (notification.info as ReturnType<typeof vi.fn>).mockReset();
});

function seedUser(emailVerified: boolean) {
  const auth = useAuthStore();
  auth.currentUser = {
    id: 1,
    username: 'tester',
    email: 'tester@example.com',
    is_admin: false,
    email_verified: emailVerified,
    created_at: '2026-04-01T00:00:00Z',
    has_password: true,
  } as never;
  auth.accessToken = null; // skip the onMounted fetchMe path
}

describe('Profile.vue — verify-resend banner (#108)', () => {
  it('hides the banner when the email is already verified', async () => {
    seedUser(true);
    const wrapper = mount(Profile, {
      global: { plugins: [makeI18n()], stubs },
    });
    await flushPromises();

    expect(wrapper.find('[data-testid="verify-banner"]').exists()).toBe(false);
    expect(wrapper.find('[data-testid="verify-resend-btn"]').exists()).toBe(false);
  });

  it('shows the banner + button when the email is unverified', async () => {
    seedUser(false);
    const wrapper = mount(Profile, {
      global: { plugins: [makeI18n()], stubs },
    });
    await flushPromises();

    expect(wrapper.find('[data-testid="verify-banner"]').exists()).toBe(true);
    const btn = wrapper.find('[data-testid="verify-resend-btn"]');
    expect(btn.exists()).toBe(true);
    expect(btn.attributes('disabled')).toBeUndefined();
  });

  it('calls resendVerification + toasts + cooldowns the button on success', async () => {
    seedUser(false);
    mockResendVerification.mockResolvedValueOnce({ ok: true });

    const wrapper = mount(Profile, {
      global: { plugins: [makeI18n()], stubs },
    });
    await flushPromises();

    const btn = wrapper.get('[data-testid="verify-resend-btn"]');
    await btn.trigger('click');
    await flushPromises();

    expect(mockResendVerification).toHaveBeenCalledTimes(1);
    expect(notification.success).toHaveBeenCalledTimes(1);

    // After the resolve the button is in cooldown — disabled until the
    // 60-second window elapses. We only check the disabled attribute,
    // not the timer expiry, to keep the test fast and deterministic.
    await flushPromises();
    expect(btn.attributes('disabled')).toBeDefined();
  });
});
