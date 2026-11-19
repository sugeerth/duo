# AgentLens Cloud

Live traces from Claude Code and Codex in the browser, with a free plan and a paid Pro plan.

## For people using it

1. Open the site, enter your email, and copy your install line:
   `curl -fsSL https://YOUR-HOST/install.sh | sh -s -- al_…`
2. Paste it into a terminal, or ask Claude Code or Codex to run it.
3. Work as usual. Each session appears on your dashboard within seconds and updates live.

The line installs `~/.agentlens/agentlens-send.py`, a watcher (Python 3, no packages) that tails the transcripts the agents already write:
- Claude Code: `~/.claude/projects/**.jsonl`
- Codex: `~/.codex/sessions/**.jsonl`

Secrets are removed on your machine before anything is sent: API keys and tokens, private keys, bearer tokens, and password/secret/token values. The installer also adds a Claude Code `SessionStart` hook that restarts the watcher; your `settings.json` is backed up first.

`python3 ~/.agentlens/agentlens-send.py status | stop | uninstall`.

## Plans

| | Free | Pro |
|---|---|---|
| Live tree, alarms, security review, long-run panel | ✓ | ✓ |
| Sessions | 3 most recent, 7 days | all |
| Fleet + catch matrix, run diff, long-horizon reading, patterns, long-term trends | locked | ✓ |

The server enforces the plan:
- Pro routes answer 402 on the free plan.
- Pro-only analysis events are stripped from free snapshots.
- Older sessions answer 402.

The viewer only draws the lock cards.

## Running it

```sh
python3 cloud/server.py --port 8080                       # or: docker build -f cloud/Dockerfile -t agentlens-cloud .
```

| Variable | |
|---|---|
| `AGENTLENS_CLOUD_URL` | public base URL, e.g. `https://lens.example.com` (used in install lines and Stripe return URLs) |
| `AGENTLENS_CLOUD_DATA` | where accounts and traces live (default `./cloud-data`; `/data` in Docker; mount a volume) |
| `STRIPE_SECRET_KEY`, `STRIPE_PRICE_ID` | Stripe Checkout for the Pro subscription (a recurring price) |
| `STRIPE_WEBHOOK_SECRET` | signing secret of a webhook to `/stripe/webhook`. Events `checkout.session.completed`, `customer.subscription.deleted`, `customer.subscription.paused` |
| `AGENTLENS_PRO_PRICE_LABEL` | price shown on the paywall (default `$12 / month`) |
| `AGENTLENS_CLOUD_DEV=1` | local testing only: Upgrade switches to Pro without payment |

**Cheapest: Fly.io**, about $2 a month (the repo's `fly.toml`). It sleeps when idle and wakes on the next visit or upload.

```sh
fly launch --copy-config --no-deploy     # pick a unique app name
fly volumes create agentlens_data --size 1
fly deploy
```

On Render, `render.yaml` uses a paid instance with a disk (about $7 a month). Render's free plan works for a trial: set `plan: free` and remove the `disk` block. It sleeps when idle, and stored data is lost on every restart.

Serve it behind HTTPS (any host that runs a container: Fly.io, Render, Railway, a VPS with Caddy). The login cookie is `Secure` when the public URL is https.

## Limits of this first version

- Sign-up has no email verification.
- The API key is the login. It's in the install line, so treat it like a password.
- Each new batch of lines rebuilds the session, which is fine up to the 60,000-line cap per transcript.
- One process with SQLite per account: right for a first few hundred users, not for a large fleet.
