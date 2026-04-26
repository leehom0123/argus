> 🌐 **English** · [中文](./README.zh-CN.md)

# Argus / frontend

Vue 3 + Ant Design Vue + ECharts SPA for the Argus service.
Talks to the FastAPI backend via `/api`; in dev, Vite proxies `/api` → `http://localhost:8000`.

## Requirements

- Node **20+** (see `.nvmrc`)
- npm 10+ (or pnpm 8+ if you prefer)

## Install

```bash
cd frontend
npm install        # or: pnpm install
```

## Develop

```bash
npm run dev        # starts Vite on http://localhost:5173
```

The dev server proxies `/api/*` to `http://localhost:8000`, so run the backend in parallel.

## Type-check & build

```bash
npm run typecheck  # vue-tsc --noEmit
npm run build      # vue-tsc + vite build, outputs dist/
```

The `dist/` folder is what the backend's StaticFiles serves in production.

## Layout

```
src/
├── api/client.ts          axios + typed helpers
├── components/            reusable UI (StatusTag, ProgressInline, LossChart, ...)
├── composables/           useChart, useLiveBatch, useCache, usePermissions, useBatchCompactData
├── i18n/                  vue-i18n catalogs (en-US + zh-CN), 1000+ keys with parity test
├── pages/                 router-mounted pages (BatchList, BatchDetail, JobDetail, HostList, ...)
├── router/index.ts
├── store/                 pinia stores (batches, app)
├── types.ts               TS shapes matching the REST contract
├── utils/                 dayjs + duration helpers, statusBorderColor, ...
├── App.vue                layout shell + ConfigProvider (dark by default) + LangSwitch
├── main.ts                entry
└── styles.css             small global overrides
```

## Engineering notes

- **Dark by default.** Toggle in the header or in `/settings`. Persisted in `localStorage`.
- **Bilingual UI** via `vue-i18n` 9. `LangSwitch` in the app shell flips locale and persists
  it. The CI gate `pnpm test:i18n` enforces parity between `en-US` and `zh-CN` catalogs and
  fails the build on hardcoded strings outside the locale files.
- **Auto-import for AntD** — via `unplugin-vue-components/resolvers/AntDesignVueResolver`.
  No manual `import { Button } from 'ant-design-vue'` needed.
- **Auto-refresh** is per-page: the list page refreshes every `appStore.autoRefreshSec`,
  `BatchDetail` refreshes every 5s while status=`running`, `HostDetail` every 15s.
  Timers are cleared in `onUnmounted`. Live data caches are capped at 10 s TTL to match
  backend response-cache TTL.
- **Error toasts** are throttled in the axios interceptor so background refreshes don't spam.
- **TypeScript strict mode** is on; `types.ts` is the single source of truth for REST shapes.

## Env

The frontend is pure SPA. There is no build-time env; the backend URL is baked as `/api` and
routed by whatever is serving `index.html` (Vite in dev, StaticFiles/Nginx in prod).
