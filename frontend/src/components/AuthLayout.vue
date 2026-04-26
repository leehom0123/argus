<script setup lang="ts">
// Centered single-card layout for /login, /register, /verify-email, /reset-password.
// Intentionally does not render the app sider / header.
import ThemeToggle from './ThemeToggle.vue';
</script>

<template>
  <div class="auth-layout">
    <div class="auth-toggle-wrap">
      <ThemeToggle />
    </div>
    <div class="auth-brand">
      <div class="auth-brand-title">{{ $t('app.brand_name') }}</div>
      <div class="auth-brand-sub">{{ $t('app.brand_sub') }}</div>
    </div>
    <div class="auth-card-wrap">
      <slot />
    </div>
    <div class="auth-footer">
      <span class="muted">v1 · MVP</span>
    </div>
  </div>
</template>

<style scoped>
.auth-layout {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: radial-gradient(
    ellipse at top,
    rgba(64, 150, 255, 0.12),
    transparent 60%
  );
  position: relative;
}
.auth-toggle-wrap {
  position: absolute;
  top: 16px;
  right: 16px;
}
.auth-brand {
  text-align: center;
  margin-bottom: 24px;
}
.auth-brand-title {
  font-size: 22px;
  font-weight: 600;
  letter-spacing: 0.5px;
  color: #4096ff;
}
.auth-brand-sub {
  font-size: 12px;
  opacity: 0.55;
  margin-top: 2px;
}
.auth-card-wrap {
  width: 100%;
  max-width: 420px;
}
.auth-footer {
  margin-top: 24px;
  font-size: 11px;
  opacity: 0.5;
}
@media (max-width: 480px) {
  .auth-card-wrap {
    max-width: 100%;
  }
}
</style>
