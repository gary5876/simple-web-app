import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { jsonResponse, stubFetch } from '../test/http'
import { TEST_USER, createTestQueryClient } from '../test/utils'
import { PendingPostsProvider } from './PendingPostsContext'
import { WritePage } from './WritePage'

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

describe('WritePage', () => {
  it('reuses the same Idempotency-Key when the user resubmits after a failure', async () => {
    const fetch = stubFetch((_url, init) =>
      init.method === 'POST' && fetch.mock.calls.length === 1
        ? jsonResponse(500, { code: 'INTERNAL', message: 'boom' })
        : jsonResponse(202, { id: 'p1', status: 'pending' }),
    )
    render(
      <QueryClientProvider client={createTestQueryClient(TEST_USER)}>
        <MemoryRouter initialEntries={['/write']}>
          <PendingPostsProvider>
            <Routes>
              <Route path="/write" element={<WritePage />} />
              <Route path="/" element={<p>목록 화면</p>} />
            </Routes>
          </PendingPostsProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    )

    await userEvent.type(screen.getByLabelText('제목'), '대피소 정보')
    await userEvent.type(screen.getByLabelText('본문'), '체육관 개방')
    await userEvent.click(screen.getByRole('button', { name: '등록' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('오류가 발생했습니다 (500).')

    await userEvent.click(screen.getByRole('button', { name: '등록' }))
    expect(await screen.findByText('목록 화면')).toBeInTheDocument()

    const keys = fetch.mock.calls.map(([, init]) => (init.headers as Record<string, string>)['Idempotency-Key'])
    expect(keys).toHaveLength(2)
    expect(keys[0]).toMatch(UUID)
    expect(keys[1]).toBe(keys[0])
  })
})
