/**
 * useMultiSSE.test.ts
 *
 * Pins the v0.2 SSE-merge composable:
 *   1. ``EventSource`` is opened with the multiplexed URL when the
 *      channels list is non-empty.
 *   2. Frames carrying ``channel:`` get demultiplexed into the matching
 *      ``onChannel[selector]`` handler.
 *   3. ``hello`` / ``keepalive`` route under reserved sentinel channel
 *      names so callers can wire ready / heartbeat indicators.
 *   4. Frames for an unrelated channel never reach the per-channel
 *      handler (only ``onMessage`` gets a copy).
 *   5. ``close()`` tears the connection down (idempotent).
 *
 * The test stubs the global ``EventSource`` with a small driver so we
 * can synthesise frames without an HTTP roundtrip; same approach as
 * the existing JobDetail / LogTailPanel tests.
 *
 * @vitest-environment jsdom
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { ref, defineComponent, h, nextTick } from 'vue';
import { mount } from '@vue/test-utils';
import { useMultiSSE } from '../useMultiSSE';

// ---------------------------------------------------------------------------
// EventSource stub
// ---------------------------------------------------------------------------

interface FakeEventSource {
  url: string;
  closed: boolean;
  listeners: Map<string, Array<(ev: MessageEvent) => void>>;
  onopen: (() => void) | null;
  onmessage: ((ev: MessageEvent) => void) | null;
  onerror: (() => void) | null;
  addEventListener: (type: string, fn: (ev: MessageEvent) => void) => void;
  close: () => void;
  /** Test helper: synthesise a frame as if the server sent one. */
  emit: (eventType: string, payload: unknown) => void;
  /** Test helper: synthesise an error so the reconnect path runs. */
  triggerError: () => void;
}

let lastSource: FakeEventSource | null = null;
let allSources: FakeEventSource[] = [];

function makeFakeEventSource(): typeof EventSource {
  // Use a class-like factory that captures the test driver references
  // when the SUT calls ``new EventSource(url)``.
  function Ctor(this: FakeEventSource, url: string) {
    this.url = url;
    this.closed = false;
    this.listeners = new Map();
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
    this.addEventListener = (type: string, fn: (ev: MessageEvent) => void) => {
      const arr = this.listeners.get(type) ?? [];
      arr.push(fn);
      this.listeners.set(type, arr);
    };
    this.close = () => {
      this.closed = true;
    };
    this.emit = (eventType: string, payload: unknown) => {
      const ev = { data: JSON.stringify(payload) } as MessageEvent;
      const arr = this.listeners.get(eventType);
      if (arr) {
        for (const fn of arr) fn(ev);
      } else if (this.onmessage) {
        this.onmessage(ev);
      }
    };
    this.triggerError = () => {
      if (this.onerror) this.onerror();
    };
    lastSource = this;
    allSources.push(this);
  }
  return Ctor as unknown as typeof EventSource;
}

// ---------------------------------------------------------------------------
// Mounting harness — useMultiSSE relies on Vue lifecycle hooks, so we
// have to instantiate it inside a component rather than calling it
// raw.
// ---------------------------------------------------------------------------

function mountWithChannels(
  channels: string[],
  handlers: Parameters<typeof useMultiSSE>[1] = {},
) {
  const channelsRef = ref(channels);
  // Capture the composable's return value into a closure rather than
  // exposing it via setup() — Vue's setup-return ref-unwrap collapses
  // the Ref<boolean> typing we want to assert on.
  let captured: ReturnType<typeof useMultiSSE> | null = null;
  const cmp = defineComponent({
    setup() {
      captured = useMultiSSE(channelsRef, handlers);
      return () => h('div');
    },
  });
  const wrapper = mount(cmp);
  if (!captured) throw new Error('useMultiSSE never ran in setup');
  return { wrapper, channelsRef, api: captured };
}

// ---------------------------------------------------------------------------
// Suite
// ---------------------------------------------------------------------------

