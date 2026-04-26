<script setup lang="ts">
import { ref, computed } from 'vue';
import { useI18n } from 'vue-i18n';
import { GithubOutlined } from '@ant-design/icons-vue';

const { t } = useI18n();

interface EnvSnapshot {
  git_sha?: string | null;
  /** First 8 chars of git_sha (#18 — derived server-side). Fallback: compute locally. */
  git_sha_short?: string | null;
  /** Normalised HTTPS repo URL, e.g. ``https://github.com/user/repo`` (#18). */
  git_remote_url?: string | null;
  git_branch?: string | null;
  git_dirty?: boolean | null;
  python_version?: string | null;
  pip_freeze?: string[] | null;
  hydra_config_digest?: string | null;
  hydra_config_content?: string | null;
  hostname?: string | null;
}

const props = defineProps<{
  envSnapshot?: EnvSnapshot | null;
}>();

// ── Modals ──────────────────────────────────────────────────────────────────
const depsModalOpen = ref(false);
const hydraModalOpen = ref(false);

// ── Git SHA chip ─────────────────────────────────────────────────────────────

const shortSha = computed(() => {
  // Prefer the server-derived short SHA (#18); fall back to slicing the
  // full SHA client-side for batches that predate the enrichment.
  const fromServer = props.envSnapshot?.git_sha_short;
  if (fromServer) return fromServer;
  const sha = props.envSnapshot?.git_sha;
  return sha ? sha.slice(0, 8) : null;
});

/**
 * Commit URL for the chip link (#18). Only produced when *both*
 * ``git_sha`` and ``git_remote_url`` are present on the snapshot — otherwise
 * the chip stays non-clickable so we never point users at a made-up URL.
 * ``git_remote_url`` is already normalised by the backend (strips .git,
 * converts SSH → HTTPS) so we can concatenate ``/commit/<sha>`` safely.
 */
const shaHref = computed<string | null>(() => {
  const sha = props.envSnapshot?.git_sha;
  const remote = props.envSnapshot?.git_remote_url;
  if (!sha || !remote) return null;
  // Strip trailing slash just in case; keep the full 40-char SHA in the URL
  // (GitHub accepts both the short and full form; full is unambiguous).
  const clean = remote.replace(/\/+$/, '');
  return `${clean}/commit/${sha}`;
});

/** Does the remote look like github.com? Controls whether we render the icon. */
const isGithubRemote = computed(() => {
  const remote = props.envSnapshot?.git_remote_url ?? '';
  return /github\.com/i.test(remote);
});

// ── Python version chip ───────────────────────────────────────────────────

const pythonLabel = computed(() => {
  const v = props.envSnapshot?.python_version;
  return v ? `Python ${v}` : null;
});

// ── Deps modal ────────────────────────────────────────────────────────────

const depsText = computed(() => {
  const freeze = props.envSnapshot?.pip_freeze;
  if (!freeze || freeze.length === 0) return '';
  return freeze.join('\n');
});

const depsCount = computed(() => props.envSnapshot?.pip_freeze?.length ?? 0);

// ── Hydra config modal ────────────────────────────────────────────────────

const hydraContent = computed(() => props.envSnapshot?.hydra_config_content ?? '');
const hydraDigest = computed(() => {
  const d = props.envSnapshot?.hydra_config_digest;
  return d ? d.slice(0, 12) : null;
});

// ── Visibility guard ──────────────────────────────────────────────────────

const hasAny = computed(() =>
  !!(
    props.envSnapshot?.git_sha ||
    props.envSnapshot?.git_dirty != null ||
    props.envSnapshot?.python_version ||
    (props.envSnapshot?.pip_freeze?.length ?? 0) > 0 ||
    props.envSnapshot?.hydra_config_content
  ),
);
</script>

