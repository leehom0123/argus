<script setup lang="ts">
import { computed, ref, watch, onMounted } from 'vue';
import { theme } from 'ant-design-vue';
import zhCNLocale from 'ant-design-vue/es/locale/zh_CN';
import enUSLocale from 'ant-design-vue/es/locale/en_US';
import dayjs from 'dayjs';
import 'dayjs/locale/zh-cn';
import 'dayjs/locale/en';
import { useI18n } from 'vue-i18n';
import LangSwitch from './components/LangSwitch.vue';
import ThemeToggle from './components/ThemeToggle.vue';
import {
  AppstoreOutlined,
  UnorderedListOutlined,
  DesktopOutlined,
  SettingOutlined,
  UserOutlined,
  LogoutOutlined,
  KeyOutlined,
  ShareAltOutlined,
  ProfileOutlined,
  SafetyCertificateOutlined,
  LaptopOutlined,
  TeamOutlined,
  FlagOutlined,
  FileSearchOutlined,
  DashboardOutlined,
  ProjectOutlined,
  ThunderboltOutlined,
  ExperimentOutlined,
  DatabaseOutlined,
  InfoCircleOutlined,
  MailOutlined,
  BellOutlined,
} from '@ant-design/icons-vue';
import { useRoute, useRouter } from 'vue-router';
import { useAppStore } from './store/app';
import { useAuthStore } from './store/auth';
import { usePinsStore, PIN_LIMIT } from './store/pins';

const { locale, t } = useI18n();
const appStore = useAppStore();
const auth = useAuthStore();
const pins = usePinsStore();
const route = useRoute();
const router = useRouter();

const antdLocale = computed(() =>
  locale.value === 'zh-CN' ? zhCNLocale : enUSLocale,
);

// Keep dayjs locale in sync with the active UI locale
function applyDayjsLocale(l: string) {
  dayjs.locale(l === 'zh-CN' ? 'zh-cn' : 'en');
}
watch(locale, applyDayjsLocale);
onMounted(() => applyDayjsLocale(locale.value));

// Load pin count once we've got a session — used for nav badge display.
if (auth.isAuthenticated) {
  void pins.ensureLoaded();
}

const themeConfig = computed(() => ({
  algorithm: appStore.darkMode ? theme.darkAlgorithm : theme.defaultAlgorithm,
  token: {
    colorPrimary: '#4096ff',
  },
}));

// Route meta.layout === 'auth' means render bare — the page component itself
// supplies AuthLayout (no sider / header).
const useAuthLayout = computed(() => route.meta?.layout === 'auth');

// Route meta.layout === 'public' means simplified chrome: top bar only
// (no sider), shown for anonymous /demo and /public/:slug visitors.
const usePublicLayout = computed(() => route.meta?.layout === 'public');

const selectedKeys = computed<string[]>(() => {
  const p = route.path;
  if (p === '/' || p.startsWith('/dashboard')) return ['dashboard'];
  if (p.startsWith('/projects')) return ['projects'];
  if (p.startsWith('/jobs')) return ['jobs'];
  if (p.startsWith('/studies')) return ['studies'];
  if (p.startsWith('/compare')) return ['compare'];
  if (p.startsWith('/hosts')) return ['hosts'];
  if (p.startsWith('/settings')) return ['settings'];
  if (p.startsWith('/admin/users')) return ['admin-users'];
  if (p.startsWith('/admin/audit-log')) return ['admin-audit'];
  if (p.startsWith('/admin/backups')) return ['admin-backups'];
  if (p.startsWith('/admin/email/templates')) return ['admin-email-templates'];
  // v0.1.4 Settings → Admin sub-routes
  if (p.startsWith('/settings/oauth-github')) return ['admin-oauth-github'];
  if (p.startsWith('/settings/smtp')) return ['admin-smtp'];
  if (p.startsWith('/settings/retention')) return ['admin-retention'];
  if (p.startsWith('/settings/feature-flags')) return ['admin-flags'];
  if (p.startsWith('/settings/demo-project')) return ['admin-demo-project'];
  if (p.startsWith('/settings/security')) return ['admin-security'];
  return ['batches'];
});

