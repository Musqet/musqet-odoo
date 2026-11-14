# pos_musqet — Install & Onboarding Guide

This guide takes a new merchant or operator from a bare Odoo 20 install to taking a
test dual-rail (card + Bitcoin Lightning) payment through a Musqet terminal, using
only this repository.

> **Compatibility:** Odoo **20.0** (Community or Enterprise), Point of Sale module. For
> Odoo 19.0, use the `19.0` branch and its releases.
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
> REF='<release tag>'     # until the first release is tagged: REF=20.0
> curl -fsSL "https://raw.githubusercontent.com/Musqet/musqet-odoo/$REF/install.sh" \
>   | bash -s -- --addons-path /opt/odoo/extra-addons --ref "$REF" --db '<your_db>' \
>     --restart "sudo systemctl restart odoo"
> ```
>
> `--ref` is required. In production use a release from the
> [Releases](https://github.com/Musqet/musqet-odoo/releases) page, and take the script
> from the same ref. A tag can be moved, so for a fixed pin use the release's full
> 40-character commit SHA, copied from that page: the script then refuses anything but
> that commit. Take the SHA only from Musqet's Releases page, since GitHub also serves
> commits from forks under this repo's URLs. The `20.0` branch is unreleased
> development code for Odoo 20 (`19.0` for Odoo 19); until the first release is tagged,
> use the branch for your Odoo version. The script prints
> the commit it installed. It needs `git` **or** `curl`/`wget` (no git required). Run
> with `--help` for options like `--source <dir>` for a local checkout.
> The manual steps below are the same thing done by hand.

1. **Get the module onto the server.** Clone (or download) this repo so that the
   `pos_musqet/` directory sits in a directory that is on your Odoo `addons_path`:

   ```bash
   cd /opt/odoo/extra-addons        # any directory already on addons_path
   # a release tag in production; until the first release is tagged, the branch
   # for your Odoo version (20.0, or 19.0 for Odoo 19)
   git clone --branch '<release tag>' https://github.com/Musqet/musqet-odoo.git
   # optional, for a fixed pin: a tag can be moved, so check out the release's full
   # commit SHA from the Releases page; it prints "pinned" only if HEAD is that commit
   SHA="$(printf %s '<full 40-character SHA>' | tr A-F a-f)"   # git prints lowercase
   git -C musqet-odoo checkout --quiet --detach "$SHA" \
     && test "$(git -C musqet-odoo rev-parse HEAD)" = "$SHA" \
     && echo "pinned to $SHA" \
     || echo "NOT pinned: do not install from this clone; check the SHA and retry" >&2
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

