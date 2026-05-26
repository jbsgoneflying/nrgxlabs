# NRGX Labs — splash page

The public face of NRGX Labs at `https://nrgxlabs.com`. A single-screen
calling card: logo, tagline, live desk ledger, door to the app at
`https://app.nrgxlabs.com`. Pure static HTML/CSS — no build step, no
backend, no JS framework.

## Live page

**`index.html`** is the live splash. It is identical to `d-portal.html`
(the file we explored as "Direction D — The Portal" during design).
The duplicate exists so the page loads cleanly on the bare domain
(`nrgxlabs.com/` resolves to `index.html` by default) while the
`d-portal.html` filename remains for back-reference against the other
exploration directions in this folder.

The splash is served directly from this folder on the droplet — nginx
points its `root` directive at `/opt/breach-algo/nrgxlabs-splash/`,
and edits ship on the next push to `main` (the existing GitHub Actions
deploy workflow runs `git pull` on the droplet, which updates these
files in place).

See **[`/deploy/nrgxlabs-migration.md`](../deploy/nrgxlabs-migration.md)**
for the full domain migration runbook (DNS → nginx → certbot → cutover).

## What's in this folder

| File | What it is |
|---|---|
| `index.html` | **Live splash** — what `nrgxlabs.com` serves |
| `d-portal.html` | Identical to `index.html`; kept for design comparison |
| `d-portal.css` | Styles for the splash (loaded by both `index.html` and `d-portal.html`) |
| `tokens.css` | Brand palette, type, spacing — shared across all directions |
| `NRG-Logo.png` | The wordmark + X-blade lockup (used as logo + OG image + favicon) |
| `robots.txt` | Allows indexing of the splash, blocks the exploration-only files |
| `sitemap.xml` | Single-URL sitemap pointing at the apex |
| `a-quiet-lab.{html,css}` | Direction A — The Quiet Lab (exploration only) |
| `b-terminal.{html,css}`  | Direction B — The Terminal (exploration only) |
| `c-memo.{html,css}`      | Direction C — The Memo (exploration only) |
| `assets/nrgxlabs-logo.png` | Earlier logo file, superseded by `NRG-Logo.png` |
| `assets/nrgxlabs-mark.svg` | Custom SVG attempt, superseded by `NRG-Logo.png` |

The exploration-only directions (`a-quiet-lab.html`, `b-terminal.html`,
`c-memo.html`) are kept as a record of the design alternatives we
considered. They are explicitly disallowed in `robots.txt` so search
engines don't index them, but they're still browsable directly if you
want to compare moods.

## How to open the live splash locally

From the workspace root:

```bash
open nrgxlabs-splash/index.html
```

For a quick visual diff against the other directions:

```bash
open nrgxlabs-splash/a-quiet-lab.html
open nrgxlabs-splash/b-terminal.html
open nrgxlabs-splash/c-memo.html
```

## Editing the splash

Most copy edits live in `index.html`:

- **Tagline** — single line, typed in via JS (see `<span class="pTaglineText" data-typed>`)
- **Desk ledger rows** — `<li class="pLedgerRow">` blocks; engine codes/names/values
- **Door label** — `<span class="pDoorLabel">Enter the desk</span>`
- **Footer email** — `<a class="pFootMail" href="mailto:desk@nrgxlabs.com">`

Style edits go in `d-portal.css` (logo sizing, ledger borders, type
scale). Brand-level colors / fonts / spacing live in `tokens.css` and
propagate everywhere.

After editing: `git push origin main`. The droplet picks up the change
within ~90 seconds via the existing deploy workflow. nginx caches
HTML with `Cache-Control: no-store`, so changes are visible on next
page load — no manual cache purge needed.

## Open follow-ups

- [ ] **Square favicon variant** — current favicon links use the wide
      logo PNG, which scales poorly at 16×16. Crop or redraw a
      square-format mark and replace the `<link rel="icon">` references.
- [ ] **MX records for `desk@nrgxlabs.com`** — set up email forwarding
      in GoDaddy so the footer mailto actually delivers somewhere.
- [ ] **App-side rebrand** — once `app.nrgxlabs.com` is the canonical
      hostname, sweep the static frontend for "Raven Tech" → "NRGX Labs"
      in page titles, headers, and login screen.
