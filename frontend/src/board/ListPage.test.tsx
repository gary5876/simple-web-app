import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import type { Post } from '../api/types'
import { jsonResponse, stubFetch } from '../test/http'
import { createTestQueryClient } from '../test/utils'
import { ListPage } from './ListPage'
import { PendingPostsProvider } from './PendingPostsContext'
import type { PendingPost } from './pending'

function post(id: string, title: string): Post {
  return { id, author_id: 'u1', author_nickname: '작성자', title, body: 'b', created_at: '2026-09-30T00:00:00Z' }
}

function pending(id: string, title: string, state: PendingPost['state'] = 'pending'): PendingPost {
  return { id, title, body: 'b', author_nickname: '테스터', submittedAt: Date.now(), state }
}

function renderList(initialPendings: PendingPost[]) {
  render(
    <QueryClientProvider client={createTestQueryClient(null)}>
      <MemoryRouter>
        <PendingPostsProvider initialPendings={initialPendings}>
          <ListPage />
        </PendingPostsProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('ListPage', () => {
  it('replaces a pending post once the server returns it', async () => {
    stubFetch(() => jsonResponse(200, { items: [post('p1', '서버에 반영된 글')], next_cursor: null }))
    renderList([pending('p1', '서버에 반영된 글'), pending('p2', '아직 대기 중인 글')])

    expect(await screen.findByRole('link', { name: '서버에 반영된 글' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByText('게시 중…')).toHaveLength(1))
    expect(screen.getByText('아직 대기 중인 글')).toBeInTheDocument()
  })

  it('offers a retry button for failed posts', async () => {
    stubFetch(() => jsonResponse(200, { items: [], next_cursor: null }))
    renderList([pending('p3', '실패한 글', 'failed')])
    expect(await screen.findByText('게시 실패')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '다시 시도' })).toBeInTheDocument()
  })

  it('shows a load-more button when there is a next page', async () => {
    stubFetch(() => jsonResponse(200, { items: [post('p4', '첫 페이지 글')], next_cursor: 'p4' }))
    renderList([])
    expect(await screen.findByRole('button', { name: '더 보기' })).toBeInTheDocument()
  })
})
