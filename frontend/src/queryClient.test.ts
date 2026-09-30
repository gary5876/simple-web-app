import { MutationObserver } from '@tanstack/react-query'
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from './api/client'
import { ME_KEY } from './auth/useMe'
import { createQueryClient } from './queryClient'
import { TEST_USER } from './test/utils'

async function failMutation(error: ApiError, onUnauthorized: () => void) {
  const client = createQueryClient(onUnauthorized)
  client.setQueryData(ME_KEY, TEST_USER)
  const observer = new MutationObserver(client, { mutationFn: () => Promise.reject(error) })
  await observer.mutate().catch(() => undefined)
  return client
}

describe('createQueryClient', () => {
  it('clears the user and calls onUnauthorized for UNAUTHORIZED', async () => {
    const onUnauthorized = vi.fn()
    const client = await failMutation(new ApiError(401, 'UNAUTHORIZED', 'x'), onUnauthorized)
    expect(onUnauthorized).toHaveBeenCalledOnce()
    expect(client.getQueryData(ME_KEY)).toBeNull()
  })

  it('ignores other 401 codes such as INVALID_CREDENTIALS', async () => {
    const onUnauthorized = vi.fn()
    const client = await failMutation(new ApiError(401, 'INVALID_CREDENTIALS', 'x'), onUnauthorized)
    expect(onUnauthorized).not.toHaveBeenCalled()
    expect(client.getQueryData(ME_KEY)).toEqual(TEST_USER)
  })
})
