# CDP Browser Automation

Use Chrome DevTools Protocol automation only when a permitted HTTP or public API route cannot do
the job. Prefer read-only operations and a dedicated profile separate from personal browsing data.

## Standard flow

1. Check for an existing compatible endpoint and reuse it.
2. Start or attach to a dedicated profile on an explicit local port.
3. Connect with a CLI client such as Playwright over CDP.
4. Read only pages, cookies, or metadata required for the approved task.
5. Leave sign-in, challenges, credential entry, and account recovery to a human.

```sh
tool cdp status
tool cdp start --no-incognito --port 9222
npx playwright --cdp-endpoint http://127.0.0.1:9222
```

Use a persistent dedicated profile when a human-established session must survive restarts. Never
print, copy, log, or commit credentials, account identities, cookies, or secret values. If the port
is occupied, inspect its holder; reuse the intended browser or choose an unused port rather than
killing an unknown process.

## Never steal focus

Prefer non-browser routes. Attach to an existing context rather than creating tabs. Never call
`bringToFront`, `Page.bringToFront`, `Target.activateTarget`, or an equivalent foregrounding API.
A background-page flag is not a guarantee against an operating-system focus change, so avoid new
targets when focus safety matters. Never point automation at a personal browser profile.
