import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { loginPath } from './redirect'
import { useMe } from './useMe'

export function RequireAuth({ children }: { children: ReactNode }) {
  const { data: me, isPending } = useMe()
  const location = useLocation()
  if (isPending) return <p>불러오는 중…</p>
  if (!me) return <Navigate to={loginPath(location.pathname + location.search)} replace />
  return <>{children}</>
}
