import { createI18n } from 'vue-i18n';
import zhCN from './locales/zh-CN';
import enUS from './locales/en-US';

export type Locale = 'zh-CN' | 'en-US';

const STORAGE_KEY = 'locale';

function getInitialLocale(): Locale {
  // 1. Explicit user preference wins
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored === 'zh-CN' || stored === 'en-US') return stored;
  // 2. Browser language — prefix-match 'zh' → zh-CN, else en-US
  const nav = navigator.language ?? '';
  if (nav.toLowerCase().startsWith('zh')) return 'zh-CN';
  return 'en-US';
}

const i18n = createI18n({
  legacy: false,           // composition API mode
  locale: getInitialLocale(),
  fallbackLocale: 'en-US',
  messages: {
    'zh-CN': zhCN,
    'en-US': enUS,
  },
});

export function setLocale(locale: Locale): void {
  (i18n.global.locale as import('vue').Ref<string>).value = locale;
  localStorage.setItem(STORAGE_KEY, locale);
  document.documentElement.lang = locale;
}

export { i18n };
export default i18n;