5. **Upgrading an existing install:** close every POS session that uses a Musqet method
   first, so no sale is still in progress, then run `odoo -d <your_db> -u pos_musqet
   --stop-after-init`. The upgrade to 19.0.1.3.0 records each Musqet sale already
   synced; a sale started before it but synced after it has no record, so Odoo can't
   refund it and marks its payment *Not verified* (see §8). The upgrade to 19.0.1.4.0
   adds a column to the Musqet Sales records (*Not Found at Musqet*); run it before the
   new code takes a payment, or every Musqet sale fails until you do. The upgrade to
   19.0.1.5.0 lets a sale or refund record have no Musqet saleId (see *Lost replies* in
   §4); until it runs, Odoo can't record a request whose reply was lost. From
   19.0.1.6.0 a cashier can drive a Musqet terminal only on its company's POS sessions
   (§3), and Odoo refuses a Musqet method's company while another company's POS still
   lists the method: remove it from there first.

   **Moving from Odoo 19 to 20** is Odoo's own database upgrade; upgrade pos_musqet to
   19.0.1.7.0 on Odoo 19 first. Odoo 20 requires every payment method to have a company
   (Odoo's upgrade sets any that are missing) and replaces *Use a Payment Terminal* with
   *Integration* and *Integrate with*: check each Musqet method afterwards shows
   **Terminal** and **Musqet** there (§3). Close every POS session first, and reload each
   till afterwards. Upgrading to 19.0.1.7.0 first is required, not optional: it gives a
   Musqet method the company of the POS configs it is on, and Odoo's own upgrade does
   not look at those. If a database skipped it, check afterwards that no Musqet method is
   listed on another company's POS (its cashiers are refused until it is removed there).

---

### 2.1 Set the secret-encryption key (required)

The Musqet API key and webhook signing secret are stored **encrypted** in Odoo's
database. The encryption key comes from the environment of the Odoo process and is
never stored in the database or in git.

1. **Generate a key once** (on any machine with Python and `cryptography`, such as the
   Odoo host):

   ```bash
   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

2. **Set it on the Odoo host** as the environment variable
   **`MUSQET_FIELD_ENCRYPTION_KEY`** (for example a Coolify environment variable, an
   `Environment=` line in the systemd unit, or the container's environment). Keep a
   copy in your secret manager and never commit it.
3. **Restart Odoo** so the process picks it up.

The same key must be present whenever Odoo reads or writes a Musqet secret, and during
any upgrade of the addon. Every Odoo host running `pos_musqet` against the same
database needs the same key.

**Use a different key in every environment.** Never give a staging or development host
the production key. A production dump restored elsewhere should show empty secrets;
that is intended, and is what keeps a copied database from exposing them.

- **Without the key, or with an invalid one**, saving an API key or webhook secret
  fails with an error naming `MUSQET_FIELD_ENCRYPTION_KEY`. Stored secrets read as
  empty: Musqet calls fail with a message saying the API key cannot be read, and
  webhooks are rejected. Each Odoo worker logs the error once per secret and cause,
  and again if the secret recovers and then fails later.
- **A broken key also blocks editing the payment method**, because the required API
  key field reads as empty. Set a valid key first. Unless you restored the original
  key (for example, after a typo), the stored secrets are unreadable: re-enter both
  secrets under the new key, as described under "Rotating the key" below. Then edit
  the method as usual.
- **If the key is lost, the stored secrets cannot be recovered.** Set a new key, then
  re-enter both secrets as described under "Rotating the key" below.
- **Rotating the key** means setting the new key and then re-entering **both** secrets
  on every Musqet payment method, including a webhook secret you don't use. A secret
  left under the old key stays unreadable and logs an error. Because it already shows
  as empty, saving it empty changes nothing: type a value, save, then clear it and save
  again. There is no automatic re-encryption in this version.

**Upgrading from 19.0.1.0.0.** Version 19.0.1.1.0 encrypts any secrets already stored
in plaintext, checks each one decrypts back, and removes the plaintext columns. Set
`MUSQET_FIELD_ENCRYPTION_KEY` **before** running the upgrade (`-u pos_musqet`). If
secrets are stored and the key is missing or invalid, the upgrade stops and changes
nothing.

**After upgrading, rotate both secrets with Musqet** (issue a new API key and webhook
signing secret, enter them in Odoo, and revoke the old ones). The old plaintext values
are not gone from the database server: they stay in backups taken before the upgrade,
and in Postgres's own data files, write-ahead log and any replicas until those are
overwritten. Rotating is the only step that makes every one of those copies useless.

**Scope of protection.** This protects the secrets if a **read-only** copy of the
database leaks, such as a Postgres dump or a backup. It does **not** protect against:

- **Write access to the database.** Someone who can change rows can make Odoo send the
  decrypted key elsewhere. The addon re-checks the API URL before every call to limit
  this, but treat database write access as access to the secrets.
- **Compromise of the Odoo host.** Odoo must decrypt the secrets to use them, so the
  key is present in the Odoo process on that machine.

Keep the Odoo instance itself off the public internet or behind authentication.
Encryption at rest is defence in depth, not a substitute for securing the host and
the database.

---

## 3. Configure the "Musqet" payment method

1. Go to **Point of Sale → Configuration → Payment Methods** and create a new method
   (or open an existing one).
2. Set **Integration** to **Terminal** and **Integrate with** to **Musqet**. A
   **Musqet** tab appears with the Musqet fields; three are **required** (the two
   webhook fields are optional — see section 5):

   | Field | What to enter | Notes |
   |---|---|---|
   | **Musqet API Key** | The Bearer token Musqet issued for this merchant | Must carry the scopes **`TERMINAL_SALES_WRITE`**, **`TERMINAL_SALES_READ`**, **`DEVICES_READ`** (create/poll sales + resolve the terminal by serial). Stored encrypted on the backend only (see §2.1), **never** sent to the browser. Visible to ERP managers only. |
   | **Musqet API URL** | Production base URL from Musqet (default: `https://api.musqet.tech/api/v1`) | Locked to the `musqet.tech` domain. Visible to ERP managers only. |
   | **Musqet Terminal Serial** | The serial number of the physical terminal bound to this method | One serial per payment method / register — see section 8. |

3. **Add the method to your POS.** Go to **Point of Sale → Configuration →
   Point of Sale**, open your shop config, and add the Musqet method under
   **Payment Methods**.

> The **API Key** and **API URL** fields are only visible to users in the
> *ERP Manager* group. A regular cashier can take Musqet payments but cannot see or
> edit these credentials.

A Musqet method belongs to **one company**: Odoo requires the method's *Company* field,
and records its sales and refunds in that company. A cashier can drive its terminal only
as a user of that company (one of the companies selected in Odoo), and only while a POS
session of that company using the method is not closed. Once every such session is being
closed, a till can still check or cancel a payment in progress, but starts no new sale or
refund. A refund's original payment must be of the same company.

### 3.1 Currency match — the most likely footgun ⚠️

**The company currency must equal the business currency configured at Musqet.**

Every amount must be in the **company currency**. The backend refuses a sale or refund
in any other currency with a currency-mismatch error, and a refund must also match the
original payment's currency. So if a POS is set to a currency other than the company
currency, its Musqet payments are refused until they match.

What the backend **cannot** check is the currency set for your business at Musqet: if
your Odoo company is in GBP but your business at Musqet is set to another currency, **sales
will be mischarged**.

Before going live, confirm with Musqet which currency your business is set to
and make your **company currency match it** (and keep each POS on the company currency).

---

## 4. Take a test payment (smoke test)

1. Open the POS session for the config that has the Musqet method.
2. Ring up a small test order, choose **Musqet** as the payment, check the amount and
   press **Send**.
3. The terminal prompts the customer for **card** or **Lightning**. Complete the
   payment on the device.
4. Odoo polls until the terminal reports **COMPLETE**, then marks the payment done and
   records the transaction. Validate the order and check the receipt:
   - A **card** sale: the terminal prints its own card slip.
   - A **Lightning** sale: the Odoo receipt shows a sats line.

   Odoo keeps its own record of each sale it starts on the terminal, settled only from
   what Musqet reports: the amount captured, the currency and the rail. These are under
   **Point of Sale → Orders → Musqet Sales** (managers only). When the order syncs,
   Odoo checks each Musqet payment line against that record and shows the result in the
   order's **Payments** tab: *Verified* if Musqet reported that sale or refund complete
   for that amount, on the order Odoo sent it for; *Not verified* if not. So a copy of a
   Musqet sale id on another order's line is Not verified, and if Odoo can't tell which
   line is the genuine one (two on the same order, or sales recorded by the upgrade from
   lines that already shared an id), none of them is. Lines of an order not yet paid are
   left blank. To find the Not verified ones, filter **Point of Sale → Orders →
   Payments** by **Musqet: Not Verified**, and check each in the Musqet app. The server
   log also names each one. A Not verified sale line can't be refunded through the
   terminal: if a copy has blocked a genuine sale's refund, delete the copy (or correct
   its Musqet sale id) and try again. Payment lines synced before this check existed
   start blank, and are checked once the line, its order's state or reference, or
   another line with the same Musqet sale changes.

   **Cancel and retry.** When the cashier cancels a payment that is waiting on the
   terminal, Odoo cancels it there and waits for Musqet to confirm before it lets the
   line go. If the customer paid first, the line settles as paid. If Musqet can't
   confirm the cancel, the line keeps waiting and Odoo asks the cashier to cancel on the
   terminal. When Odoo stops waiting by itself (after about five minutes, or after losing
   contact with the server), it cancels the sale on the terminal first in the same way.
   Paying the same order again, for the same amount, on the same terminal, picks up a
   sale still in progress or already paid at the terminal instead of starting another,
   so a retry never charges the customer twice, even after the POS page was reloaded.
   While a sale of the order for another amount, or on another terminal, is still in
   progress, a new one is refused until it ends.

   **Lost replies.** If Musqet's reply to a payment or refund request never reaches Odoo
   (the connection drops or times out, or Musqet or a gateway in front of it answers
   with anything but a plain refusal, such as a server error), the terminal may have
   taken it all the same, and Musqet can't look a request up by order reference. So
   Odoo records it with no Musqet saleId, tells the cashier, and refuses another Musqet
   payment of that order (or another refund in that refund order) until a manager
   clears it. A lost refund also counts against the original sale until then. To
   clear one, find it under **Musqet Sales** or **Musqet Refunds** (filter *Reply lost*)
   and check the Musqet app for a request with its reference:
   - nothing taken or refunded: open it and click **Mark as Failed**;
   - a sale whose payment was taken: refund it in the Musqet app, then mark it failed;
   - a refund that was made: open it and click **Mark as Refunded** (never Mark as
     Failed). It keeps counting against the sale, and the rest of its refund order can
     be refunded.
