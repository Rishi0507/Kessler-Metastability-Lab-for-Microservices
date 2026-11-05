# Deploy Kessler

One server running Docker Compose holds the whole stack: Postgres primary and replica, Redis, three api instances, two gateways, two schedulers, two workers, the nginx balancers and Caddy for HTTPS. CI starts this exact setup and runs a smoke test against it on every pull request.

## What you need

- A Linux server with **4 vCPU and 8 GB RAM** (x86-64). Simulations are CPU-bound, so more cores mean faster sweeps.
- A domain name you control.
- Docker Engine with the Compose plugin, version 2.24 or later.

## 1. Point the domain at the server

Create an `A` record (and `AAAA` if the server has IPv6) for your domain with the server's public address. Caddy needs it to get a certificate.

## 2. Open only the web ports

Allow inbound 22, 80 and 443. Everything else stays closed: Grafana, Prometheus and Jaeger listen on `127.0.0.1` only.

```bash
sudo ufw allow 22,80,443/tcp && sudo ufw enable
```

## 3. Configure

```bash
git clone https://github.com/Rishi0507/Kessler-Metastability-Lab-for-Microservices.git kessler
cd kessler
cp .env.prod.example .env
```

Fill in `.env`:

| Setting | Value |
|---|---|
| `KESSLER_DOMAIN` | your domain, for example `kessler.example.com` |
| `KESSLER_PUBLIC_URL` | `https://` plus the domain |
| `KESSLER_JWT_SECRET`, `KESSLER_STREAM_SECRET` | output of `openssl rand -hex 32`, a different value for each |
| `POSTGRES_PASSWORD`, `REPLICATION_PASSWORD` | output of `openssl rand -hex 24` |

With `KESSLER_ENV=prod`, the services refuse to start if a secret is missing, shorter than 32 bytes, or `KESSLER_COOKIE_SECURE` is not `true`.

Keep `.env` out of git; `.gitignore` already excludes it.

## 4. Start

```bash
docker compose -f docker-compose.yml -f deploy/compose.prod.yml up -d --build
```

The first build compiles the C++ engine and the web app and takes a few minutes.

## 5. Check it

```bash
python3 scripts/smoke.py https://kessler.example.com
```

It signs in as a guest, creates a system and runs a sweep to completion. Every line should start with `ok`.

To see the dashboards, tunnel to the server instead of opening ports:

```bash
ssh -L 3000:127.0.0.1:3000 -L 16686:127.0.0.1:16686 you@server
```

Then open http://localhost:3000 for Grafana and http://localhost:16686 for Jaeger.

## Update

```bash
git pull
docker compose -f docker-compose.yml -f deploy/compose.prod.yml up -d --build
```

The migrate service applies new database migrations before the api starts.

## Back up

```bash
docker compose exec -T postgres pg_dump -U kessler kessler | gzip > kessler-$(date +%F).sql.gz
```

Copy the file off the server. Results can be recomputed, so the database holds the only data you can't regenerate: accounts, systems and their versions.

## Limits for a public demo

These defaults protect a shared server; tune them in `.env`:

| Setting | Default | Effect |
|---|---|---|
| `KESSLER_MAX_ACTIVE_ANALYSES_PER_USER` | 3 | sweeps one person can run at once |
| `KESSLER_MAX_SIMS_PER_ANALYSIS` | 600 | largest sweep |
| `KESSLER_ADMISSION_QUEUE_LIMIT` | 20000 | queued simulations before new sweeps get `503` |

nginx also limits requests per IP address, and the api limits writes per user.
