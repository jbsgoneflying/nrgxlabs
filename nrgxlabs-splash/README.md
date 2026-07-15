# NRGX Labs — splash / public homepage

The public face of NRGX Labs at `https://nrgxlabs.com`. A single-page
institutional product site: dark graphite palette, editorial typography,
SVG architecture diagrams, founder + ecosystem sections. Pure static
HTML/CSS with a small inline vanilla JS block — no build step, no
backend, no JS framework.

Founder narrative lives on [joshuabsmith.io](https://joshuabsmith.io).
This page speaks as quantitative research infrastructure built by
Joshua B. Smith — part product site, part quiet founder proof.

## Live page

**`index.html`** is the live splash. It uses `tokens.css` (shared brand
tokens) + `home.css` (page styles) and is kept in sync with
`d-portal.html` (historical filename from the portal exploration; both
files are identical on deploy).

The splash is served directly from this folder on the droplet — nginx
points its `root` directive at `/opt/breach-algo/nrgxlabs-splash/`,
and edits ship on the next push to `main`.

See **[`/deploy/nrgxlabs-migration.md`](../deploy/nrgxlabs-migration.md)**
for the full domain migration runbook.

## Page shape

1. **Nav** — fixed translucent bar: wordmark, section anchors, "Private
   Research Platform" status, Enter the desk
2. **Hero** — headline + verified proof line + animated systems-map SVG
3. **/01 Research problem** — why multiple engines, not one signal
4. **/02 Platform** — eight platform layers
5. **/03 Architecture** — six-tier topology SVG (mobile gets a stacked flow)
6. **/04 Engineering** — measured repo stats + engineering characteristics
7. **/05 Research domains** — three featured domains with technical
   SVG visuals + a compact index of six more
8. **/06 Discipline** — research philosophy
9. **/07 Founder** — Joshua B. Smith + operator record panel
10. **/08 Ecosystem** — RavenOS · InjuryOS · Versefold · joshuabsmith.io
11. **Closing + footer** — editorial close, disclaimer, ecosystem links

## What's in this folder

| File | What it is |
|---|---|
| `index.html` | **Live page** — what `nrgxlabs.com` serves |
| `d-portal.html` | Identical to `index.html` (kept for deploy convention) |
| `home.css` | **Live styles** — institutional homepage |
| `tokens.css` | Brand palette, type, spacing — shared foundation |
| `b-terminal.{html,css}` | Direction B — previous live page (archive) |
| `NRG-Logo.png` | Wordmark lockup (OG image + favicon source) |
| `a-quiet-lab.{html,css}` | Direction A — exploration only |
| `c-memo.{html,css}` | Direction C — exploration only |
| `d-portal.css` | Legacy portal styles (exploration archive) |

## Verified technical claims on the page

All figures are measured from the repository (re-verify before changing):

- **18 analytical engines** — matches engine modules + joshuabsmith.io
- **190+ API routes** — 199 route decorators across backend
- **160,000+ lines of Python/JS** — 162K excluding tests
- **1,400+ automated tests** — 1,470 collected by pytest
- **6 market-data integrations** — ORATS, EODHD, Benzinga, FMP, FRED,
  API Ninjas (vendors intentionally not named on the page)
- **22 routed service modules** — `backend/routers/*.py`

## Editing the page

Copy and styles live in `index.html` + `home.css`:

- **Stats** — hero proof line + `/04` stat row (`data-count` attrs)
- **Diagrams** — inline SVGs in hero and `/03` (mobile stack is HTML)
- **Founder / ecosystem** — sections `/07` and `/08`
- **Disclaimer** — footer `.footDisclaimer`

After editing: bump `?v=` on the `home.css` link in `index.html`, sync
`d-portal.html` (`cp index.html d-portal.html`), `git push origin main`.
nginx serves HTML with `no-store`; CSS is cached 7 days — always
version-bust CSS on style changes.

## Open follow-ups

- [ ] **Square favicon variant** — crop or redraw for 16×16
- [ ] **MX records for `desk@nrgxlabs.com`** — email forwarding in DNS
- [ ] **Dedicated OG image** — 1200×630 card built from the architecture visual
