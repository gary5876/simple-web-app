import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { LoginPage } from './LoginPage'
import { ME_KEY } from './useMe'
import { jsonResponse, stubFetch } from '../test/http'
import { LocationDisplay, TEST_USER, createTestQueryClient } from '../test/utils'

function renderLogin(client = createTestQueryClient(null)) {
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/login?next=%2Fwrite']}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/write" element={<LocationDisplay />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return client
}

async function submit() {
  await userEvent.type(screen.getByLabelText('이메일'), 'user@example.com')
  await userEvent.type(screen.getByLabelText('비밀번호'), 'password123')
  await userEvent.click(screen.getByRole('button', { name: '로그인' }))
}

describe('LoginPage', () => {
  it('stores the user and returns to the next path', async () => {
    const fetch = stubFetch(() => jsonResponse(200, TEST_USER))
    const client = renderLogin()
    await submit()
    expect(await screen.findByTestId('location')).toHaveTextContent('/write')
    expect(client.getQueryData(ME_KEY)).toEqual(TEST_USER)
    expect(fetch).toHaveBeenCalledWith('/api/auth/login', expect.objectContaining({ method: 'POST' }))
  })

  it('shows a friendly message for wrong credentials', async () => {
    stubFetch(() => jsonResponse(401, { code: 'INVALID_CREDENTIALS', message: 'bad' }))
    renderLogin()
    await submit()
    expect(await screen.findByRole('alert')).toHaveTextContent('이메일 또는 비밀번호가 올바르지 않습니다.')
  })
})