// Keep the Admin submenu open whenever we're on one of its pages, so users
// landing via deep-link see their current location in context.
const ADMIN_SETTINGS_PATHS = [
  '/settings/oauth-github',
  '/settings/smtp',
  '/settings/retention',
  '/settings/feature-flags',
  '/settings/demo-project',
  '/settings/security',
];
const openKeys = computed<string[]>(() => {
  if (route.path.startsWith('/admin')) return ['admin'];
  if (ADMIN_SETTINGS_PATHS.some((p) => route.path.startsWith(p))) {
    return ['admin'];
  }
  return [];
});

function onMenuClick(info: { key: string | number }) {
  const key = String(info.key);
  if (key === 'dashboard') router.push('/');
  else if (key === 'projects') router.push('/projects');
  else if (key === 'batches') router.push('/batches');
  else if (key === 'jobs') router.push('/jobs');
  else if (key === 'studies') router.push('/studies');
  else if (key === 'compare') router.push('/compare');
  else if (key === 'hosts') router.push('/hosts');
  else if (key === 'settings') router.push('/settings');
  else if (key === 'admin-users') router.push('/admin/users');
  else if (key === 'admin-audit') router.push('/admin/audit-log');
  else if (key === 'admin-backups') router.push('/admin/backups');
  else if (key === 'admin-email-templates') router.push('/admin/email/templates');
  // v0.1.4 Settings → Admin sub-routes
  else if (key === 'admin-oauth-github') router.push('/settings/oauth-github');
  else if (key === 'admin-smtp') router.push('/settings/smtp');
  else if (key === 'admin-retention') router.push('/settings/retention');
  else if (key === 'admin-flags') router.push('/settings/feature-flags');
  else if (key === 'admin-demo-project') router.push('/settings/demo-project');
  else if (key === 'admin-security') router.push('/settings/security');
}

// ---- Email-verified banner ----
const verifyBannerDismissed = ref(false);
const showVerifyBanner = computed(
  () =>
    !useAuthLayout.value &&
    auth.isAuthenticated &&
    auth.currentUser !== null &&
    !auth.isEmailVerified &&
    !verifyBannerDismissed.value,
);

// ---- User dropdown ----
const userInitial = computed(() => {
  const name = auth.currentUser?.username ?? '';
  return name.length > 0 ? name[0].toUpperCase() : '?';
});

function onUserMenuClick(info: { key: string | number }) {
  const key = String(info.key);
  switch (key) {
    case 'profile':
      router.push('/settings/profile');
      break;
    case 'tokens':
      router.push('/settings/tokens');
      break;
    case 'shares':
      router.push('/settings/shares');
      break;
    case 'preferences':
      router.push('/settings/preferences');
      break;
    case 'sessions':
      router.push('/settings/sessions');
      break;
    case 'password':
      router.push('/settings/password');
      break;
    case 'about':
      router.push('/settings/about');
      break;
    case 'notifications':
      router.push('/settings/notifications');
      break;
    case 'logout':
      void handleLogout();
      break;
  }
}

// Build-time injected (see vite.config.ts `define`). Falls back to an
// empty string in the rare case the macro is stripped by a tree shaker.
const appVersion = typeof __APP_VERSION__ !== 'undefined' ? __APP_VERSION__ : '';

async function handleLogout() {
  await auth.logout();
  await router.push('/login');
}
</script>

