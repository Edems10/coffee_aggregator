# Ripit Roastery (ripit.sk)

- site_id: `woo_ripit`  |  country: SK  |  base: `https://ripit.sk`
- captured: 3 requests, UA `coffee-aggregator/0.2 (+onboarding)`, >= 1 s between requests to the host
- category page (human): https://ripit.sk/obchod/kavy/

## robots.txt
- HTTP 200, 364 bytes
- Crawl-delay: none
- wp-json rules: none
- Sitemap: https://ripit.sk/sitemap.xml

## Store API
- `GET /wp-json/wc/store/v1/products/categories?per_page=100` -> HTTP 200, 8 categories
- `/wp-json/wc/store/v1/products?per_page=100&page=1&category=16` -> HTTP 200
- `X-WP-Total: 32`, `X-WP-TotalPages: 1`, items in this page: 32
- `Server: openresty`

## Coffee categories
- `16` **kavy** - "Kávy" (count=32, parent=0)  <- used as `&category=`
- `20` **espresso** - "Espresso" (count=26, parent=16)
- `21` **filter** - "Filter" (count=11, parent=16)

## Attributes seen in store_products_page1.json (32 products, 32 carry attributes)

| attribute name | taxonomy | variation? | example value |
| --- | --- | --- | --- |
| Gramáž | pa_gramaz | yes | 250 g |
| Pôvod | _(custom / none)_ | no | Colombia |
| Región | _(custom / none)_ | no | Quindio |
| Odroda | _(custom / none)_ | no | Caturra / Pink Bourbon |
| Nadmorská výška | _(custom / none)_ | no | 1500 – 1750 m.n.m. |
| Spracovanie | _(custom / none)_ | no | Co-fermented |
| Praženie | _(custom / none)_ | no | Light |
| Chuťový profil | _(custom / none)_ | no | červený melón, med, liči |
| Telo | _(custom / none)_ | no | 5 |
| Acidita | _(custom / none)_ | no | 4 |
| Sladkosť | _(custom / none)_ | no | 5 |
| Typ | _(custom / none)_ | no | Single origin |
| Príprava | _(custom / none)_ | no | Filter |
| Zloženie | _(custom / none)_ | no | 100% Arabica |
| Typ mletia | pa_typ-mletia | yes | Bez mletia |

## Prices
- currency: **EUR**, `currency_minor_unit` = 2  (prices are integer strings in minor units)
- product types: variable x32
- 32/32 products have variations; 26 products expose a `prices.price_range` (parent `price` = cheapest variation, real per-size prices only via `/products/<id>` or the variations endpoint)
- prices hidden/zero on parent: no
- SKU present on 0/32 products

## "Label: value" lines in descriptions
- none detected.

## Anything odd
- `pa_gramaz` is the only taxonomy attribute; the 14 origin attributes (Pôvod, Región, Odroda, Nadmorská výška, Spracovanie, Praženie, Chuťový profil ...) are custom per-product attributes (taxonomy null).

## Files
- `notes.md` (2576 bytes)
- `robots.txt` (364 bytes)
- `store_categories.json` (1342 bytes)
- `store_products_page1.json` (284221 bytes)
