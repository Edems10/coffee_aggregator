# COFFIA (coffia.sk)

- site_id: `coffia` | country: SK | base: `https://coffia.sk/`
- captured: 4 requests, UA `coffee-aggregator/0.2 (+onboarding)`, >= 1 s between requests to the host
- storefront collection (human): https://coffia.sk/collections/vsetky-kavy

## robots.txt
- HTTP 200, 3602 bytes — the stock Shopify storefront file
- Crawl-delay: none
- `Allow: /` with `Disallow:` on /admin, /cart/, /checkout, /orders, /account, /services, /sf_*,
  /cart.js, /recommendations/products and the collection filter/sort crawl traps
- `/products.json`, `/collections.json` and `/collections/<handle>/products.json` are **not** disallowed
- Sitemap: https://coffia.sk/sitemap.xml

## JSON endpoints
- `GET /collections.json` -> HTTP 200, 21 collections
- `GET /products.json?limit=250&page=1` -> HTTP 200, 72 products — only **12** of them coffee
- `GET /collections/vsetky-kavy/products.json?limit=250&page=1` -> HTTP 200, 10 products
- no authentication, no key, no currency field anywhere

## Collections
- `vsetky-kavy` — "Čerstvo pražené kávy" (18) — used by the config
- `dark-roast` — "Old School pražené kávy" (25) — used by the config
- `espresso-kava-1` (55), `filter-kava` (55) — the same coffees re-listed per brewing style
- `prislusenstvo` (34), `comandante` (20), `drippery` (11), `darcekove-karty` — not coffee

## Products
- product_type: `KÁVA` for coffee, `príslušenstvo` / `Gift Cards` / `milk pitcher` otherwise
- vendor: `COFFIA` on the coffee, HARIO / COMANDANTE / Fellow / ACAIA on the gear
- variants: `Filter 250g` / `Espresso 1kg` / `Old School Espresso 500g` — the brewing style and
  the pack size share one axis, each with its own `price` and `available`
- `grams`: **always 0**, so every weight comes from the variant title
- `sku`: null on every product
- prices: plain decimal strings in EUR major units (`"14.50"`), no minor-unit exponent

## "Label: value" in body_html
- one fact per line, roughly half with a colon and half without:
  - `Cupping score: 87`, `Spracovanie: NATURAL`, `Odroda: Red Bourbon`,
    `Nadmorská výška: 1600 - 1950 m.n.m.`, `Farma: …`, `Chuťový profil: …`
  - `Výška 1700-1800`, `Odroda Villalobos`, `Spracovanie Anaerobic Washed`, `CUPSCORE 85,5`
- and a heading/value pair: `<p><strong>Chuťový profil</strong></p><p>Jablko, maliny, …</p>`
- `CUPSCORE` is the shop's own spelling and lives in the config's `[label_map]`

## Anything odd
- rich-text editor debris: `​` zero-width spaces and ` ` inside values
  (`"​​ Caturra"`), stripped by the adapter before anything is stored
- `Rwanda Mushonyi` sells out per brewing style: the three Filter variants are unavailable while
  the three Espresso ones are in stock, so the product-level flag is the OR of them
- no rating, no review count, no stock quantity, no currency, no SKU

## Files
- `notes.md`
- `robots.txt` (3602 bytes)
- `collections.json` (whole response, 21 collections)
- `collection_products_page1.json` (trimmed to the 5 products the tests assert on)
