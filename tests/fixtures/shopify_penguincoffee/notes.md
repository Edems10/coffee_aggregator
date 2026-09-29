# PENGUIN COFFEE (penguincoffee.cz)

- site_id: `penguincoffee` | country: CZ | base: `https://penguincoffee.cz/`
- captured: 4 requests, UA `coffee-aggregator/0.2 (+onboarding)`, >= 1 s between requests to the host
- storefront collection (human): https://penguincoffee.cz/collections/kava

## robots.txt
- HTTP 200, 3632 bytes — the stock Shopify storefront file
- Crawl-delay: none
- `Allow: /` with `Disallow:` on /admin, /cart/, /checkout, /orders, /account, /services, /sf_*,
  /cart.js, /recommendations/products and the collection filter/sort crawl traps
- `/products.json`, `/collections.json` and `/collections/<handle>/products.json` are **not** disallowed
- Sitemap: https://penguincoffee.cz/sitemap.xml

## JSON endpoints
- `GET /collections.json` -> HTTP 200, 11 collections
- `GET /products.json?limit=250&page=1` -> HTTP 200, 45 products (the whole catalogue, gear included)
- `GET /collections/kava/products.json?limit=250&page=1` -> HTTP 200, 24 products
  (`kava` reports products_count 42; the JSON endpoint serves only the published ones)
- no authentication, no key, no currency field anywhere

## Collections
- `kava` — "Káva" (42) — the parent used by the config
- `jednodruhova-kava` (11), `vyberova-kava` (22), `blendy` (5), `dark-edition` (4) — its children
- `prislusenstvi` (15), `priprava-kavy` (25), `balicky` (7), `vanoce-2022` (7) — not coffee

## Products
- product_type: Jednodruhová Káva, Výběrová káva, Blendy, Dark Edition, příslušenství, balíčky, Dárkové karty
- vendor: `penguincoffee` on every product (single-roaster shop)
- variants: always `250g` / `500g` / `1000g`, each with its own `price` and `available`
- `grams`: **filled** (250 / 500 / 1000) on most products; left at 0 on the older ones
  (`trenggiling-indonesia`), which is why the variant title stays the fallback
- `sku`: empty or absent on every product
- prices: plain decimal strings in CZK major units (`"350.00"`), no minor-unit exponent

## "Label: value" in body_html
- the "Dark Edition" line writes the whole passport as **one pipe-joined line**:
  `Region: Sierra Maestra, Kuba | Nadmořská výška: 1000 - 2000 m n. m. | Odrůda: Typica | Zpracování: praná`
- the other lines are marketing prose; `Hmotnost:` appears once, on a gift box

## Anything odd
- the catalogue endpoint is 1/3 brewing gear, so the config walks `collections/kava` instead
- `available` exists per variant only; the product-level flag is the OR of them
- no rating, no review count, no stock quantity, no currency, no SKU

## Files
- `notes.md`
- `robots.txt` (3632 bytes)
- `collections.json` (whole response, 11 collections)
- `collection_products_page1.json` (trimmed to the 5 products the tests assert on)
