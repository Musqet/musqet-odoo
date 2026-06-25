# musqet-odoo

Musqet's Odoo integration.

This repository contains **`pos_musqet`**, an Odoo 19 Point of Sale addon that adds a
**Musqet** dual-rail payment terminal (card + Bitcoin Lightning) to the POS payment
screen. A cashier picks *Musqet* as the payment, the customer pays by card or Lightning
on the physical terminal, and the result settles back onto the Odoo order
automatically.

- **Works behind NAT** — settles via polling, no public HTTPS required.
- **API key stays on the backend** — every Musqet API call is proxied server-side; the
  POS browser never sees the key or the API URL.
- An optional signed webhook is available for publicly-reachable deployments.

## Quickstart

1. Put `pos_musqet/`'s parent directory (`musqet-odoo/`) on your Odoo `addons_path` and
   restart Odoo.
2. **Apps → Update Apps List**, then install **POS Musqet**.
3. **Point of Sale → Configuration → Payment Methods**: new method, *Use a Payment
   Terminal* = **Musqet**, fill in **API Key**, **API URL**, **Terminal Serial**, and
   add it to your POS.
4. Open a POS session and ring a test sale on the Musqet method.

⚠️ **Make your POS/company currency match the terminal's currency at Musqet** — the
most common setup mistake.

## Full documentation

See **[pos_musqet/docs/INSTALL.md](pos_musqet/docs/INSTALL.md)** for the complete
install, configuration, webhook, accounting, and merchant-onboarding guide.

## Compatibility

- Odoo **19.0** (Community or Enterprise) with the Point of Sale module.

## License

LGPL-3. See [LICENSE](LICENSE).
