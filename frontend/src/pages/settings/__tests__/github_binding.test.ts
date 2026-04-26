/**
 * github_binding.test.ts
 *
 * Pins the GitHub link / unlink button states on Profile.vue (#108).
 * Verifies:
 *
 *   1. When ``github_login`` is null + a password is set → only the
 *      "Link GitHub" button renders.
 *   2. When ``github_login`` is set AND ``has_password=true`` → the
 *      "Unlink" button renders (popconfirm-wrapped) and the linked
 *      handle is shown.
 *   3. When ``github_login`` is set BUT ``has_password=false`` → the
 *      "Set a password" CTA renders alongside an unlink button that
 *      opens the set-password modal first (the SUT toggles its own
 *      modal rather than calling the API directly).
 *
 * The flow is purely state-rendered — no API calls are exercised here
 * (the click handlers themselves are covered by their own tests in the
 * existing auth.spec / e2e suite). We just want to lock the button
 * matrix so a regression in ``githubLinked`` / ``githubOnly`` /
 * ``hasPassword`` computeds is loud.
 *
 * @vitest-environment jsdom
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createI18n } from 'vue-i18n';
import { createPinia, setActivePinia } from 'pinia';

// Stub network APIs — the link-state assertions never trigger them.
vi.mock('../../../api/me', () => ({
  resendVerification: vi.fn(),
}));
vi.mock('../../../api/auth', () => ({
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

// ---------------------------------------------------------------------------
// Stubs — render slot content + named slots so we can introspect the
// "Linked as @{login}" line and the button labels.
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
  AAlert: { template: '<div><slot /><slot name="action" /></div>' },
  AButton: {
    inheritAttrs: false,
    props: { loading: { type: Boolean, default: false }, disabled: { type: Boolean, default: false } },
    template: '<button v-bind="$attrs" :disabled="disabled || loading"><slot /></button>',
  },
  AInput: { template: '<input />' },
  AInputPassword: { template: '<input type="password" />' },
  AModal: { template: '<div><slot /></div>' },
  AForm: { template: '<form><slot /></form>' },
  AFormItem: { template: '<div><slot /></div>' },
  // Render the trigger button via the popconfirm's default slot so the
  // "Unlink" button is reachable. The actual confirm-flow logic isn't
  // under test here.
  APopconfirm: { template: '<div><slot /></div>' },
  RouterLink: { template: '<a><slot /></a>' },
};

function makeI18n() {
  return createI18n({
    legacy: false,
    locale: 'en-US',
    fallbackLocale: 'en-US',
    // Provide only the keys the rendered buttons use so we can assert
    // on the button label text rather than on opaque test IDs (the
    // template uses $t('page_settings_profile.github_link_button') etc.,
    // not data-testid).
    messages: {
      'en-US': {
        common: { cancel: 'Cancel' },
        page_register: {
          password_hint: '',
          validation_password_short: '',
          validation_password_weak: '',
          validation_password_mismatch: '',
        },
        page_settings_profile: {
          breadcrumb_settings: 'Settings',
          breadcrumb_profile: 'Profile',
          tab_profile: 'Profile',
          card_account: 'Account',
          label_user_id: 'User ID',
          label_username: 'Username',
          label_email: 'Email',
          tag_verified: 'verified',
          tag_unverified: 'unverified',
          label_role: 'Role',
          tag_admin: 'admin',
          tag_user: 'user',
          label_created: 'Created',
          label_last_login: 'Last login',
          card_change_password: 'Change password',
          email_section_title: 'Email',
          email_change_button: 'Change email',
          email_change_modal_title: 'Change email',
          email_modal_new_email: 'New email',
          email_modal_current_password: 'Current password',
          email_modal_submit: 'Send link',
          email_change_error_wrong_password: 'Wrong password',
          password_moved_banner: 'Moved',
          password_moved_link: 'Settings > Password',
          github_section_title: 'GitHub account',
          github_unlinked_description: 'Link your GitHub.',
          github_link_button: 'Link GitHub account',
          github_linked_as_label: 'Linked as',
          github_only_account_banner: 'GitHub-only',
          github_unlink_button: 'Unlink GitHub',
          github_unlink_confirm_title: 'Unlink?',
          github_unlink_confirm_body: 'Are you sure?',
          github_set_password_button: 'Set a password',
          github_set_password_first: 'Set password first',
          github_set_password_first_desc: 'Need a password',
          github_set_password_modal_title: 'Set password',
          github_set_password_submit: 'Save',
          github_link_failed: 'Failed',
          github_unlink_success: 'Unlinked',
          github_unlink_failed: 'Unlink failed',
          github_set_password_success: 'Saved',
          github_set_password_failed: 'Save failed',
          label_new_password: 'New password',
          label_confirm_new_password: 'Confirm',
          // Verify-banner keys (#108) — strings unused by these tests
          // but referenced when ``email_verified=false``.
          verify_banner_message: '',
          verify_banner_description: '',
          verify_resend_button: 'Resend verification',
          verify_resend_cooldown: 'Wait {seconds}s',
          verify_resent_toast: '',
          verify_already_verified_toast: '',
          verify_rate_limited_toast: '',
          verify_resend_failed_toast: '',
        },
      },
      'zh-CN': {},
    },
    missingWarn: false,
    fallbackWarn: false,
  });
}

beforeEach(() => {
  setActivePinia(createPinia());
});

function seedUser(opts: {
  github_login: string | null;
  has_password: boolean;
  email_verified?: boolean;
}) {
  const auth = useAuthStore();
  auth.currentUser = {
    id: 1,
    username: 'tester',
    email: 'tester@example.com',
    is_admin: false,
    email_verified: opts.email_verified ?? true,
    created_at: '2026-04-01T00:00:00Z',
    github_login: opts.github_login,
    has_password: opts.has_password,
  } as never;
  auth.accessToken = null;
}

describe('Profile.vue — GitHub binding states (#108)', () => {
  it('renders the "Link GitHub" button when no GitHub identity is bound', async () => {
    seedUser({ github_login: null, has_password: true });
    const wrapper = mount(Profile, {
      global: { plugins: [makeI18n()], stubs },
    });
    await flushPromises();

    const text = wrapper.text();
    expect(text).toContain('Link GitHub account');
    expect(text).not.toContain('Unlink GitHub');
  });

  it('renders the "Unlink GitHub" button + linked handle when bound + has password', async () => {
    seedUser({ github_login: 'octocat', has_password: true });
    const wrapper = mount(Profile, {
      global: { plugins: [makeI18n()], stubs },
    });
    await flushPromises();

    const text = wrapper.text();
    expect(text).toContain('@octocat');
    expect(text).toContain('Unlink GitHub');
    expect(text).not.toContain('Link GitHub account');
  });

  it('renders the "Set a password" CTA when bound but no local password', async () => {
    seedUser({ github_login: 'octocat', has_password: false });
    const wrapper = mount(Profile, {
      global: { plugins: [makeI18n()], stubs },
    });
    await flushPromises();

    const text = wrapper.text();
    expect(text).toContain('@octocat');
    // Both the proactive "Set a password" CTA AND the danger "Unlink"
    // button render — the unlink button just routes through the
    // set-password modal first (template uses ``<a-button v-else danger>``).
    expect(text).toContain('Set a password');
    expect(text).toContain('Unlink GitHub');
  });
});