5. **Refund test:** from the order, create a Refund, choose Musqet and press
   **Refund**. A
   card-settled original refunds back to the card through the terminal. A
   Lightning-settled original shows a *manual refund* message and makes **no** API
   call (Lightning can't be reversed on the device). Odoo refunds only a sale Musqet
   reported complete, and caps the refund at the amount Musqet captured, on the rail
   Musqet reported, whatever the original payment line says.

   Odoo records every refund Musqet accepts, straight away (except in the case
   below). So the refunds of one sale can't add up to more than the sale, even before
   the refund orders sync, and a refund order sent twice by mistake is refused. A refund
   that fails at the terminal stops counting once Musqet reports it as failed. One refund
   order takes one Musqet refund of a given sale: to refund more of that sale later, make
   a new refund order.

   A refund whose reply was lost is recorded with no saleId (see *Lost replies* above).
   The case Odoo can't record: it fails to save the record (the server log then names the
   refund).

   The records are under **Point of Sale → Orders → Musqet Refunds** (managers only). A
   refund stuck as *Pending* that Musqet shows did not go through can be marked
   **Failed** there, so it stops counting against the sale. This can't be undone, so
   check the Musqet app first.

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
  than **±300s** from now are rejected to blunt replay. A header naming `t` or `v1`
  twice, or a timestamp that isn't 1 to 12 plain digits, is rejected. A form-encoded
  body is dropped: Musqet sends JSON.
