import { describe, expect, it } from 'vitest'
import { STATUS_CHECK_AFTER_MS, applyStatus, dueForStatusCheck, upsertPending, visiblePending, type PendingPost } from './pending'

const NOW = 1_000_000

function pending(id: string, overrides: Partial<PendingPost> = {}): PendingPost {
  return { id, title: `title ${id}`, body: 'body', author_nickname: '테스터', submittedAt: NOW, state: 'pending', ...overrides }
}

describe('upsertPending', () => {
  it('adds new posts to the top without duplicates', () => {
    const list = [pending('a'), pending('b')]
    expect(upsertPending(list, pending('b', { title: 'new' })).map((p) => [p.id, p.title])).toEqual([
      ['b', 'new'],
      ['a', 'title a'],
    ])
  })
})

describe('visiblePending', () => {
  it('hides posts that the server already returned', () => {
    expect(visiblePending([pending('a'), pending('b')], new Set(['a'])).map((p) => p.id)).toEqual(['b'])
  })
})

describe('dueForStatusCheck', () => {
  it('returns non-failed posts older than 30 seconds', () => {
    const list = [
      pending('fresh', { submittedAt: NOW - STATUS_CHECK_AFTER_MS + 1 }),
      pending('old', { submittedAt: NOW - STATUS_CHECK_AFTER_MS }),
      pending('delayed', { submittedAt: NOW - 60_000, state: 'delayed' }),
      pending('failed', { submittedAt: NOW - 60_000, state: 'failed' }),
    ]
    expect(dueForStatusCheck(list, NOW).map((p) => p.id)).toEqual(['old', 'delayed'])
  })
})

describe('applyStatus', () => {
  const list = [pending('a'), pending('b')]
  it('removes published posts', () => {
    expect(applyStatus(list, 'a', 'published').map((p) => p.id)).toEqual(['b'])
  })
  it('marks still-pending posts as delayed', () => {
    expect(applyStatus(list, 'a', 'pending')[0].state).toBe('delayed')
  })
  it('marks failed posts as failed', () => {
    expect(applyStatus(list, 'b', 'failed')[1].state).toBe('failed')
  })
})
