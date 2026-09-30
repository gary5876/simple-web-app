import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { jsonResponse, stubFetch } from '../test/http'
import { createTestQueryClient } from '../test/utils'
import { ListPage } from './ListPage'
import { PendingPostsProvider } from './PendingPostsContext'
import { STATUS_CHECK_AFTER_MS, type PendingPost } from './pending'

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

function old(id: string, title: string, state: PendingPost['state'] = 'pending'): PendingPost {
  return { id, title, body: 'b', author_nickname: '테스터', submittedAt: Date.now() - STATUS_CHECK_AFTER_MS - 1000, state }
}

function renderList(initialPendings: PendingPost[]) {
  render(
    <QueryClientProvider client={createTestQueryClient(null)}>
      <MemoryRouter>
        <PendingPostsProvider initialPendings={initialPendings} checkIntervalMs={20}>
          <ListPage />
        </PendingPostsProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const isList = (url: string) => url === '/api/board/posts'
const emptyList = () => jsonResponse(200, { items: [], next_cursor: null })

describe('PendingPostsProvider status check', () => {
  it('does not start a second check while a slow one is in flight', async () => {
    let release: (r: Response) => void = () => {}
    let calls = 0
    stubFetch((url) => {
      if (isList(url)) return emptyList()
      calls += 1
      return new Promise<Response>((resolve) => (release = resolve))
    })
    renderList([old('p1', '느린 글')])
    await waitFor(() => expect(calls).toBe(1))
    await new Promise((r) => setTimeout(r, 120)) // 약 6번의 간격이 지난다
    expect(calls).toBe(1)
    release(jsonResponse(200, { status: 'pending' }))
    expect(await screen.findByText('지연 중')).toBeInTheDocument()
  })

  it('removes a post that turned out to be published', async () => {
    stubFetch((url) => (isList(url) ? emptyList() : jsonResponse(200, { status: 'published', post: {} })))
    renderList([old('p1', '게시된 글')])
    await waitFor(() => expect(screen.queryByText('게시된 글')).not.toBeInTheDocument())
  })

  it('marks a still-pending post as delayed', async () => {
    stubFetch((url) => (isList(url) ? emptyList() : jsonResponse(200, { status: 'pending' })))
    renderList([old('p1', '대기 글')])
    expect(await screen.findByText('지연 중')).toBeInTheDocument()
  })

  it('marks a failed post as failed with a retry button', async () => {
    stubFetch((url) => (isList(url) ? emptyList() : jsonResponse(200, { status: 'failed' })))
    renderList([old('p1', '실패 글')])
    expect(await screen.findByText('게시 실패')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '다시 시도' })).toBeInTheDocument()
  })

  it('treats 404 as failed', async () => {
    stubFetch((url) => (isList(url) ? emptyList() : jsonResponse(404, { code: 'NOT_FOUND', message: 'x' })))
    renderList([old('p1', '사라진 글')])
    expect(await screen.findByText('게시 실패')).toBeInTheDocument()
  })
})

describe('retrying a failed post', () => {
  it('sends a new, distinct Idempotency-Key on every retry', async () => {
    const keys: string[] = []
    stubFetch((url, init) => {
      if (init.method === 'POST') {
        keys.push(new Headers(init.headers).get('Idempotency-Key') ?? '')
        return jsonResponse(202, { id: `n${keys.length}`, status: 'pending' })
      }
      return isList(url) ? emptyList() : jsonResponse(200, { status: 'failed' })
    })
    renderList([old('p1', '실패 글 1', 'failed'), old('p2', '실패 글 2', 'failed')])
    const buttons = await screen.findAllByRole('button', { name: '다시 시도' })
    fireEvent.click(buttons[0])
    await waitFor(() => expect(keys).toHaveLength(1))
    fireEvent.click(await screen.findByRole('button', { name: '다시 시도' }))
    await waitFor(() => expect(keys).toHaveLength(2))
    expect(keys[0]).toMatch(UUID)
    expect(keys[1]).toMatch(UUID)
    expect(keys[0]).not.toBe(keys[1])
  })
})
