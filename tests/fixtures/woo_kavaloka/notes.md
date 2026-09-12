# KÁVALOKA (kavaloka.cz)

- site_id: `woo_kavaloka`  |  country: CZ  |  base: `https://www.kavaloka.cz`
- captured: 3 requests, UA `coffee-aggregator/0.2 (+onboarding)`, >= 1 s between requests to the host
- category page (human): https://www.kavaloka.cz/e-shop/

## robots.txt
- HTTP 200, 390 bytes
- Crawl-delay: none
- wp-json rules: none
- Sitemap: https://www.kavaloka.cz/sitemap_index.xml

## Store API
- `GET /wp-json/wc/store/v1/products/categories?per_page=100` -> HTTP 200, 8 categories
- `/wp-json/wc/store/v1/products?per_page=100&page=1` -> HTTP 200
- `X-WP-Total: 54`, `X-WP-TotalPages: 1`, items in this page: 54
- `Server: openresty`

## Coffee categories
- `19` **jednodruhova-kava** - "Jednodruhová káva" (count=13, parent=0)
- `20` **kavove-smesi-kavaloka** - "Kávové směsi" (count=3, parent=0)
- `21` **kava-bez-kofeinu** - "Káva bez kofeinu" (count=1, parent=0)
- capture is UNFILTERED (`per_page=100&page=1`).

## Attributes seen in store_products_page1.json (54 products, 45 carry attributes)

| attribute name | taxonomy | variation? | example value |
| --- | --- | --- | --- |
| Velikost balení | pa_velikost-baleni | yes | 100 g |
| Hrubost kávy | pa_hrubost | yes | zrnková |
| Stupeň pražení | pa_stupen-prazeni | no | střední |
| Kyselost | pa_kyselost | no | střední |
| Chuťový profil | pa_chut | no | rybíz, černý čaj, hořká čokoláda |
| Vhodná pro | pa_vhodna-pro | no | espresso |
| Složení | pa_slozeni | no | 100% arabika |
| Země původu | pa_zeme-puvodu | no | Rwanda (Lake Kivu) |
| Nadmořská výška | pa_nadmorska-vyska | no | 1700 - 2000 m n. m. |
| Odrůda | _(custom / none)_ | no | Red Bourbon |
| Zpracování | pa_zpracovani | no | washed |
| Specifikace a složení | pa_specifikace-a-slozeni | no | Dárková taška s okénkem šířka-výška-sklad (180 x 190 x 80 mm |
| Druh | pa_druh | no | ořechy v polevě |
| Výživové hodnoty | pa_vyzivove-hodnoty | no | Výživové údaje/ 100 g: energetická hodnota 2278 kj/544 kcal; |
| Ostatní | pa_ostatni | no | Minimální trvanlivost 6-8 měsíců. |
| Předplatné | pa_predplatne | yes | 3 měsíce |

## Prices
- currency: **CZK**, `currency_minor_unit` = 0  (prices are integer strings in minor units; minor_unit 0 means the string IS the whole-unit amount)
- product types: simple x18, variable x36
- 36/54 products have variations; 36 products expose a `prices.price_range` (parent `price` = cheapest variation, real per-size prices only via `/products/<id>` or the variations endpoint)
- prices hidden/zero on parent: no
- SKU present on 27/54 products

## "Label: value" lines in descriptions
- yes - most frequent labels (count of occurrences over the page):
  - `Objem` (1) e.g. "530 ml"
  - `Váha prázdné lahve` (1) e.g. "330 g"
  - `Minerální látky` (1) e.g. "draslík, fosfor, hořčík, měď, vápník, zinek, železo"

## Anything odd
- `currency_minor_unit` is 0 for CZK -> `prices.price` = '390' means 390 Kč, NOT 3.90 Kč. Do not assume 2.
- Coffee is split over three sibling top-level categories (19 jednodruhova-kava, 20 kavove-smesi-kavaloka, 21 kava-bez-kofeinu) and the shop also sells dried fruit/nuts, so the capture is unfiltered (54 products).

## Files
- `notes.md` (3338 bytes)
- `robots.txt` (390 bytes)
- `store_categories.json` (8135 bytes)
- `store_products_page1.json` (352847 bytes)
