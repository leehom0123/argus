> 🌐 **中文** · [English](./README.md)

# Argus / 前端

基于 Vue 3 + Ant Design Vue + ECharts 的单页应用，服务于 Argus。通过 `/api`
与 FastAPI 后端通信；开发模式下 Vite 将 `/api` 代理至 `http://localhost:8000`。

## 环境要求

- Node **20+**（参见 `.nvmrc`）
- npm 10+（或 pnpm 8+）

## 安装

```bash
cd frontend
npm install        # 或：pnpm install
```

## 开发

```bash
npm run dev        # 在 http://localhost:5173 启动 Vite
```

开发服务器将 `/api/*` 代理到 `http://localhost:8000`，请同时启动后端。

## 类型检查与构建

```bash
npm run typecheck  # vue-tsc --noEmit
npm run build      # vue-tsc + vite build，输出至 dist/
```

`dist/` 目录是生产环境中后端 StaticFiles 所服务的内容。

## 目录结构

```
src/
├── api/client.ts          axios + 类型化辅助函数
├── components/            可复用 UI 组件（StatusTag、ProgressInline、LossChart……）
├── composables/useChart   ECharts + 深色主题封装
├── pages/                 路由挂载的页面（BatchList、BatchDetail、JobDetail、HostList……）
├── router/index.ts
├── store/                 Pinia 状态仓库（batches、app）
├── types.ts               与 REST 契约对应的 TypeScript 类型定义
├── utils/format.ts        dayjs + 时长格式化辅助函数
├── App.vue                布局外壳 + ConfigProvider（默认深色主题）
├── main.ts                入口文件
└── styles.css             少量全局样式覆盖
```

## 工程说明

- **默认深色主题。** 可在页头切换，也可在 `/settings` 中更改，偏好存储于 `localStorage`。
- **AntD 自动导入** — 通过 `unplugin-vue-components/resolvers/AntDesignVueResolver` 实现，无需手动 `import { Button } from 'ant-design-vue'`。
- **自动刷新** 按页面配置：列表页按 `appStore.autoRefreshSec` 刷新；`BatchDetail` 在状态为 `running` 时每 5 秒刷新；`HostDetail` 每 15 秒刷新。组件卸载时（`onUnmounted`）定时器会被清除。
- **错误提示限流** — axios 拦截器对错误提示进行节流，避免后台刷新频繁弹窗。
- **TypeScript 严格模式** 已开启；`types.ts` 是 REST 数据结构的唯一真源。

## 环境变量

前端为纯单页应用，无构建时环境变量；后端 URL 硬编码为 `/api`，由提供 `index.html` 的服务（开发时为 Vite，生产时为 StaticFiles/Nginx）负责路由。
