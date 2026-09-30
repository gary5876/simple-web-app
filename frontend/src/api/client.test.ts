import { describe, expect, it, vi } from 'vitest'
import { ApiError, MAX_RETRIES, computeBackoff, createClient } from './client'
import { jsonResponse } from '../test/http'

function setup(responses: Response[]) {
  const fetch = vi.fn(async (_input: string, _init: RequestInit) => {
    const next = responses.shift()
    if (!next) throw new Error('no more responses')
    return next
  })
  const sleep = vi.fn(async (_ms: number) => {})
  const client = createClient({ fetch, sleep, random: () => 0 })
  return { client, fetch, sleep }
}

describe('computeBackoff', () => {
  it('grows exponentially from 500ms', () => {
    expect(computeBackoff(0, null, () => 0)).toBe(500)
    expect(computeBackoff(2, null, () => 0)).toBe(2000)
  })
  it('caps at 8000ms plus jitter', () => {
    expect(computeBackoff(10, null, () => 0.999)).toBe(8000 + 249)
  })
  it('honors Retry-After when it is longer', () => {
    expect(computeBackoff(0, 5, () => 0)).toBe(5000)
    expect(computeBackoff(3, 1, () => 0)).toBe(4000)
  })
})

describe('request', () => {
  it('returns parsed JSON on success', async () => {
    const { client, fetch } = setup([jsonResponse(200, { ok: true })])
    await expect(client.request('/api/x')).resolves.toEqual({ ok: true })
    expect(fetch).toHaveBeenCalledWith('/api/x', expect.objectContaining({ method: 'GET', credentials: 'same-origin' }))
  })

  it('returns undefined on 204', async () => {
    const { client } = setup([new Response(null, { status: 204 })])
    await expect(client.request('/api/x', { method: 'POST' })).resolves.toBeUndefined()
  })

  it('sends JSON bodies with a content type', async () => {
    const { client, fetch } = setup([jsonResponse(201, {})])
    await client.request('/api/x', { method: 'POST', body: { a: 1 } })
    const init = fetch.mock.calls[0][1]
    expect(init.body).toBe('{"a":1}')
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json')
  })

  it('throws ApiError with the server error code', async () => {
    const { client } = setup([jsonResponse(409, { code: 'EMAIL_TAKEN', message: 'taken' })])
    const err = await client.request('/api/x').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect(err).toMatchObject({ status: 409, code: 'EMAIL_TAKEN', message: 'taken' })
  })

  it('falls back to HTTP_<status> for non-JSON errors', async () => {
    const { client } = setup([new Response('<html>bad gateway</html>', { status: 502 })])
    await expect(client.request('/api/x')).rejects.toMatchObject({ status: 502, code: 'HTTP_502' })
  })

  it('does not retry 500', async () => {
    const { client, fetch } = setup([jsonResponse(500, { code: 'INTERNAL', message: 'boom' })])
    await expect(client.request('/api/x')).rejects.toMatchObject({ status: 500 })
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('retries 429 using Retry-After and notifies the listener', async () => {
    const { client, fetch, sleep } = setup([
      jsonResponse(429, { code: 'RATE_LIMITED', message: 'slow down' }, { 'Retry-After': '2' }),
      jsonResponse(200, { ok: true }),
    ])
    const listener = vi.fn()
    client.onRetry(listener)
    await expect(client.request('/api/x')).resolves.toEqual({ ok: true })
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(sleep).toHaveBeenCalledWith(2000)
    expect(listener).toHaveBeenCalledWith({ attempt: 1, delayMs: 2000, status: 429 })
  })

  it(`gives up after ${MAX_RETRIES} retries on 503`, async () => {
    const busy = () => jsonResponse(503, { code: 'QUEUE_FULL', message: 'busy' })
    const { client, fetch } = setup([busy(), busy(), busy(), busy()])
    await expect(client.request('/api/x')).rejects.toMatchObject({ status: 503, code: 'QUEUE_FULL' })
    expect(fetch).toHaveBeenCalledTimes(MAX_RETRIES + 1)
  })

  it('does not auto-retry when Retry-After exceeds 30 seconds', async () => {
    const { client, fetch, sleep } = setup([
      jsonResponse(429, { code: 'RATE_LIMITED', message: 'slow' }, { 'Retry-After': '31' }),
    ])
    await expect(client.request('/api/x')).rejects.toMatchObject({ status: 429, retryAfter: 31 })
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(sleep).not.toHaveBeenCalled()
  })

  it('still retries when Retry-After is exactly 30 seconds', async () => {
    const { client, sleep } = setup([
      jsonResponse(429, { code: 'RATE_LIMITED', message: 'slow' }, { 'Retry-After': '30' }),
      jsonResponse(200, { ok: true }),
    ])
    await expect(client.request('/api/x')).resolves.toEqual({ ok: true })
    expect(sleep).toHaveBeenCalledWith(30000)
  })

  it('never auto-retries TOO_MANY_ATTEMPTS', async () => {
    const { client, fetch, sleep } = setup([
      jsonResponse(429, { code: 'TOO_MANY_ATTEMPTS', message: 'locked' }, { 'Retry-After': '2' }),
    ])
    await expect(client.request('/api/auth/login', { method: 'POST', body: {} })).rejects.toMatchObject({
      status: 429,
      code: 'TOO_MANY_ATTEMPTS',
    })
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(sleep).not.toHaveBeenCalled()
  })

  it('sends identical headers on every retry', async () => {
    const { client, fetch } = setup([
      jsonResponse(503, { code: 'QUEUE_FULL', message: 'busy' }),
      jsonResponse(202, { id: 'p1', status: 'pending' }),
    ])
    await client.request('/api/board/posts', { method: 'POST', body: {}, headers: { 'Idempotency-Key': 'k-1' } })
    const keys = fetch.mock.calls.map(([, init]) => (init.headers as Record<string, string>)['Idempotency-Key'])
    expect(keys).toEqual(['k-1', 'k-1'])
  })
})