- The route always answers `200` with the plain-text body `[accepted]`, so it can't be
  used as a signature oracle. Only a body declared over 64 KB is refused (`413`),
  unread.
- The webhook body is never trusted as the result: a verified webhook makes Odoo read
  the sale back from Musqet's API, and both Odoo's record of the sale and the till take
  that reply. A leaked signing secret alone can't change what a refund is capped at or
  make the till accept a payment. Odoo reads only while a read could still settle its
  record of the sale (pending, or done with no rail yet), so a replayed webhook for a
  settled sale costs no call to Musqet; a replay before then (within the ±300s window)
  costs one call each, which the rate-limiting below also bounds.
- Webhook and polling are mutually idempotent: whichever lands first settles the sale,
  the other is ignored. Every till using the payment method is told a result is ready,
  but only the till waiting on that sale takes it. A till whose page was reloaded
  mid-payment doesn't take it either: paying the order again picks the sale up.
- The webhook route does an unauthenticated indexed lookup *before* HMAC verification.
  For public deployments, front it with reverse-proxy **rate-limiting / an IP
  allow-list** as defence-in-depth, and **cap the request body size** there too (64 KB
  is ample; nginx `client_max_body_size`). A body sent chunked, with no
  `Content-Length`, can't be refused up front: Werkzeug 2.3 or later (Odoo's packages
  on Python 3.12) stops reading it at 64 KB, but older Werkzeug reads all of it.

---

## 6. Accounting & reconciliation (V1)

Keep this simple for V1:

- **Book everything in your company currency to one journal.** Configure the Musqet
  payment method with a single standard payment **journal** (the normal Odoo payment
  method setup). Card and Lightning sales both post to that one journal in your company
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
- [ ] **`MUSQET_FIELD_ENCRYPTION_KEY` generated and set** on every Odoo host (one key
      per environment; never reuse production's; see §2.1), with a copy kept in your
      secret manager.
- [ ] **Settlement option chosen** (Voltage LND *keep sats*, or Solidi *auto-exchange
      to GBP*) — and that it's configured on the Musqet side.
- [ ] **Business currency** at Musqet, and that your **company currency matches it**
      (each POS must use the company currency; see §3.1).
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
- **Secrets are encrypted at rest, but the key lives on the Odoo host** (see §2.1).
  This defends against a leaked read-only copy of the database, not against write
  access to the database or compromise of the Odoo host.
  Visibility is also restricted to ERP managers. An external-secrets store is a
  possible later hardening step.
- **The POS still marks an order paid on the browser's word.** Odoo flags a Musqet
  payment line it can't match to a sale or refund Musqet reported complete (*Not
  verified*, see §4), and won't refund it through the terminal, but it does not refuse
  the order: refusing would leave a customer who has paid with an order that can't be
  saved. Check the flagged lines in the Musqet app.
- **Sales made before the upgrade to 19.0.1.3.0 are taken as the POS synced them.** The
  upgrade records each one from its payment line (amount, currency, rail), so it can
  still be refunded. A sale still in progress during the upgrade is not recorded, which
  is why §2 says to close the POS sessions first. Refunds Odoo has no record of (see §4)
  still count by their synced payment line, which can only tighten the cap, never
  release more money.
- **No automated JavaScript test harness in-repo.** The Python backend has unit tests
  (`pos_musqet/tests/`); the POS frontend logic (polling, in-flight lock, webhook/poll
  race) is verified by the live smoke test in section 4, not by automated JS tests. A
  hoot/QUnit harness is a possible follow-up.

---

## 9. Getting help

- Addon source & issues: <https://github.com/Musqet/musqet-odoo>
- Musqet terminal API / credentials: contact Musqet support.
