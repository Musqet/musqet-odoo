# pos_musqet — Install & Onboarding Guide

This guide takes a new merchant or operator from a bare Odoo 19 install to taking a
test dual-rail (card + Bitcoin Lightning) payment through a Musqet terminal, using
only this repository.

> **Compatibility:** Odoo **19.0** (Community or Enterprise), Point of Sale module.
> The addon's technical name is **`pos_musqet`**.

---

## 1. What this addon does

It adds a **"Musqet"** payment terminal option to the POS payment-method screen. A
cashier rings up an order, picks *Musqet* as the payment, and the physical Musqet
terminal prompts the customer to pay by **card** or **Bitcoin Lightning**. When the
terminal reports the sale complete, the result settles back onto the Odoo order
automatically.

- **Card and Lightning share one flow.** Odoo sends `mode:"any"` and the terminal
  decides the rail; the rail used (`card` / `bitcoin`) is recorded on the payment.
- **Polling is the working path.** Odoo polls the Musqet API for the result, so the
  addon works end-to-end behind NAT with **no public HTTPS** required. The webhook
  (section 5) is an optional optimisation for publicly-reachable deployments only.
- **The API key never reaches the browser.** Every call to the Musqet API is proxied
  through the Odoo backend; POS JavaScript never sees the key or the API URL.

---

## 2. Side-load the addon from GitHub

`pos_musqet` is not on the Odoo App Store — install it by dropping the module into
your Odoo addons path.

> **Quick path — `install.sh`.** The repo ships an install script that does steps 1–4
> below for you (fetch from GitHub → place on the addons path → optionally install into
> a database → optionally restart):
>
> ```bash
> curl -fsSL https://raw.githubusercontent.com/Musqet/musqet-odoo/main/install.sh \
>   | bash -s -- --addons-path /opt/odoo/extra-addons --db <your_db> \
>     --restart "sudo systemctl restart odoo"
> ```
>
> It needs `git` **or** `curl`/`wget` (no git required). Run with `--help` for options
> like `--ref <tag>` to pin a release or `--source <dir>` for a local checkout. The
> manual steps below are the same thing done by hand.

1. **Get the module onto the server.** Clone (or download) this repo so that the
   `pos_musqet/` directory sits in a directory that is on your Odoo `addons_path`:

   ```bash
   cd /opt/odoo/extra-addons        # any directory already on addons_path
   git clone https://github.com/Musqet/musqet-odoo.git
   # pos_musqet now lives at /opt/odoo/extra-addons/musqet-odoo/pos_musqet
   ```

   Make sure the **parent** directory (`.../musqet-odoo`) is listed in `addons_path`
   in your `odoo.conf`, e.g.:

   ```ini
   addons_path = /opt/odoo/addons,/opt/odoo/extra-addons/musqet-odoo
   ```

   > Tip: you can also symlink just `pos_musqet/` into an existing addons directory if
   > you prefer not to add a new path.

2. **Restart Odoo** so it picks up the new path:

   ```bash
   sudo systemctl restart odoo        # or however you run it
   ```

3. **Update the apps list.** In Odoo, enable *Developer mode*
   (Settings → scroll down → *Activate the developer mode*), then go to
   **Apps → Update Apps List** and confirm.

4. **Install.** Search Apps for **"POS Musqet"** and click **Install**. The Point of
   Sale module is pulled in automatically as a dependency.

   Command-line equivalent:

   ```bash
   odoo -d <your_db> -i pos_musqet --stop-after-init
   odoo -d <your_db> -i pos_musqet --test-enable --stop-after-init   # also runs the tests
   ```

---

## 3. Configure the "Musqet" payment method

1. Go to **Point of Sale → Configuration → Payment Methods** and create a new method
   (or open an existing one).
2. Set **Use a Payment Terminal** to **Musqet**. Several Musqet fields appear; three
   are **required** (the two webhook fields are optional — see section 5):

   | Field | What to enter | Notes |
   |---|---|---|
   | **Musqet API Key** | The Bearer token Musqet issued for this merchant | Must carry the scopes **`TERMINAL_SALES_WRITE`**, **`TERMINAL_SALES_READ`**, **`DEVICES_READ`** (create/poll sales + resolve the terminal by serial). Stored on the backend only, **never** sent to the browser. Visible to ERP managers only. |
   | **Musqet API URL** | Production base URL from Musqet (default: `https://api.musqet.tech/api/v1`) | Locked to the `musqet.tech` domain. Visible to ERP managers only. |
   | **Musqet Terminal Serial** | The serial number of the physical terminal bound to this method | One serial per payment method / register — see section 8. |

3. **Add the method to your POS.** Go to **Point of Sale → Configuration →
   Point of Sale**, open your shop config, and add the Musqet method under
   **Payment Methods**.

> The **API Key** and **API URL** fields are only visible to users in the
> *ERP Manager* group. A regular cashier can take Musqet payments but cannot see or
> edit these credentials.

### 3.1 Currency match — the most likely footgun ⚠️

**The POS currency must equal the terminal/business currency configured at Musqet.**

The addon sends every sale amount in the **POS currency**. The sale side is **not**
validated server-side — Odoo trusts your POS currency — so if your Odoo company/POS is
in GBP but the Musqet terminal settles in another currency, **sales will be
mischarged** and there is no guard to catch it. Refunds *are* enforced: the backend
**rejects any refund** whose currency does not match the original payment's currency.

Before going live, confirm with Musqet which currency the terminal/business is set to
and make your **POS pricelist / company currency match it**.

---

## 4. Take a test payment (smoke test)

