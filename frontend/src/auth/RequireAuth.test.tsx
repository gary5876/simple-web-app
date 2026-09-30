import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { RequireAuth } from './RequireAuth'
import { jsonResponse, stubFetch } from '../test/http'
import { LocationDisplay, TEST_USER, createTestQueryClient } from '../test/utils'

function renderAt(path: string, me?: typeof TEST_USER | null) {
  render(
    <QueryClientProvider client={createTestQueryClient(me)}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/write" element={<RequireAuth><p>비밀 화면</p></RequireAuth>} />
          <Route path="/login" element={<LocationDisplay />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('RequireAuth', () => {
  it('redirects guests to login with the return path', async () => {
    renderAt('/write?draft=1', null)
    expect(await screen.findByTestId('location')).toHaveTextContent('/login?next=%2Fwrite%3Fdraft%3D1')
    expect(screen.queryByText('비밀 화면')).not.toBeInTheDocument()
  })

  it('renders children for logged-in users', () => {
    renderAt('/write', TEST_USER)
    expect(screen.getByText('비밀 화면')).toBeInTheDocument()
  })

  // api 클라이언트가 503을 3회 재시도(약 3.5초)한 뒤에야 쿼리가 실패한다.
  it('does not treat a 503 from /me as logged out, and retry refetches', async () => {
    let up = false
    const fetch = stubFetch(() =>
      up ? jsonResponse(200, TEST_USER) : jsonResponse(503, { code: 'UNAVAILABLE', message: 'x' }),
    )
    renderAt('/write')
    expect(await screen.findByText(/일시적으로 사용할 수 없습니다/, undefined, { timeout: 8000 })).toBeInTheDocument()
    expect(screen.queryByTestId('location')).not.toBeInTheDocument()
    up = true
    const before = fetch.mock.calls.length
    await userEvent.click(screen.getByRole('button', { name: '다시 시도' }))
    expect(await screen.findByText('비밀 화면')).toBeInTheDocument()
    await waitFor(() => expect(fetch.mock.calls.length).toBeGreaterThan(before))
  }, 20000)
})
