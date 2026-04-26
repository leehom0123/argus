<script setup lang="ts">
import { BulbFilled, BulbOutlined } from '@ant-design/icons-vue';
import { useAppStore } from '../store/app';

const appStore = useAppStore();
</script>

<template>
  <a-tooltip :title="appStore.darkMode ? 'Switch to light' : 'Switch to dark'">
    <a-button type="text" @click="appStore.toggleDark()">
      <template #icon>
        <BulbFilled v-if="appStore.darkMode" />
        <BulbOutlined v-else />
      </template>
    </a-button>
  </a-tooltip>
</template>