<template>
  <a-config-provider :theme="themeConfig" :locale="antdLocale">
    <!-- Bare layout for /login, /register, /verify-email, /reset-password -->
    <template v-if="useAuthLayout">
      <router-view />
    </template>

    <!-- Public layout: top bar only. Used for /demo and /public/:slug so
         anonymous visitors don't see empty "My Projects" / "Notifications"
         sidebar items. Signed-in users visiting /demo/* also land here
         (conscious demo preview). -->
    <template v-else-if="usePublicLayout">
      <a-layout class="app-root">
        <a-layout>
          <a-layout-header
            style="
              padding: 0 20px;
              display: flex;
              align-items: center;
              justify-content: space-between;
              background: transparent;
            "
          >
            <router-link
              :to="auth.isAuthenticated ? '/' : '/demo'"
              style="
                font-size: 15px;
                font-weight: 600;
                letter-spacing: 0.5px;
                text-decoration: none;
              "
            >
              Argus
            </router-link>
            <div style="display: flex; align-items: center; gap: 8px">
              <LangSwitch />
              <ThemeToggle />
              <template v-if="auth.isAuthenticated">
                <router-link to="/">
                  <a-button size="small">{{ t('nav.dashboard') }}</a-button>
                </router-link>
              </template>
              <template v-else>
                <router-link to="/login">
                  <a-button size="small">
                    {{ t('nav.sign_in') }}
                  </a-button>
                </router-link>
                <router-link to="/register">
                  <a-button type="primary" size="small">
                    <template #icon><UserOutlined /></template>
                    {{ t('component_anonymous_cta.sign_up') }}
                  </a-button>
                </router-link>
              </template>
            </div>
          </a-layout-header>
          <a-layout-content>
            <router-view />
          </a-layout-content>
        </a-layout>
      </a-layout>
    </template>

    <!-- Main app layout -->
    <template v-else>
      <a-layout class="app-root">
        <a-layout-sider
          v-model:collapsed="appStore.siderCollapsed"
          collapsible
          :width="220"
          theme="dark"
        >
          <div
            style="
              height: 56px;
              display: flex;
              align-items: center;
              justify-content: center;
              color: #fff;
              font-weight: 600;
              letter-spacing: 0.5px;
            "
          >
            <span v-if="!appStore.siderCollapsed">Argus</span>
            <span v-else>A</span>
          </div>
          <a-menu
            theme="dark"
            mode="inline"
            :selected-keys="selectedKeys"
            :open-keys="openKeys"
            @click="onMenuClick"
          >
            <a-menu-item key="dashboard">
              <template #icon><DashboardOutlined /></template>
              <span>{{ t('nav.dashboard') }}</span>
            </a-menu-item>
            <a-menu-item key="projects">
              <template #icon><ProjectOutlined /></template>
              <span>{{ t('nav.projects') }}</span>
            </a-menu-item>
            <a-menu-item key="batches">
              <template #icon><AppstoreOutlined /></template>
              <span>{{ t('nav.batches') }}</span>
            </a-menu-item>
            <a-menu-item key="studies">
              <template #icon><ExperimentOutlined /></template>
              <span>{{ t('nav.studies') }}</span>
            </a-menu-item>
            <a-menu-item key="jobs">
              <template #icon><UnorderedListOutlined /></template>
              <span>{{ t('nav.jobs') }}</span>
            </a-menu-item>
            <a-menu-item key="compare">
              <template #icon><ThunderboltOutlined /></template>
              <span>
                {{ t('nav.compare') }}
                <a-tag
                  v-if="pins.count > 0"
                  color="blue"
                  style="margin-left: 6px; font-size: 10px; line-height: 16px; padding: 0 4px"
                >
                  {{ pins.count }}/{{ PIN_LIMIT }}
                </a-tag>
              </span>
            </a-menu-item>
            <a-menu-item key="hosts">
              <template #icon><DesktopOutlined /></template>
              <span>{{ t('nav.hosts') }}</span>
            </a-menu-item>
            <a-menu-item key="settings">
              <template #icon><SettingOutlined /></template>
              <span>{{ t('nav.settings') }}</span>
            </a-menu-item>
            <a-sub-menu v-if="auth.isAdmin" key="admin">
              <template #icon><SafetyCertificateOutlined /></template>
              <template #title>{{ t('nav.admin') }}</template>
              <a-menu-item key="admin-users">
                <template #icon><TeamOutlined /></template>
                <span>{{ t('nav.admin_users') }}</span>
              </a-menu-item>
              <a-menu-item key="admin-oauth-github">
                <template #icon><SafetyCertificateOutlined /></template>
                <span>{{ t('nav.admin_oauth_github') }}</span>
              </a-menu-item>
              <a-menu-item key="admin-smtp">
                <template #icon><MailOutlined /></template>
                <span>{{ t('nav.admin_smtp') }}</span>
              </a-menu-item>
              <a-menu-item key="admin-retention">
                <template #icon><DatabaseOutlined /></template>
                <span>{{ t('nav.admin_retention') }}</span>
              </a-menu-item>
              <a-menu-item key="admin-flags">
                <template #icon><FlagOutlined /></template>
                <span>{{ t('nav.feature_flags') }}</span>
              </a-menu-item>
              <a-menu-item key="admin-demo-project">
                <template #icon><ProjectOutlined /></template>
                <span>{{ t('nav.admin_demo_project') }}</span>
              </a-menu-item>
              <a-menu-item key="admin-security">
                <template #icon><SafetyCertificateOutlined /></template>
                <span>{{ t('nav.admin_security') }}</span>
              </a-menu-item>
              <a-menu-item key="admin-audit">
                <template #icon><FileSearchOutlined /></template>
                <span>{{ t('nav.audit_log') }}</span>
              </a-menu-item>
              <a-menu-item key="admin-backups">
                <template #icon><DatabaseOutlined /></template>
                <span>{{ t('nav.backups') }}</span>
              </a-menu-item>
              <a-menu-item key="admin-email-templates">
                <template #icon><MailOutlined /></template>
                <span>{{ t('nav.admin_email_templates') }}</span>
              </a-menu-item>
            </a-sub-menu>
          </a-menu>
        </a-layout-sider>

        <a-layout>
          <a-layout-header
            style="
              padding: 0 20px;
              display: flex;
              align-items: center;
              justify-content: space-between;
              background: transparent;
            "
          >
            <div style="font-size: 14px; opacity: 0.75">
              ML experiment monitor — batches, jobs, resources
            </div>
            <div style="display: flex; align-items: center; gap: 8px">
              <LangSwitch />
              <ThemeToggle />

              <template v-if="auth.isAuthenticated">
                <a-dropdown :trigger="['click']">
                  <a-button type="text" style="padding: 0 8px; height: 40px">
                    <a-avatar
                      size="small"
                      style="background-color: #4096ff; margin-right: 8px"
                    >
                      {{ userInitial }}
                    </a-avatar>
                    <span>{{ auth.currentUser?.username }}</span>
                  </a-button>
                  <template #overlay>
                    <a-menu @click="onUserMenuClick">
                      <a-menu-item key="profile">
                        <template #icon><ProfileOutlined /></template>
                        {{ t('nav.profile') }}
                      </a-menu-item>
                      <a-menu-item key="tokens">
                        <template #icon><KeyOutlined /></template>
                        {{ t('nav.tokens') }}
                      </a-menu-item>
                      <a-menu-item key="shares">
                        <template #icon><ShareAltOutlined /></template>
                        {{ t('nav.shares') }}
                      </a-menu-item>
                      <a-menu-item key="preferences">
                        <template #icon><SettingOutlined /></template>
                        {{ t('nav.preferences') }}
                      </a-menu-item>
                      <a-menu-item key="notifications">
                        <template #icon><BellOutlined /></template>
                        {{ t('nav.notifications') }}
                      </a-menu-item>
                      <a-menu-item key="sessions">
                        <template #icon><LaptopOutlined /></template>
                        {{ t('nav.sessions') }}
                      </a-menu-item>
                      <a-menu-item key="password">
                        <template #icon><SafetyCertificateOutlined /></template>
                        {{ t('nav.password') }}
                      </a-menu-item>
                      <a-menu-item key="about">
                        <template #icon><InfoCircleOutlined /></template>
                        {{ t('nav.about') }}
                      </a-menu-item>
                      <a-menu-divider />
                      <a-menu-item key="logout">
                        <template #icon><LogoutOutlined /></template>
                        {{ t('nav.sign_out') }}
                      </a-menu-item>
                    </a-menu>
                  </template>
                </a-dropdown>
              </template>
              <template v-else>
                <router-link to="/login">
                  <a-button type="primary" size="small">
                    <template #icon><UserOutlined /></template>
                    {{ t('nav.sign_in') }}
                  </a-button>
                </router-link>
              </template>
            </div>
          </a-layout-header>

          <a-alert
            v-if="showVerifyBanner"
            type="warning"
            show-icon
            closable
            banner
            message="Email not verified — please click the link we sent you. Some features may be limited until you confirm your address."
            style="margin: 0 20px"
            @close="verifyBannerDismissed = true"
          />

          <a-layout-content>
            <router-view />
          </a-layout-content>

          <a-layout-footer class="app-footer">
            <span>Argus<template v-if="appVersion"> v{{ appVersion }}</template></span>
            <span class="app-footer-sep">·</span>
            <a
              href="https://www.apache.org/licenses/LICENSE-2.0"
              target="_blank"
              rel="noopener noreferrer"
            >{{ t('footer.license_link') }}</a>
            <span class="app-footer-sep">·</span>
            <router-link to="/settings/about">{{ t('footer.about_link') }}</router-link>
          </a-layout-footer>
        </a-layout>
      </a-layout>
    </template>
  </a-config-provider>
</template>

<style scoped>
.app-footer {
  text-align: center;
  padding: 12px 20px;
  font-size: 12px;
  opacity: 0.65;
  background: transparent;
}
.app-footer-sep {
  margin: 0 6px;
  opacity: 0.5;
}
</style>
