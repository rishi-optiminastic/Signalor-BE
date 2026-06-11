# Signalor — Backend (`ranking-be`)

Django 5.2 + DRF API for signalor.ai. SQLite in dev, PostgreSQL in prod.

## Quick start
```bash
python -m venv venv && ./venv/Scripts/activate         # Windows
# source venv/bin/activate                              # macOS/Linux
pip install -r requirements-dev.txt                     # includes ruff + pre-commit
pre-commit install
cp env/example.env env/local.env                        # fill in real values
python manage.py migrate
python manage.py runserver 0.0.0.0:8000
```

## Stack
- **Framework**: Django 5.2 + Django REST Framework
- **Auth**: Custom `User` model (`apps.accounts`, `AbstractBaseUser`)
- **DB**: SQLite local, Postgres in prod (Neon via Render Blueprint)
- **Cache**: Redis (prod) / locmem (dev), `apps/analyzer/_cache.py`
- **Background work**: `threading.Thread` (v1) — no Celery yet
- **AI**: Gemini 2.0 Flash for entity / AI visibility / competitor discovery
- **Search SERP**: DataForSEO
- **Payments**: Dodo Payments SDK (Shopify Billing API webhooks for the Shopify app)
- **Deploy**: Render Blueprint (`render.yaml`)

## Apps
```
apps/
  accounts/       # Custom User, payments, plan limits, referrals
  organizations/  # Organization model (owner_email FK)
  analyzer/       # GEO Analyzer — crawl, score, prompt tracker, AI visibility
  integrations/   # GA4, Shopify, WordPress, WooCommerce
  partners/       # Affiliate / partner program
  referrals/      # Two-sided referral discounts
  visibility/     # Brand visibility roll-ups (Google/Reddit/etc.)
```

## Conventions
- **App registration**: `INSTALLED_APPS = [..., "apps.X.apps.XConfig"]` — never bare `"apps.X"`.
- **Permissions**: most analyzer endpoints are `AllowAny`; auth is gated upstream by cookie + email match.
- **AnalysisRun**: identified by `slug` (public, opaque) — see `runs/s/<slug>/...` routes. Numeric `pk` routes (`runs/<int:run_id>/...`) are internal.
- **Caching**: heavy aggregations use `cached_or_compute(key, ttl, fn)` from `apps/analyzer/_cache.py`. TTL 5–10 min for dashboard data.
- **PromptResult / PromptCitation**: every analysis fires N prompts × K engines × 3 runs and persists each response + parsed citations. The "AI recommendation" dashboard card reads only what's already in these tables (no live AI calls per request).
- **Plan enforcement**: feature flags via `apps.accounts.payments.is_plan_limits_enforcement_enabled()` and `get_plan_limits(email)`. Free tier limits which engines fire.
- **Webhook auth**: Shopify app → backend uses `X-Signalor-Webhook-Secret` header (HMAC compare).

## Layout
```
config/
  settings/       # base.py, development.py, production.py
  urls.py
  wsgi.py
apps/<app>/
  models.py
  views.py
  serializers.py
  urls.py
  admin.py
  migrations/
  pipeline/        # (analyzer only) crawl, score, AI pipeline modules
  services/        # External-call clients
  management/commands/
```

## Tooling
- **Lint + format**: `ruff check .` / `ruff format .` (config in `pyproject.toml`).
- **Pre-commit**: `.pre-commit-config.yaml` runs ruff + ruff-format + whitespace fixers on staged files. Install once with `pre-commit install`.
- **Django checks**: `python manage.py check && python manage.py makemigrations --dry-run` — **always run before pushing**.
- **Tests**: `python manage.py test` (sparse coverage today).

## Branching
- `main` → Render production (api.signalor.ai)
- `staging` → Render staging
- `arkit-01`, `tushar-05` → personal/feature branches

Cross-cutting changes (tooling, env, model changes) flow `staging → arkit-01 → main`.

## Migrations
- Always create a migration for any model change: `python manage.py makemigrations`.
- Resolve migration-merge conflicts with a merge migration (`python manage.py makemigrations --merge`).
- Migration `0044` depends on local migration `0035` — re-pinning when rebasing is normal.

## Common pitfalls
- **Don't bypass `AnalysisRun.slug`** for public endpoints — `pk` is only used by privileged tools.
- **Encryption key** (`ENCRYPTION_KEY`) is required for integrations (Fernet); missing it fails token decryption silently in some paths.
- **Dodo billing currency** requires Adaptive Pricing enabled in the Dodo dashboard — otherwise `billing_currency` is ignored and falls back to USD.
- **Threading + DB connections**: long-running background threads must call `django.db.close_old_connections()` between iterations to avoid stale connection errors.
- **Plan limits filtering**: when aggregating PromptResults for charts, respect `get_plan_limits(email)["engines"]` so free-tier users don't see engines they didn't query.
