import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { SignupPage } from './SignupPage'
import { jsonResponse, stubFetch } from '../test/http'
import { LocationDisplay, TEST_USER, createTestQueryClient } from '../test/utils'

function renderSignup() {
  render(
    <QueryClientProvider client={createTestQueryClient(null)}>
      <MemoryRouter initialEntries={['/signup']}>
        <Routes>
          <Route path="/signup" element={<SignupPage />} />
          <Route path="/" element={<LocationDisplay />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

async function fillAndSubmit() {
  await userEvent.type(screen.getByLabelText('이메일'), 'user@example.com')
  await userEvent.type(screen.getByLabelText('닉네임'), '테스터')
  await userEvent.type(screen.getByLabelText('비밀번호'), 'password123')
  await userEvent.click(screen.getByRole('button', { name: '가입하기' }))
}

describe('SignupPage', () => {
  it('signs up, logs in and goes home', async () => {
    stubFetch((url) => (url === '/api/auth/signup' ? jsonResponse(201, TEST_USER) : jsonResponse(200, TEST_USER)))
    renderSignup()
    await fillAndSubmit()
    expect(await screen.findByTestId('location')).toHaveTextContent('/')
  })

  it('shows EMAIL_TAKEN next to the email field', async () => {
    stubFetch(() => jsonResponse(409, { code: 'EMAIL_TAKEN', message: 'x' }))
    renderSignup()
    await fillAndSubmit()
    const alert = await screen.findByText('이미 가입된 이메일입니다.')
    expect(alert.closest('.field')).toContainElement(screen.getByLabelText('이메일'))
    expect(screen.queryByText('이미 사용 중인 닉네임입니다.')).not.toBeInTheDocument()
  })

  it('shows NICKNAME_TAKEN next to the nickname field', async () => {
    stubFetch(() => jsonResponse(409, { code: 'NICKNAME_TAKEN', message: 'x' }))
    renderSignup()
    await fillAndSubmit()
    const alert = await screen.findByText('이미 사용 중인 닉네임입니다.')
    expect(alert.closest('.field')).toContainElement(screen.getByLabelText('닉네임'))
  })

  it('maps 422 fields to their inputs', async () => {
    stubFetch(() =>
      jsonResponse(422, {
        code: 'VALIDATION_ERROR',
        message: 'x',
        fields: [{ loc: ['body', 'nickname'], msg: '너무 짧습니다' }],
      }),
    )
    renderSignup()
    await fillAndSubmit()
    const alert = await screen.findByText('너무 짧습니다')
    expect(alert.closest('.field')).toContainElement(screen.getByLabelText('닉네임'))
  })
})
