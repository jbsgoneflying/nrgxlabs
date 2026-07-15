# NRGX Labs — splash / project page

The public face of NRGX Labs at `https://nrgxlabs.com`. **Direction B —
The Terminal**: dark desk aesthetic, live tape header, hero + three
institutional panels (research / method / access). Pure static HTML/CSS —
no build step, no backend, no JS framework.

Founder narrative lives on [joshuabsmith.io](https://joshuabsmith.io).
This page speaks as infrastructure for private capital — not a portfolio
site.

## Live page

**`index.html`** is the live splash. It uses `b-terminal.css` and is kept
in sync with `d-portal.html` (historical filename from the portal
exploration; both files are identical on deploy).

The splash is served directly from this folder on the droplet — nginx
points its `root` directive at `/opt/breach-algo/nrgxlabs-splash/`,
and edits ship on the next push to `main`.

See **[`/deploy/nrgxlabs-migration.md`](../deploy/nrgxlabs-migration.md)**
for the full domain migration runbook.

## Page shape

1. **Top bar** — NRG logo, live regime tape, Enter the desk
2. **Hero** — headline + engine state panel (redacted values)
3. **Three panels** — `/01 research` · `/02 method` · `/03 access`
4. **Footer** — NRGX/Labs · Private Capital Research · app + email

## What's in this folder

| File | What it is |
|---|---|
| `index.html` | **Live page** — what `nrgxlabs.com` serves |
| `d-portal.html` | Identical to `index.html` (kept for deploy convention) |
| `b-terminal.css` | **Live styles** — terminal / desk aesthetic |
| `d-portal.css` | Legacy portal styles (exploration archive) |
| `tokens.css` | Brand palette, type, spacing — shared across directions |
| `NRG-Logo.png` | Wordmark lockup (logo + OG image + favicon source) |
| `a-quiet-lab.{html,css}` | Direction A — exploration only |
| `b-terminal.html` | Direction B prototype (superseded by `index.html`) |
| `c-memo.{html,css}` | Direction C — exploration only |

## Editing the page

Copy and styles live in `index.html` + `b-terminal.css`:

- **Tape / timestamp** — header `.tTape`; ET stamp via `[data-stamp]` JS
- **Engine rows** — hero panel table + `/01 research` list
- **Method / access copy** — panels `/02` and `/03`
- **CTA** — `https://app.nrgxlabs.com` and `desk@nrgxlabs.com`

After editing: bump `?v=` on CSS links in `index.html`, sync
`d-portal.html`, `git push origin main`. nginx serves HTML with
`no-store`; CSS is cached 7 days — always version-bust CSS on style changes.

## Open follow-ups

- [ ] **Square favicon variant** — crop or redraw for 16×16
- [ ] **MX records for `desk@nrgxlabs.com`** — email forwarding in DNS
- [ ] **Live tape values** — wire regime/VIX from `/api/desk-state` when ready
