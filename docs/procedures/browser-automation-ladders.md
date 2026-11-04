# Browser Automation Ladders

Choose the least-privileged route that can answer the question:

1. Use a documented public API or static source.
2. Fetch a permitted public page with an ordinary HTTP client.
3. Attach to an already-running browser only when authenticated state is necessary.

For an authenticated browser, never print cookies or credential values, never attempt a login or
challenge bypass autonomously, and stop for human input when the session has expired. Read data
only, apply rate limits, and avoid opening or foregrounding windows unless the user explicitly
asked for it.