describe('useMultiSSE', () => {
  beforeEach(() => {
    lastSource = null;
    allSources = [];
    // Stub localStorage token so ``connect()`` doesn't bail.
    window.localStorage.setItem('argus.access_token', 'tok-123');
    // Replace global EventSource with the test driver.
    (globalThis as unknown as { EventSource: typeof EventSource }).EventSource =
      makeFakeEventSource();
  });

  afterEach(() => {
    window.localStorage.clear();
  });

  it('opens an EventSource pointing at /api/sse with the channel list', () => {
    mountWithChannels(['batch:b1', 'job:b1:j2']);
    expect(lastSource).not.toBeNull();
    expect(lastSource!.url).toContain('/api/sse?channels=');
    // URL-encoded comma is %2C.
    expect(decodeURIComponent(lastSource!.url)).toContain('batch:b1,job:b1:j2');
    expect(decodeURIComponent(lastSource!.url)).toContain('token=tok-123');
  });

  it('does not open a connection when channel list is empty', () => {
    mountWithChannels([]);
    expect(lastSource).toBeNull();
  });

  it('skips connect when no auth token is set', () => {
    window.localStorage.removeItem('argus.access_token');
    const { api } = mountWithChannels(['batch:b1']);
    expect(lastSource).toBeNull();
    expect(api.error.value).toBe('no auth token');
  });

  it('demultiplexes frames into onChannel handlers by channel selector', async () => {
    const batchSpy = vi.fn();
    const jobSpy = vi.fn();
    mountWithChannels(['batch:b1', 'job:b1:j2'], {
      onChannel: { 'batch:b1': batchSpy, 'job:b1:j2': jobSpy },
    });

    // Hand-rolled fake server frames.
    lastSource!.emit('job_epoch', {
      channel: 'job:b1:j2',
      event_type: 'job_epoch',
      batch_id: 'b1',
      job_id: 'j2',
      data: { epoch: 3 },
    });
    lastSource!.emit('batch_done', {
      channel: 'batch:b1',
      event_type: 'batch_done',
      batch_id: 'b1',
      data: { final: true },
    });

    expect(jobSpy).toHaveBeenCalledTimes(1);
    expect(jobSpy.mock.calls[0][0]).toBe('job_epoch');
    expect((jobSpy.mock.calls[0][1] as Record<string, unknown>).job_id).toBe('j2');

    expect(batchSpy).toHaveBeenCalledTimes(1);
    expect(batchSpy.mock.calls[0][0]).toBe('batch_done');
  });

  it('routes hello / keepalive under reserved sentinel channels', () => {
    const helloSpy = vi.fn();
    const keepaliveSpy = vi.fn();
    const messageSpy = vi.fn();
    mountWithChannels(['batch:b1'], {
      onChannel: { __hello__: helloSpy, __keepalive__: keepaliveSpy },
      onMessage: messageSpy,
    });

    // Hello carries no channel field — must dispatch via __hello__.
    lastSource!.emit('hello', { subscribed: true, channels: ['batch:b1'] });
    lastSource!.emit('keepalive', '12345');

    expect(helloSpy).toHaveBeenCalledTimes(1);
    expect(helloSpy.mock.calls[0][0]).toBe('hello');
    expect(keepaliveSpy).toHaveBeenCalledTimes(1);
    // ``onMessage`` is the catch-all so it gets every frame.
    expect(messageSpy.mock.calls.map((c) => c[0])).toEqual(['__hello__', '__keepalive__']);
  });

  it('does not deliver a frame to an unrelated per-channel handler', () => {
    const batchSpy = vi.fn();
    const otherSpy = vi.fn();
    mountWithChannels(['batch:b1', 'batch:b2'], {
      onChannel: { 'batch:b1': batchSpy, 'batch:b2': otherSpy },
    });

    lastSource!.emit('job_epoch', {
      channel: 'batch:b1',
      event_type: 'job_epoch',
      batch_id: 'b1',
      data: {},
    });

    expect(batchSpy).toHaveBeenCalledTimes(1);
    expect(otherSpy).not.toHaveBeenCalled();
  });

  it('marks connected=true on open and resets to false on error', async () => {
    const { api } = mountWithChannels(['batch:b1']);
    expect(lastSource).not.toBeNull();
    // Drive the open handler explicitly — JS doesn't run microtasks for us.
    lastSource!.onopen?.();
    expect(api.connected.value).toBe(true);

    lastSource!.triggerError();
    expect(api.connected.value).toBe(false);
    expect(api.error.value).toBe('stream disconnected');
  });

  it('close() tears down the EventSource and is idempotent', () => {
    const { api } = mountWithChannels(['batch:b1']);
    const src = lastSource!;
    expect(src.closed).toBe(false);
    api.close();
    expect(src.closed).toBe(true);
    // Calling close() a second time must not raise.
    api.close();
    expect(src.closed).toBe(true);
  });

  it('reopens the connection when the channel ref changes', async () => {
    const { channelsRef } = mountWithChannels(['batch:b1']);
    const firstUrl = lastSource!.url;
    channelsRef.value = ['batch:b1', 'job:b1:j2'];
    await nextTick();
    expect(allSources.length).toBeGreaterThanOrEqual(2);
    expect(allSources[0].closed).toBe(true);
    expect(allSources[allSources.length - 1].url).not.toBe(firstUrl);
    expect(decodeURIComponent(allSources[allSources.length - 1].url)).toContain(
      'job:b1:j2',
    );
  });
});
