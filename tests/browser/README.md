# Browser tests for the control plane

These drive `/admin` in a real Chrome: sign in, every page, the Add an agent form, keyboard use, hostile input,
phone width, content-security-policy violations and stray placeholder text.

```bash
demo/run.sh &                      # the demo they test against, on http://localhost:8013
cd tests/browser && npm install && npm test
```

Set `CHROME_PATH` if Chrome is not at the macOS default, `BASE_URL` to test another address, and
`SHOTS_DIR` to choose where screenshots go (git-ignored).