1. Open the POS session for the config that has the Musqet method.
2. Ring up a small test order and choose **Musqet** as the payment.
3. The terminal prompts the customer for **card** or **Lightning**. Complete the
   payment on the device.
4. Odoo polls until the terminal reports **COMPLETE**, then marks the payment done and
   records the transaction. Validate the order and check the receipt:
   - A **card** sale: the terminal prints its own card slip.
   - A **Lightning** sale: the Odoo receipt shows a sats line.
5. **Refund test:** from the order, create a Refund and pay it with Musqet. A
   card-settled original refunds back to the card through the terminal. A
   Lightning-settled original shows a *manual refund* message and makes **no** API
   call (Lightning can't be reversed on the device).

---

## 5. Webhook (optional — public-HTTPS deployments only)

**Skip this section for the pilot / any NAT'd or local deployment.** Polling already
settles every sale; the webhook is purely an optimisation for servers that Musqet can
reach over the public internet.

The webhook lets Musqet *push* the sale result instead of Odoo polling for it. It
requires:

- A **public HTTPS** URL for your Odoo server. Musqet's production API will **not**
  deliver to private/localhost/`.local` targets.
- For local development you'd need a public tunnel (e.g. ngrok/Cloudflare Tunnel) — at
  which point polling is simpler, so most local setups just use polling.

To enable it:

1. In the Musqet payment method, set a **Musqet Webhook Signing Secret** (shared with
   Musqet). The field is ERP-manager-only.
2. Copy the read-only **Musqet Webhook URL** shown on the form. It is
   `<your-odoo-base-url>/musqet/webhook` and is derived from your Odoo *web base URL*
   (System Parameter `web.base.url`) — make sure that is set to your **public** HTTPS
   address, not `localhost`.
3. Register that URL with Musqet (self-registration / request to Musqet support).

Security notes (already enforced by the addon):

- Every inbound webhook is **HMAC-verified** (`x-webhook-signature: t=<ts>,v1=<hmac>`,
  HMAC-SHA256 over `"<ts>.<rawbody>"`) using a constant-time compare; timestamps more
  than **±300s** from now are rejected to blunt replay.
- The route always returns `200` so it can't be used as a signature oracle.
- Webhook and polling are mutually idempotent: whichever lands first settles the sale,
  the other is ignored.
- The webhook route does an unauthenticated indexed lookup *before* HMAC verification.
  For public deployments, front it with reverse-proxy **rate-limiting / an IP
  allow-list** as defence-in-depth.

---

## 6. Accounting & reconciliation (V1)

Keep this simple for V1:

- **Book everything in your POS/store currency to one journal.** Configure the Musqet
  payment method with a single standard payment **journal** (the normal Odoo payment
  method setup). Card and Lightning sales both post to that one journal in your store
  currency. There is **no** separate sats ledger and **no** exchange-rate field in
  Odoo.
- **my.musqet is the source of truth for the Bitcoin-vs-pounds split.** The Musqet
  dashboard tells the merchant how much they took in Bitcoin and how much in fiat. Odoo
  records the rail used per payment (a rough in-Odoo per-rail view), but the
  authoritative reconciliation lives in my.musqet.
- **Settlement is configured on the Musqet side, not in Odoo.** The merchant picks one
  of two options with Musqet: (1) **keep the sats** via an LND node on Voltage, or
  (2) a **Solidi** account that auto-exchanges sats → GBP. Either way Odoo does not
  hold LND/Solidi settings and does not need to.

> **Deferred (later iteration):** per-rail journals and representing Bitcoin as a
> currency in Odoo. Not needed for V1.

---

## 7. Merchant onboarding checklist

Before going live, request the following from Musqet and confirm each item:

- [ ] **Production API base URL** (confirm it's `https://api.musqet.tech/api/v1` or the
      value Musqet gives you).
- [ ] **API key** for the merchant account, with the scopes
      **`TERMINAL_SALES_WRITE`**, **`TERMINAL_SALES_READ`**, and **`DEVICES_READ`**.
- [ ] **Test terminal serial** for a physical/sandbox terminal.
- [ ] **Settlement option chosen** (Voltage LND *keep sats*, or Solidi *auto-exchange
      to GBP*) — and that it's configured on the Musqet side.
- [ ] **Terminal/business currency** at Musqet, and that your **POS/company currency
      matches it** (see §3.1).
- [ ] Sample `metadata` shapes (card + bitcoin) so you know what the terminal returns.
- [ ] **Terminal API is deployed** for your environment (the Musqet-side terminal API
      work, tracked separately) — confirm the create/poll/cancel endpoints are live
      against the base URL you were given.
- [ ] If using the webhook: a public HTTPS URL registered + signing secret exchanged.

Then run the smoke test in section 4 with the test credentials.

---

## 8. Known limitations (V1 pilot)

- **One terminal serial per payment method / register.** The async webhook result
  buffer is a single slot per method, which is correct for one-terminal-per-register.
  Running several physical terminals through one method on a public/multi-register
  webhook deployment is not supported in V1 (polling deployments are unaffected).
- **API key is stored as a normal field** (visibility restricted to ERP managers, the
  same model Odoo uses for payment-provider credentials). Use encrypted database
  backups; an external-secrets store is a possible later hardening step.
- **No automated JavaScript test harness in-repo.** The Python backend has unit tests
  (`pos_musqet/tests/`); the POS frontend logic (polling, in-flight lock, webhook/poll
  race) is verified by the live smoke test in section 4, not by automated JS tests. A
  hoot/QUnit harness is a possible follow-up.

---

## 9. Getting help

- Addon source & issues: <https://github.com/Musqet/musqet-odoo>
- Musqet terminal API / credentials: contact Musqet support.
