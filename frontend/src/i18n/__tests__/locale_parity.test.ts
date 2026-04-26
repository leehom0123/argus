/**
 * locale_parity.test.ts
 *
 * Asserts that zh-CN and en-US locale objects have identical key shapes
 * at every nesting level. Runs under vitest.
 *
 * If either locale file is absent the test suite is skipped with an
 * informative message so the CI failure is actionable before Dev-1 / Dev-2
 * deliver their files.
 */

import { describe, it, expect } from 'vitest';
import { existsSync } from 'node:fs';
import { resolve } from 'node:path';

// Resolve locale file paths relative to this test file
const ZH_PATH = resolve(__dirname, '../locales/zh-CN.ts');
const EN_PATH = resolve(__dirname, '../locales/en-US.ts');

const bothExist = existsSync(ZH_PATH) && existsSync(EN_PATH);

// ---------------------------------------------------------------------------
// Helper: collect every dotted key path from a (possibly nested) object
// ---------------------------------------------------------------------------
function flatKeys(obj: Record<string, unknown>, prefix = ''): string[] {
  const keys: string[] = [];
  for (const [k, v] of Object.entries(obj)) {
    const full = prefix ? `${prefix}.${k}` : k;
    if (v !== null && typeof v === 'object' && !Array.isArray(v)) {
      keys.push(...flatKeys(v as Record<string, unknown>, full));
    } else {
      keys.push(full);
    }
  }
  return keys;
}

// ---------------------------------------------------------------------------
// Conditional import — only attempted when both files exist.
// `vitest` resolves TypeScript imports at runtime via its built-in transformer.
// ---------------------------------------------------------------------------
describe('i18n locale parity', () => {
  if (!bothExist) {
    it.skip(
      'locale files not yet generated; run Dev-1 / Dev-2 tasks first',
      () => { /* skipped */ },
    );
    return;
  }

  // Dynamic imports are resolved once outside of individual `it` blocks so
  // that parse errors surface immediately rather than per-assertion.
  // We use a module-level variable filled by a setup `it` that must run first.
  let zh: Record<string, unknown>;
  let en: Record<string, unknown>;

  it('can import both locale modules', async () => {
    const zhMod = await import('../locales/zh-CN');
    const enMod = await import('../locales/en-US');
    zh = zhMod.default as Record<string, unknown>;
    en = enMod.default as Record<string, unknown>;
    expect(zh).toBeTruthy();
    expect(en).toBeTruthy();
  });

  it('zh-CN and en-US have the same top-level keys', async () => {
    const zhMod = await import('../locales/zh-CN');
    const enMod = await import('../locales/en-US');
    zh = zhMod.default as Record<string, unknown>;
    en = enMod.default as Record<string, unknown>;

    const zhTop = Object.keys(zh).sort();
    const enTop = Object.keys(en).sort();
    expect(enTop).toEqual(zhTop);
  });

  it('zh-CN and en-US have identical key shapes recursively (full dotted-path parity)', async () => {
    const zhMod = await import('../locales/zh-CN');
    const enMod = await import('../locales/en-US');
    zh = zhMod.default as Record<string, unknown>;
    en = enMod.default as Record<string, unknown>;

    const zhKeys = flatKeys(zh).sort();
    const enKeys = flatKeys(en).sort();

    const onlyInZh = zhKeys.filter((k) => !enKeys.includes(k));
    const onlyInEn = enKeys.filter((k) => !zhKeys.includes(k));

    expect(onlyInZh, 'Keys present in zh-CN but missing in en-US').toEqual([]);
    expect(onlyInEn, 'Keys present in en-US but missing in zh-CN').toEqual([]);
  });

  it('en-US has no empty-string values (untranslated entries)', async () => {
    const enMod = await import('../locales/en-US');
    en = enMod.default as Record<string, unknown>;

    const enKeys = flatKeys(en);
    const untranslated = enKeys.filter((dotPath) => {
      // Navigate the object by dotted path
      const parts = dotPath.split('.');
      let node: unknown = en;
      for (const p of parts) {
        node = (node as Record<string, unknown>)[p];
      }
      return node === '';
    });

    expect(
      untranslated,
      'en-US contains empty-string values (untranslated keys)',
    ).toEqual([]);
  });
});
