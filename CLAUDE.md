# FreePT for coding agents

FreePT (`freept.py`) routes OpenAI-style chat requests to free-tier LLM providers and falls back
to local Ollama when every free quota is spent. Use it for bulk, checkable work: generating many
candidate rewrites, tests or drafts that a compiler or test suite then verifies.

## Rules
- Keys live in Windows Credential Manager (`FreePT:<VAR>`, set with `FreePT Keys.cmd`) or in
  environment variables. Never read, print, log or copy a key, and never put one in a file or in git.
- One account per provider. Don't suggest making more accounts to stack quotas.
- Free tiers may train on prompts. Only send code and data the user is allowed to share; never
  send secrets, credentials or personal data.
- Free-model output is a candidate, not a result. It counts only after a mechanical check
  (compile, tests, diff) passes, and it then goes through the project's normal review or gate.
- Don't spend your own tokens re-checking what the loop already verified: read the results and
  the checker's output, not the model's chatter.

## Start
```
python freept.py status        # keys found, model picked per provider, today's usage
python freept.py usage         # what's left per provider and when each resets
python freept.py serve         # OpenAI-compatible API at http://127.0.0.1:4000/v1, meter at /
```
On Windows, start the server with `Start FreePT (hidden).vbs` (no window, logs to `server.log`)
and the key window with `FreePT Keys.vbs`. Don't launch it with `cmd /c` from a background job:
with Windows Terminal as the default terminal that leaves a console window open the whole time.

## Routes (the `model` field)
- `freept`: best quality first. `freept/fast`: fastest first.
- `<provider>` or `<provider>/<model-id>`: pin one (`groq`, `cerebras`, `ollama`, ...).

## Handling answers
- HTTP 429 with error type `freept_all_spent`: every provider is used up. Stop the batch cleanly
  (or switch to `ollama`); don't retry in a loop.
- The response has `freept.provider`: log it next to each result.
- Send `max_tokens` (e.g. 5000) for long answers; some providers stop at about 3000 otherwise.
- Ask for JSON and parse defensively: strip code fences, fall back to fenced code blocks.

## Troubleshooting
- `cooling down until ...`: that provider hit a limit; it comes back by itself.
- `FAILED: no preferred model`: the provider renamed its models. Update `prefer=[...]` in
  `PROVIDERS` (pick ids from the provider's `/models` list).
- 402 from a provider: it wants a payment method; FreePT parks it for 7 days.
