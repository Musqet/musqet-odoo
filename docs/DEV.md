# Developing & testing `pos_musqet` locally

This repo ships a throwaway **Odoo 19 + Postgres** stack (`docker-compose.yml`) so
anyone can install the addon, run its tests, and exercise the POS flow without
touching their own machine's Python/Postgres setup.

> For installing the addon on a *real* Odoo server (merchant onboarding,
> configuration, webhook, accounting), see
> [`../pos_musqet/docs/INSTALL.md`](../pos_musqet/docs/INSTALL.md). This file is for
> contributors and testers only.

> **Two compose files, two jobs.** This guide uses `docker-compose.yml` — the local,
> throwaway dev stack. To deploy a *permanent* test/demo instance on a VPS, use the
> hardened `docker-compose.coolify.yml` and [`DEPLOY-coolify.md`](DEPLOY-coolify.md)
> instead.

## Prerequisites

- **Docker** with Compose v2 (`docker compose`, not the old `docker-compose`).
- That's it — Odoo, Postgres, and all Python deps come from the `odoo:19` image.

## What the stack does

`docker-compose.yml` runs two containers:

- **`db`** — `postgres:16`, credentials `odoo` / `odoo`.
- **`odoo`** — the official `odoo:19` image, published on
  [http://localhost:8069](http://localhost:8069).

The repo's `pos_musqet/` directory is mounted into the container at
`/mnt/extra-addons/pos_musqet`, which is already on Odoo's `addons_path`, so the
module is discovered automatically. Editing the source on your host is reflected in
the container (restart Odoo to reload Python changes).

## 1. Run the test suite

The fastest way to verify a change. This creates a fresh DB, installs the addon,
and runs **only** the addon's own Python unit tests (`pos_musqet/tests/`, tagged
`post_install`) — the `--test-tags=/pos_musqet` filter keeps Odoo from also running
the thousands of unrelated core-module tests (some of which fail in a minimal
container for reasons that have nothing to do with this addon):

```bash
docker compose run --rm odoo \
  odoo -d test -i pos_musqet --test-enable --test-tags=/pos_musqet --stop-after-init
```

A passing run prints `0 failed, 0 error(s) of N tests` near the end. Re-running is
safe; to start from an empty DB first, see **Teardown** below.

> Drop the `--test-tags=/pos_musqet` filter only if you deliberately want to run the
> whole suite for every installed module.

## 2. Bring up the UI for a live smoke test

```bash
docker compose up -d            # start in the background
docker compose logs -f odoo     # watch startup (Ctrl-C to stop tailing)
```

Then open [http://localhost:8069](http://localhost:8069):

1. Create a database (e.g. `test`). When it asks, **install no demo data** is fine.
2. Install **POS Musqet** from **Apps** (enable developer mode → *Update Apps List*
   if it isn't listed), then configure the Musqet payment method per
   [`INSTALL.md` §3](../pos_musqet/docs/INSTALL.md).
3. Walk the smoke checklist in [`INSTALL.md` §4](../pos_musqet/docs/INSTALL.md).

To install/upgrade the addon from the command line instead of the Apps UI:

```bash
docker compose run --rm odoo odoo -d test -i pos_musqet --stop-after-init   # install
docker compose run --rm odoo odoo -d test -u pos_musqet --stop-after-init   # upgrade
```

> **Heads-up — the live smoke test needs a reachable Musqet terminal API.** Creating
> and settling a real sale requires a test API key, a test terminal serial, and live
> create/poll/cancel endpoints (see the onboarding checklist in `INSTALL.md §7`).
> Without those, the test suite (step 1) plus install/config verification is the
> ceiling of what this stack alone can prove — the addon never reaches a live
> terminal.

## 3. Teardown

```bash
docker compose down        # stop containers, keep the database
docker compose down -v     # stop AND delete the database + filestore (clean slate)
```

## Notes

- **Python changes** need an Odoo restart (`docker compose restart odoo`) and an
  addon upgrade (`-u pos_musqet`) if you touched models/views.
- **POS JavaScript changes** (`pos_musqet/static/src/**`) are served as assets;
  reload the POS with the browser cache disabled, or restart Odoo, to pick them up.
- The stack pins `odoo:19` and `postgres:16`. The addon targets Odoo **19.0** only.
