# Deploying `odoo.musqet.dev` on Coolify

This is a runbook for standing up a **permanent test/demo** Odoo 19 instance with
`pos_musqet` installed, using [Coolify](https://coolify.io) on a VPS. It uses
[`docker-compose.coolify.yml`](../docker-compose.coolify.yml) (the hardened compose),
**not** the local-dev `docker-compose.yml`.

> **Scope:** this is a demo instance — test products only, no real shop — but it is
> set up to last: TLS, generated secrets, persistent + backed-up volumes, locked-down
> single database. It *will* talk to the **live** Musqet terminal API, so real card /
> Lightning payments can flow through a real terminal during a smoke test.

---

## 0. Prerequisites

- A VPS already running Coolify.
- A DNS **A record** for `odoo.musqet.dev` pointing at the VPS public IP. (Let's
  Encrypt validation needs this resolving before deploy.)
- This repo reachable by Coolify (GitHub source or a deploy key).
- From Musqet, ready to paste later: the production **API key**, the **terminal
  serial**, and the **business currency** the terminal settles in.

---

## 1. Create the Coolify resource

1. **New Resource → Docker Compose**, pointed at this repository.
2. Set the **Compose file path** to `docker-compose.coolify.yml`.
3. Set the **branch** to `main` (or your release branch).

Coolify parses the compose and recognises the magic variables:

- `SERVICE_PASSWORD_POSTGRES` — Coolify **generates** a strong DB password on first
  deploy and injects it into both `db` and `odoo`. You never set or see a plaintext
  password in git.
- `SERVICE_FQDN_ODOO_8069` — tells Coolify to route a domain to the `odoo` service on
  port 8069 and terminate TLS.

## 2. Set the domain

In the `odoo` service settings (or the resource's Domains field), set the FQDN to:

```
https://odoo.musqet.dev
```

Leave **Let's Encrypt / automatic HTTPS enabled** (Coolify's default). Do **not** add
a host port mapping — Odoo must only be reachable through Coolify's proxy, which is
what makes `--proxy-mode` safe.

## 3. Deploy

Hit **Deploy**. Wait until both containers are healthy. The `odoo` healthcheck only
checks that the server is listening, so it can go healthy *before* the database
exists — that's expected; the next step creates it.

## 4. One-time database init

The compose locks the instance to a single database named `odoo` and disables the web
database manager, so create the database from the command line **once**. Open the
`odoo` service's **Terminal / Execute Command** in Coolify and run:

```bash
odoo -d odoo -i pos_musqet --without-demo=True --stop-after-init \
  --db_host="$HOST" --db_user="$USER" --db_password="$PASSWORD"
```

This creates the `odoo` database, installs `pos_musqet` (which pulls in Point of
Sale), and loads **no demo data**. When it exits, the running service will serve that
database. (The `$HOST/$USER/$PASSWORD` env vars are already set inside the container.)

## 5. First login & lockdown

1. Browse to **https://odoo.musqet.dev**. The login page appears (the database is
   auto-selected by the `--db-filter`).
2. Log in as `admin` / `admin` and **immediately change the admin password** to a
   strong one (top-right → *Preferences*, or Settings → Users).
3. **Set the base URL.** Settings → Technical → System Parameters:
   - `web.base.url` = `https://odoo.musqet.dev`
   - add `web.base.url.freeze` = `True` so Odoo stops rewriting it from request
     headers (important for correct webhook URLs and emailed links).

## 6. Configure the Musqet payment method

Follow [`pos_musqet/docs/INSTALL.md` §3](../pos_musqet/docs/INSTALL.md):

- **Point of Sale → Configuration → Payment Methods**, new method, *Use a Payment
  Terminal* = **Musqet**.
- **Musqet API Key** = the production key from Musqet.
- **Musqet API URL** = `https://api.musqet.tech/api/v1` (the prod base URL; it must be
  under `musqet.tech` or the addon's SSRF guard rejects it).
- **Musqet Terminal Serial** = the serial of the physical terminal.
- **Currency match (the footgun):** set the company / POS currency to the terminal's
  business currency at Musqet before taking any sale — sales are not currency-validated
  server-side (see `INSTALL.md §3.1`).
- Add the method to your POS config and create a couple of **test products**.

## 7. Smoke test

Open a POS session and walk [`INSTALL.md` §4](../pos_musqet/docs/INSTALL.md): a card
sale settling via polling, a Lightning sale (rail + sats receipt line), and a refund.
This is the live-verification pass that closes epic #1.

## 8. Webhook (now available)

Unlike a NAT'd dev box, this instance **has public HTTPS**, so the optional webhook is
viable (see [`INSTALL.md` §5](../pos_musqet/docs/INSTALL.md)). If you enable it:

- Set the **Musqet Webhook Signing Secret** and register
  `https://odoo.musqet.dev/musqet/webhook` with Musqet.
- The webhook route does an unauthenticated indexed lookup *before* HMAC verification.
  Add a **rate-limit / IP allow-list** at Coolify's Traefik proxy as defence-in-depth.

Polling already settles every sale, so the webhook is purely an optimisation — skip it
if you just want the smoke test working.

## 9. Operating it

- **Backups.** Even for a demo, enable Coolify's **scheduled Postgres backups** on the
  `db` service so the instance is genuinely durable. The `odoo-data` volume holds the
  filestore (attachments/receipts) and persists across redeploys.
- **Updating the addon.** A Coolify redeploy pulls the latest repo and recreates the
  containers; the volumes (database + filestore) persist. After a code change, apply
  the module upgrade once via the `odoo` Terminal:

  ```bash
  odoo -d odoo -u pos_musqet --stop-after-init \
    --db_host="$HOST" --db_user="$USER" --db_password="$PASSWORD"
  ```

- **Scaling.** This runs threaded (`--workers=0`) so HTTP and websocket share port
  8069 and Coolify only routes one port — fine for a demo. For real concurrency, set
  `--workers=2+` in the compose and route `/websocket` to port 8072 at the proxy.

## Security recap

- DB password generated by Coolify; never in git.
- DB port not published to the host; Postgres is reachable only on the internal
  compose network.
- Web database manager disabled (`--no-database-list`) and the instance pinned to one
  database (`--db-filter`).
- TLS terminated by Coolify; `--proxy-mode` trusts forwarded headers **only** because
  nothing but the proxy can reach Odoo.
- The Musqet API key lives on the Odoo backend (ERP-manager-visible field), never in
  the browser — unchanged from the addon's design.
