import { expect, test } from '@playwright/test'

test('signup → write a post → see it published → delete account', async ({ page }) => {
  const id = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`
  const email = `e2e-${id}@example.com`
  const password = 'e2e-password-123'
  const nickname = `e2e${id}`.slice(0, 20)
  const title = `대피소 안내 ${id}`

  await page.goto('/signup')
  await page.getByLabel('이메일').fill(email)
  await page.getByLabel('닉네임').fill(nickname)
  await page.getByLabel('비밀번호').fill(password)
  await page.getByRole('button', { name: '가입하기' }).click()
  await expect(page.getByRole('link', { name: '내 정보' })).toBeVisible()

  await page.getByRole('link', { name: '글쓰기' }).click()
  await page.getByLabel('제목').fill(title)
  await page.getByLabel('본문').fill('시청 체육관이 대피소로 개방되었습니다.')
  await page.getByRole('button', { name: '등록' }).click()
  await expect(page).toHaveURL('/')

  // 먼저 "게시 중…"으로 보이고, 워커가 저장한 뒤 폴링(5초)으로 실제 글 링크로 바뀐다.
  const item = page.getByRole('listitem').filter({ hasText: title })
  await expect(item.first()).toBeVisible()
  await expect(item.getByRole('link', { name: title })).toBeVisible({ timeout: 20_000 })
  await expect(item.getByText('게시 중…')).toHaveCount(0)

  await page.getByRole('link', { name: '내 정보' }).click()
  await page.getByLabel('비밀번호 확인').fill(password)
  await page.getByRole('button', { name: '회원 탈퇴' }).click()
  await expect(page.getByRole('link', { name: '로그인' })).toBeVisible()
})
