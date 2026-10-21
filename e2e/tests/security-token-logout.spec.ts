import { test, expect } from "../fixtures/engine.ts"

test.describe("token sign-in security", { tag: ["@skip-docs"] }, () => {
  test("signing out revokes the browser session on the daemon", async ({ engine }) => {
    const token = engine.env.ENGINE_SERVICE_TOKEN
    expect(token).toBeTruthy()

    const signIn = await fetch(`${engine.url}/api/auth/token`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    })
    expect(signIn.status).toBe(200)
    const cookie = signIn.headers.get("set-cookie")!.split(";")[0]
    expect(cookie).toMatch(/^engine_token_session=/)

    // A copy of the cookie, such as one lifted from the browser, works while signed in.
    expect((await fetch(`${engine.url}/api/config`, { headers: { Cookie: cookie } })).status).toBe(200)

    const signOut = await fetch(`${engine.url}/api/auth/github/logout`, { method: "POST", headers: { Cookie: cookie } })
    expect(signOut.status).toBe(200)

    // After signing out, the copied cookie must no longer reach the API.
    expect((await fetch(`${engine.url}/api/config`, { headers: { Cookie: cookie } })).status).toBe(401)
  })
})
