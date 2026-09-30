import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { RequireAuth } from './RequireAuth'
import { LocationDisplay, TEST_USER, createTestQueryClient } from '../test/utils'

function renderAt(path: string, me: typeof TEST_USER | null) {
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
})
