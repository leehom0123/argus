import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import Components from 'unplugin-vue-components/vite';
import { AntDesignVueResolver } from 'unplugin-vue-components/resolvers';
import path from 'node:path';
import { readFileSync } from 'node:fs';

// Expose the frontend package.json version to the app at build time —
// surfaced under Settings → About and the layout footer.
const pkg = JSON.parse(
  readFileSync(path.resolve(__dirname, 'package.json'), 'utf-8'),
) as { version: string };

export default defineConfig({
  define: {
    __APP_VERSION__: JSON.stringify(pkg.version),
  },
  plugins: [
    vue(),
    Components({
      resolvers: [
        AntDesignVueResolver({
          importStyle: false, // ant-design-vue 4.x ships CSS-in-JS, no need to import styles
        }),
      ],
      dts: 'components.d.ts',
    }),
  ],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
  },
  server: {
    port: 5173,
    host: '0.0.0.0',
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 1200,
    rollupOptions: {
      output: {
        // Split the heaviest third-party libs out of the eagerly-loaded
        // index bundle so the shell paints faster. Each chart-consuming
        // page pulls the echarts chunk on demand (via the
        // defineAsyncComponent wrappers in Dashboard / JobDetail /
        // BatchDetail / HostDetail); chart-free pages never download it.
        //
        // ant-design-vue is imported from nearly every page so we leave it
        // in the main bundle — splitting it would only add a round-trip.
        manualChunks: {
          echarts: ['echarts', 'echarts/core', 'vue-echarts'],
          dayjs: ['dayjs'],
        },
      },
    },
  },
});
