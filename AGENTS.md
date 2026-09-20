# SNS Media Collector

- Preserve existing working behavior unless the task explicitly requires changing it.
- Never expose, commit, log, or paste authentication secrets such as X cookies or pixiv refresh tokens.
- Use the relevant repository skill under `.agents/skills` when the task matches it.
- Run tests appropriate to the changed area before considering implementation complete.
- Prefer targeted tests during iteration; run the full validation flow when preparing a release or when changes have broad impact.
- Do not rewrite unrelated code merely for cleanup.
