import { computed, type ComputedRef } from 'vue';
import { useAuthStore } from '../store/auth';

/**
 * Single source of truth for "is this view allowed to mutate?" logic.
 *
 * The `readOnly` prop flows in from the router for public-demo /
 * public-share entry points (/demo/*, /public/:slug). When the caller
 * passes a boolean it is respected verbatim — even a signed-in user
 * visiting /demo/projects/foo gets a read-only experience, which lets
 * us preview what anonymous visitors see.
 *
 * When no explicit value is given we fall back to the auth store:
 * signed-in → writable, anonymous → read-only.
 */
export interface PermissionState {
  /** True when the current view must not mutate anything. */
  isReadOnly: ComputedRef<boolean>;
  /** Convenience inverse of isReadOnly. Use `v-if="canWrite"` on buttons. */
  canWrite: ComputedRef<boolean>;
  /** True specifically when the visitor is anonymous (no session). */
  isAnonymous: ComputedRef<boolean>;
}

export function usePermissions(readOnly?: boolean): PermissionState {
  const auth = useAuthStore();

  const isAnonymous = computed(() => !auth.isAuthenticated);

  const isReadOnly = computed(() => {
    // Explicit prop wins — lets logged-in users preview the demo.
    if (typeof readOnly === 'boolean') return readOnly;
    // Otherwise derive from auth state.
    return isAnonymous.value;
  });

  const canWrite = computed(() => !isReadOnly.value);

  return { isReadOnly, canWrite, isAnonymous };
}
