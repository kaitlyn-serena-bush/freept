# FreePT

FreePT routes OpenAI-style chat requests across free-tier LLM API providers until each
provider's free quota is used up, then moves on to the next one. If every provider is spent,
it falls back to a local [Ollama](https://ollama.com) model when one is running.

- **One file, no dependencies.** `freept.py` uses only the Python standard library (3.8+).
- **OpenAI-compatible server** on `http://127.0.0.1:4000/v1`. Point any OpenAI client at it.
- **Budget tracking.** A local SQLite ledger counts calls per provider against a daily (and,
  where needed, monthly) budget set a little under each free limit.
- **Rotates on errors.** 429 (rate limit), 402 (payment required), 5xx and timeouts put a
  provider on a cooldown of the right length, and the request moves on to the next provider.
  A retired model id (404) is remembered and the next preferred model is picked.
- **Usage meter.** `python freept.py usage`, or a live page at `http://127.0.0.1:4000/`, shows
  what's left per provider, when each budget resets, and the provider's own rate-limit headers.

## Quick start

1. Get free API keys from the providers you want (see the table below). Any subset works.
2. Store the keys:
   - **Windows:** double-click `FreePT Keys.cmd`. It opens a small window that saves each key
     in Windows Credential Manager (as `FreePT:<VAR>`) and has a Test button per key.
   - **macOS / Linux / anywhere:** set environment variables with the names in the table,
     for example `export GROQ_API_KEY=...`.
3. Check them:
   ```
   python freept.py status
   ```
   This lists each provider, whether a key was found, the model FreePT picked, and today's usage.
4. Start the server (or double-click `FreePT Server.cmd` on Windows):
   ```
   python freept.py serve          # port 4000; "serve 5000" for another port
   ```

## Providers

| Provider | Key variable(s) | Get a key | FreePT's default daily budget |
|---|---|---|---|
| Google Gemini (flash models) | `GEMINI_API_KEY` | https://aistudio.google.com/apikey | 100 (`gemini`) + 250 (`gemini-flash`) |
| Cerebras | `CEREBRAS_API_KEY` | https://cloud.cerebras.ai/ | 2300 |
| Groq | `GROQ_API_KEY` | https://console.groq.com/keys | 950 |
| SambaNova | `SAMBANOVA_API_KEY` | https://cloud.sambanova.ai/apis | 19 |
| NVIDIA | `NVIDIA_API_KEY` | https://build.nvidia.com/ | 500 |
| OpenRouter (`:free` models only) | `OPENROUTER_API_KEY` | https://openrouter.ai/settings/keys | 48 |
| Cloudflare Workers AI | `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` | https://dash.cloudflare.com/profile/api-tokens | 60 |
| Cohere | `COHERE_API_KEY` | https://dashboard.cohere.com/api-keys | 60 (1000 a month) |
| Mistral | `MISTRAL_API_KEY` | https://console.mistral.ai/api-keys | 500 |
| Ollama (local) | none | https://ollama.com | unlimited |

Free limits change often, and some providers ask for a payment method even for free use (FreePT
parks a provider for 7 days when it answers 402). The budgets, model preferences and order live in
the `PROVIDERS` list at the top of `freept.py`. Edit them to match what your accounts get, and check
each provider's terms before you use it.

## Commands

```
python freept.py status [--offline]      keys, model picked per provider, today's usage
python freept.py usage  [--offline]      what's left per provider and when each budget resets
python freept.py ask [--route=R] "text"  one prompt, answer on stdout (reads stdin if no text)
python freept.py serve [port]            the server (127.0.0.1 only)
```

`--offline` skips the network calls (model lists, OpenRouter's key check).

## Routes

The `model` field of a request (or `--route=` for `ask`) picks how FreePT routes it:

| Route | Meaning |
|---|---|
| `freept` | Best quality first, then down the list |
| `freept/fast` | Fastest providers first |
| `groq`, `cerebras`, ... | Only that provider, with its preferred model |
| `groq/openai/gpt-oss-120b` | That provider and that exact model id |

When every provider is spent, the server answers **HTTP 429** with error type `freept_all_spent`.
Treat that as "stop for now" (or switch to `ollama`), not as something to retry in a loop.

## Using it from code

Any OpenAI-compatible client works. Python example with the official `openai` package:

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:4000/v1", api_key="unused")
r = client.chat.completions.create(model="freept", messages=[{"role": "user", "content": "Hello"}])
print(r.choices[0].message.content)
print(r.model_extra.get("freept"))   # {"provider": "..."}: who answered
```

Or with nothing but the standard library:

```python
import json, urllib.request
body = {"model": "freept/fast", "messages": [{"role": "user", "content": "Hello"}], "max_tokens": 2000}
req = urllib.request.Request("http://127.0.0.1:4000/v1/chat/completions", json.dumps(body).encode(),
                             {"Content-Type": "application/json"})
print(json.load(urllib.request.urlopen(req))["choices"][0]["message"]["content"])
```

Other endpoints: `GET /v1/models` (the routes), `GET /usage` (the meter as JSON), `GET /` (the meter page).

Tips:
- Send `max_tokens` for long answers. Some providers otherwise stop at about 3000 tokens.
- Streaming is not supported. FreePT drops `stream` and returns the whole answer.
- `<think>`/`<thought>` blocks that some models put at the start of the answer are removed.

## Using it for code-search loops

Free models are weaker than frontier models, but they are fast and plentiful. That makes them good
generators in a loop where something else does the checking:

1. Give the model one small, well-defined task (one function, one failing test) with its context.
2. Ask for several different candidates per request, as JSON.
3. Check every candidate mechanically: compile it, run the tests, diff the output.
4. Keep only the candidates that pass. Feed the best score and the ideas already tried back into
   the next round.

Expect a low hit rate per candidate, so the checker must be cheap and automatic. Run large batches
unattended, and check `python freept.py usage` (or `GET /usage`) to see how much quota is left.

## Fair use

- Use one account per provider. Don't create extra accounts to stack free quotas. Most providers
  forbid it, and it is what gets free tiers taken away.
- Free tiers may use your prompts to train their models. Never send secrets, credentials,
  personal data or private code you aren't allowed to share.
- Respect each provider's terms and rate limits. FreePT's budgets sit a little under the published
  free limits, and a 429 from the provider always wins over FreePT's own count.

## Security

- FreePT never writes keys to disk. It reads them from Windows Credential Manager or environment
  variables when it needs them, and sends each key only to its own provider over HTTPS.
- `ledger.sqlite` (next to `freept.py`, or the path in `FREEPT_DB`) stores only timestamps,
  provider and model names, HTTP status, token counts, short error messages, cooldowns and the
  providers' rate-limit headers. It is in `.gitignore`. Error messages can name your account
  or organization, so don't share the ledger.
- The server listens on 127.0.0.1 only and has no authentication. Don't expose it to a network.

## License

MIT. See [LICENSE](LICENSE).
