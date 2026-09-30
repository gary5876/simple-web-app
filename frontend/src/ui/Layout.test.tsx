import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { Layout } from './Layout'
import { jsonResponse, stubFetch } from '../test/http'
import { createTestQueryClient } from '../test/utils'

describe('Layout', () => {
  it('shows no guest links while login state is unknown (me query errored)', async () => {
    stubFetch(() => jsonResponse(503, { code: 'UNAVAILABLE', message: 'x' }, { 'Retry-After': '0' }))
    const client = createTestQueryClient()
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter><Layout><p>본문</p></Layout></MemoryRouter>
      </QueryClientProvider>,
    )
    await screen.findByText('본문')
    await new Promise((r) => setTimeout(r, 4500))
    expect(client.getQueryState(['me'])?.status).toBe('error')
    expect(screen.queryByText('로그인')).not.toBeInTheDocument()
    expect(screen.queryByText('회원가입')).not.toBeInTheDocument()
  }, 15000)
})
