import { describe, expect, it } from 'vitest'
import { loginPath, safeNext } from './redirect'

describe('loginPath', () => {
  it('encodes the return path', () => {
    expect(loginPath('/write?draft=1')).toBe('/login?next=%2Fwrite%3Fdraft%3D1')
  })
})

describe('safeNext', () => {
  it('accepts same-origin paths', () => {
    expect(safeNext('/write')).toBe('/write')
  })
  it.each([null, '', '//evil.example.com', 'https://evil.example.com', '/\\evil.example.com', '/a\\b'])('rejects %s', (raw) => {
    expect(safeNext(raw)).toBe('/')
  })
})
