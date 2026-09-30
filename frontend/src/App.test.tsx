import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { App } from './App'
import { jsonResponse, stubFetch } from './test/http'
import { createTestQueryClient } from './test/utils'

describe('App', () => {
  it('shows the board and guest navigation', async () => {
    stubFetch((url) =>
      url === '/api/auth/me'
        ? jsonResponse(401, { code: 'UNAUTHORIZED', message: 'login required' })
        : jsonResponse(200, { items: [], next_cursor: null }),
    )
    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    expect(await screen.findByRole('heading', { name: '게시판' })).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: '로그인' })).toBeInTheDocument()
  })
})