<template>
  <div
    v-if="envSnapshot && hasAny"
    style="display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-bottom: 10px"
  >
    <!-- Git SHA chip (#18 — clickable only when git_remote_url + git_sha
         are both present on env_snapshot). Non-clickable otherwise so we
         never fabricate commit URLs. GitHub icon renders when the remote
         looks like github.com; otherwise it's a plain tag link. -->
    <a-tooltip
      v-if="shortSha"
      :title="shaHref ? t('component_repro_chip_row.git_sha_open_github') : (envSnapshot?.git_sha ?? '')"
    >
      <a-tag
        color="purple"
        :style="{
          fontSize: '11px',
          lineHeight: '18px',
          padding: '0 6px',
          cursor: shaHref ? 'pointer' : 'default',
        }"
      >
        <a
          v-if="shaHref"
          :href="shaHref"
          target="_blank"
          rel="noopener noreferrer"
          style="color: inherit; text-decoration: none; display: inline-flex; align-items: center; gap: 4px"
        >
          <GithubOutlined
            v-if="isGithubRemote"
            style="font-size: 11px"
          />
          <span>{{ t('component_repro_chip_row.git_sha_prefix') }} {{ shortSha }}</span>
        </a>
        <span v-else>{{ t('component_repro_chip_row.git_sha_prefix') }} {{ shortSha }}</span>
      </a-tag>
    </a-tooltip>

    <!-- Git branch chip -->
    <a-tag
      v-if="envSnapshot?.git_branch"
      color="geekblue"
      style="font-size: 11px; line-height: 18px; padding: 0 6px"
    >
      {{ envSnapshot.git_branch }}
    </a-tag>

    <!-- Dirty working-tree warning -->
    <a-tag
      v-if="envSnapshot?.git_dirty"
      color="orange"
      style="font-size: 11px; line-height: 18px; padding: 0 6px"
    >
      {{ t('component_repro_chip_row.dirty_warning') }}
    </a-tag>

    <!-- Python version chip -->
    <a-tag
      v-if="pythonLabel"
      color="cyan"
      style="font-size: 11px; line-height: 18px; padding: 0 6px"
    >
      {{ pythonLabel }}
    </a-tag>

    <!-- View deps button -->
    <a-button
      v-if="depsCount > 0"
      size="small"
      style="font-size: 11px; height: 22px; line-height: 20px"
      @click="depsModalOpen = true"
    >
      {{ t('component_repro_chip_row.view_deps', { n: depsCount }) }}
    </a-button>

    <!-- View Hydra config button -->
    <a-button
      v-if="hydraContent"
      size="small"
      style="font-size: 11px; height: 22px; line-height: 20px"
      @click="hydraModalOpen = true"
    >
      {{ t('component_repro_chip_row.view_hydra_config') }}
      <span v-if="hydraDigest" class="muted" style="font-size: 10px; margin-left: 3px">
        ({{ hydraDigest }})
      </span>
    </a-button>
  </div>

  <!-- Deps modal -->
  <a-modal
    v-model:open="depsModalOpen"
    :title="t('component_repro_chip_row.deps_modal_title', { n: depsCount })"
    :footer="null"
    width="560"
  >
    <div
      style="
        max-height: 420px;
        overflow-y: auto;
        font-family: monospace;
        font-size: 12px;
        white-space: pre;
        background: #1a1a1a;
        color: #d9d9d9;
        padding: 12px 14px;
        border-radius: 6px;
        line-height: 1.7;
      "
    >{{ depsText }}</div>
  </a-modal>

  <!-- Hydra config modal -->
  <a-modal
    v-model:open="hydraModalOpen"
    :title="t('component_repro_chip_row.hydra_modal_title')"
    :footer="null"
    width="640"
  >
    <div
      style="
        max-height: 480px;
        overflow-y: auto;
        font-family: monospace;
        font-size: 12px;
        white-space: pre-wrap;
        background: #1a1a1a;
        color: #d9d9d9;
        padding: 12px 14px;
        border-radius: 6px;
        line-height: 1.7;
      "
    >{{ hydraContent }}</div>
    <div v-if="hydraDigest" class="muted" style="font-size: 11px; margin-top: 6px; text-align: right">
      sha256: {{ envSnapshot?.hydra_config_digest }}
    </div>
  </a-modal>
</template>
