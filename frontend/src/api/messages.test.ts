import { describe, expect, it } from 'vitest'
import { ApiError } from './client'
import { errorMessage } from './messages'

describe('errorMessage', () => {
  it('maps known codes to Korean messages', () => {
    expect(errorMessage(new ApiError(409, 'EMAIL_TAKEN', 'x'))).toBe('이미 가입된 이메일입니다.')
  })
  it('falls back to the status for unknown codes', () => {
    expect(errorMessage(new ApiError(418, 'TEAPOT', 'x'))).toBe('오류가 발생했습니다 (418).')
  })
  it('treats non-API errors as network errors', () => {
    expect(errorMessage(new TypeError('Failed to fetch'))).toBe('네트워크 오류가 발생했습니다.')
  })
})
