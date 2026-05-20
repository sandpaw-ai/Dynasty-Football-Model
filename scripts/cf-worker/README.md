# MFL proxy worker

A tiny Cloudflare Worker that proxies `api.myfantasyleague.com` so the
GitHub Pages site can query MFL leagues directly from the browser. MFL's
CORS policy only allows their own subdomains, so without this in between,
the browser blocks every cross-origin request from `pstiehl.github.io`.

The worker also proxies `api.sleeper.app` purely so we can add edge
caching to the slow `transactions/<week>` endpoint that the manager
rankings feature walks once per page load.

## What it does

```
GET https://dynasty-model-proxy.<subdomain>.workers.dev/health
  -> { status: "ok", time: "..." }

GET .../mfl/2026/export?TYPE=league&L=12345&JSON=1
  -> proxies api.myfantasyleague.com/2026/export?TYPE=league&L=12345&JSON=1
  -> CORS-allowed for pstiehl.github.io
  -> cached at the edge for 5 minutes

GET .../sleeper/v1/league/12345/transactions/1
  -> proxies api.sleeper.app/v1/league/12345/transactions/1
  -> cached at the edge for 5 minutes
```

Only GET is allowed. CORS origin allowlist is in `worker.js`
(`ALLOWED_ORIGINS`). Add localhost or other domains there if you need them.

## Deploy

You need:

1. A Cloudflare account (free tier is enough — 100k requests/day).
2. A Cloudflare API token scoped to **Workers Scripts: Edit** for your
   account. Create one at
   <https://dash.cloudflare.com/profile/api-tokens>.
3. Node 18+ and `npx`.

Then:

```bash
cd scripts/cf-worker
export CLOUDFLARE_API_TOKEN=<paste-your-token-here>
npx wrangler@latest deploy
```

After the first deploy, Wrangler prints the worker URL — something like
`https://dynasty-model-proxy.<your-subdomain>.workers.dev`. Plumb that
into the site build:

### Option A — set in GitHub Actions (recommended)

1. In the repo, go to **Settings → Secrets and variables → Actions →
   Variables tab** (not Secrets — the worker URL isn't sensitive).
2. Click **New repository variable**.
   - Name: `PROXY_URL`
   - Value: `https://dynasty-model-proxy.<your-subdomain>.workers.dev`
     (no trailing slash)
3. Edit `.github/workflows/daily-refresh.yml`. Add an `env:` block to
   the `Run the model end-to-end` step:
   ```yaml
   - name: Run the model end-to-end
     env:
       PROXY_URL: ${{ vars.PROXY_URL }}
     run: |
       python -m dynasty.launcher_headless
   ```
4. Commit + push. Next workflow run bakes the URL into the site.

### Option B — local builds

If you're building the site locally rather than via CI:

```bash
export PROXY_URL=https://dynasty-model-proxy.<your-subdomain>.workers.dev
python -m dynasty.launcher_headless
```

The site build reads `PROXY_URL` and bakes it into `league.html` as a
`data-proxy-url` attribute on the form. When set, the MFL form on the
page activates; when unset, the page tells the user MFL leagues require
the worker (or the `leagues.json` fallback).

## Local testing

```bash
cd scripts/cf-worker
npx wrangler@latest dev    # spins up the worker on http://localhost:8787
```

Then in another terminal:

```bash
curl -H "Origin: http://localhost:8000" http://localhost:8787/health
curl -H "Origin: http://localhost:8000" \
  "http://localhost:8787/mfl/2026/export?TYPE=league&L=12345&JSON=1" | head
```

## Cost

Free tier: 100,000 requests/day. A typical user pulling one MFL league
makes ~20 requests (league, rosters, draftResults, transactions). So one
user-pull-per-second sustained would consume the daily budget. For
Phil's use case (a handful of leagues, occasional clicks), free tier is
way over-provisioned.

## Operational notes

- Worker has no authentication — it's a public proxy. The MFL endpoints
  we forward are public anyway (no cookie required for read-only data
  on public leagues).
- Worker caches by upstream URL, so two users querying the same league
  ID within the 5-minute TTL share one upstream hit.
- Logs visible in the Cloudflare dashboard if you turn on the Logpush
  product (paid). For free, use `wrangler tail` in your terminal to
  stream logs from a running worker.
