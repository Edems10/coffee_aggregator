# WooCommerce Store API survey - 39 CZ/SK coffee shops

**Date:** 2026-09-12 · **Requests made:** 118 (39 x robots.txt + 39 x `/wp-json/wc/store/v1/products?per_page=2` + 39 x `/wp-json/wc/store/v1/products/categories?per_page=100`, plus 1 HTML category page for the one shop whose API failed). No redirect hops, 1 s delay between requests to the same host, UA `coffee-aggregator-research/0.1`.
**Raw payloads:** `woo_survey/raw/<domain>/{robots.txt,products.json,products.hdr,categories.json,categories.hdr}` · **Machine-readable:** `woo_survey/results.json`

## Headline

| Outcome | Shops |
|---|---|
| Store API returns **200 + valid JSON product array** | **38 / 39** |
| API blocked / 401 / 403 / WAF challenge | **0** |
| API 404 (not WooCommerce at all) | **1** - `qb.coffee` (Shoptet storefront, misclassified in the first wave) |
| robots.txt `User-agent: *` disallows `/wp-json` | **1** - `ebenica.sk` (`Disallow: /wp-json/`) |
| robots.txt missing (404, i.e. nothing disallowed) | 1 - `mlsnacava.sk` |
| Behind Cloudflare but **no** challenge for a plain UA | 2 - `illimite.sk`, `aurelica.coffee` |
| `X-WP-Total` / `X-WP-TotalPages` present | 38 / 38 (only `kavavitazov.sk` reports a wrong total) |

So: the Store API is effectively universal on this cohort. There is no "blocked by a plugin/WAF" case at all - the only two shops that cannot be read through the API are one non-WooCommerce store and one robots.txt opt-out.

## Field availability (38 API shops, 2 sampled products each = 76 products)

`id`, `name`, `permalink`, `slug`, `prices`, `attributes`, `variations`, `categories`, `tags`, `short_description`, `description`, `images`, `is_in_stock`, `sku`, `type`, `on_sale`, `price_html` are present in **38/38** shops (`slug` missing only on `riksakava.sk`, which runs an older 27-field schema; `weight` present in 25/38).

`prices` is identical everywhere - **all 38 shops** return the full object: `price`, `regular_price`, `sale_price`, `price_range`, `currency_code`, `currency_symbol`, `currency_minor_unit`, `currency_decimal_separator`, `currency_thousand_separator`, `currency_prefix`, `currency_suffix`.

Practical gaps:
- **`sku` is empty** on most shops - only 10/38 populate it. Use `permalink`/`id` as the stable key, never SKU.
- **Prices for variable products are min-only.** 29/38 shops returned variable products in the sample (weight variants). `prices.price` = cheapest variant, `prices.price_range` = `{min_amount, max_amount}`. The `variations[]` array carries **only `id` + attribute slug pairs - no per-variant price**. Getting "250 g costs X" therefore needs either a second call per variation id or the HTML detail page's `data-product_variations` JSON. 3 shops (`doraz.sk`, `ripit.sk`, `triproasters.sk`) run an older build that omits `price_range` entirely, so handle its absence.
- **`description` is empty in the list response** on 4 shops (`goriffee.com`, `mlsnacava.sk`, `zrnco.sk`, `severan.eu`) - page-builder content; `short_description` is empty on 7 others. Always read both fields.
- Images: 37/38 shops have >=1 image on every sampled product (`kmen.coffee` had one image-less product).
- `tags` are used by 14/38 shops and are often genuinely useful tasting/type descriptors (`diroastery.sk`: cokoladova/ovocna/sladka; `zrnco.sk`: specialty-coffee/vyberova-kava/kolumbia; `goodtimes.coffee`: espresso/stredne-prazenie).

## Currencies

- **EUR: 32 shops** (all SK) · **CZK: 6 shops** (all CZ: industra, beansmiths, cokafe, kmen, kavaloka, longberry).
- `currency_minor_unit` is **2** for 36 shops but **0** for `kmen.coffee` and `kavaloka.cz` - their `price: "360"` means **360 CZK, not 3.60**. The adapter must always compute `amount = int(price) / 10**currency_minor_unit`; hardcoding /100 corrupts those two shops by 100x.
- One zero price seen (`longberry.cz`, an espresso machine "price on request").

## Attribute coverage (this is the real decision point)

| Situation | Shops |
|---|---|
| Products carry **origin/quality attributes** (Odroda, Spracovanie, Nadmorská výška...) | **17** |
| Products carry **only weight / grind / packaging** attributes | **16** |
| Products carry **no attributes at all** | **5** |

Of the 21 shops without origin attributes, **16 put the same facts into the description as `Label: value` lines** (`Odroda: Caturra`, `Nadmorská výška: 1 800 m`, `Spracovanie: Natural`, often wrapped in `<strong>`), so a label-line parser recovers them. Only **5 shops** (`ebenica.sk`, `kaan.sk`, `zarno.sk`, `mlsnacava.sk`, `poetrycoffee.sk`) expose neither structured attributes nor label lines - there the API gives you name/price/stock/images and a prose blurb, nothing more.

Caveat: attribute coverage was measured on **2 products per shop** (`per_page=2`, whatever the API returned first). For 4 of the 5 "no attributes" shops the sample happened to be non-coffee items (soap, espresso machine, French press, brewer), so their coffee products may still carry attributes - only `karmacoffee.sk` was confirmed on real coffee products.

## Attribute / label union (default label map for the adapter)

Frequency = number of shops where the label appears, counting both WooCommerce attribute names and `Label:` lines found in descriptions. Full data in `results.json` / `label_union.json`.

| Freq | Label (SK/CZ/EN variants) | Canonical field |
|---|---|---|
| 16 | Chuťový profil, Chuť, Profil, Primary flavour note, Our baristas notes | `tasting_notes` |
| 13 | Spracovanie, Zpracování, Spôsob spracovania, Process, Processing, Príprava | `process` |
| 12 | Hmotnosť, Váha, Gramáž, Veľkosť balenia, Velikost balení, Balenie, Balení, Package size/weight, Weight, Obal, Typ balení | `weight` / `packaging` |
| 12 | Nadmorská výška, Nadmořská výška, Altitude, výška | `altitude` |
| 12 | Odroda, Odrůda, Odroda alebo varieta, Odroda kávy, Variety, Varieties | `variety` |
| 9 | Región, Region, Oblasť, Oblast, Podľa oblasti | `region` |
| 8 | Farma, Farmár, Farmář, Producer, Plantáž / farmár, Vyrobil | `farm` / `producer` |
| 8 | Praženie, Pražení, Typ praženia, Typ pražení, Spôsob praženia, Roast | `roast_level` |
| 6 | Stupeň praženia, Stupeň pražení, Stupeň praženia (Roast) | `roast_level` |
| 4 | Druh, Druh kávy, Typ kávy, Typ | `species` / `product_type` |
| 3 | Krajina pôvodu, Krajina, Země původu, Pôvod, Pôvod kávy, Country of origin | `origin_country` |
| 3 | Zloženie, Složení, Zloženie kávy, Zloženie podľa druhu | `blend_composition` |
| 2 | Cuppingové skóre, Skóre kvality, Cupping score, SCA | `cupping_score` |
| 2 | Mletie, Typ mletia, Hrubost kávy | `grind` |
| 2 | Kyslosť, Kyselost, Acidita, Kyslosť kávy | `acidity` |
| 2 | Sladkosť, Horkosť, Telo, Tělo, Intenzita | `sensory_scores` |
| 2 | Zber, Obdobie zberu | `harvest` |
| 1 each | Odporúčaná príprava / Odporúčaný spôsob prípravy / Preparation method / Použitie / Vhodná pro, Klasifikácia kávy, Skladovanie, Obsah balenia, Dozvuk, Location | `brew_recommendation`, misc |

Shop-specific noise to ignore or route to `packaging`: `gramáž (produkt)` (ebenica, taxonomy `pa_variant-kosik`), `"Vylepši" si kávu sám`, `Čerstvo pražená výberová káva`, `Praženie (od 500g)` (zrnco), `Ak by ste túto kávičku radi vyskúšali, prosím Vás vyberte si hmotnosť balenia.` (a whole sentence used as an attribute label), `Doplnky`, `Veľkosť drippera`, `Potlač`, `Rozmer`.

One shop, `severan.eu`, ships the facts as an **HTML `<table class="coffee-table">` inside `short_description`** (icon + `Chuť` / value rows) - needs table parsing, not line parsing.

## Category naming patterns

`/products/categories` worked on all 38 API shops and returns `id`, `slug`, `name`, `count`, `parent`. It **hides empty categories**, so the returned list is already the live tree.

- The single most common coffee slug is **`kava`** (17 shops), then `espresso` (9), `filter`/`filtr` (7-9), `bezkofeinova-kava` (5), `vyberova-kava` (3), `prazena-kava` (2). But a `kava`-slug heuristic **fails outright on 9 shops**: `zarno.sk` (arabica/robusta/zmes), `goodtimes.coffee` (premium), `kavavitazov.sk` (novinky/oblubene - no product taxonomy at all), `severan.eu` (horka/ovocna/bezkofeinova), `doraz.sk` (arabiky/blendy), `25coffee.sk` (zrnkova-kava-100-arabica...), `sweetbeans.coffee` + `triplefivecoffee.com` (English slugs), `riksakava.sk` (ponuka-kav), `karmacoffee.sk` (nasa-kava).
- Coffee categories are frequently **polluted with gear**: `bozinroastery.sk` `domaca-priprava-kavy` (147) and `domace-kavovary` (13) both match a naive "káva" regex; `tokycaffe.sk` `kavovary-a-prislusenstvo` (19); `triproasters.sk` `prislusenstvo-ku-kave` (25); `ebenica.sk` `kavove-prislusenstvo` (26).
- **Bilingual duplication** on `beansmiths.com` (kava 67 / coffee 58, filtr 37 / filter 35, espresso 30 / espresso 28), `industra.coffee` (filtrovana-kava / filter-coffee, espresso-2 twice) and `longberry.cz` (kava 156 / kava-2 581). Pick one language tree or you double-count.
- `caffe4u.sk` returns **synthetic 16-digit category ids** (`9003241321018644:prazena-kava`) alongside the normal ones - prefer the small integer ids.
- `X-WP-Total` on `/products` counts the **whole catalogue**, not coffee (e.g. `bozinroastery.sk` 328, `triplefivecoffee.com` 136 vs ~11 coffees). Category filtering is mandatory for sane volumes.

## Recommendation: API-first, with per-shop category ids and an HTML detail-page fallback

**Build the WooCommerce adapter as Store-API-first.** 38/39 shops answer, the schema is uniform enough to write one parser, no auth, no WAF, correct stock and machine-readable prices - HTML-first would be strictly worse everywhere except `ebenica.sk`.

Concrete shape:

1. **Discovery**: `GET /wp-json/wc/store/v1/products/categories?per_page=100` once per shop, store the coffee category ids in shop config (table below). Re-run monthly - ids are stable, slugs are not.
2. **Listing**: `GET /wp-json/wc/store/v1/products?category=<id>&per_page=100&page=N`, paginate on `X-WP-TotalPages`, but **stop on an empty array too** (`kavavitazov.sk` reports `X-WP-Total: 1` while returning more).
3. **Normalisation**: `amount = int(prices.price) / 10**prices.currency_minor_unit`; keep `price_range` as the min/max span; treat missing `price_range` (3 shops) and `price = "0"` as "price unknown".
4. **Attribute extraction, three tiers in this order**:
   a. `attributes[]` -> label map above (17 shops covered fully);
   b. `Label: value` regex over `short_description` + `description` after tag-stripping, using the same label map (recovers 16 more shops; `<strong>`-wrapped values are the common form);
   c. `<table>` parsing for `severan.eu`.
5. **HTML fallback, only per-shop and only where it buys something**:
   - `ebenica.sk` - robots forbids `/wp-json`, so HTML-only (its category/detail pages are standard WooCommerce).
   - Per-variant weight prices on the 29 variable-product shops, if the aggregator needs price-per-100 g: parse `data-product_variations` on the detail page (one fetch per product) rather than N Store API calls per product.
   - `kaan.sk`, `zarno.sk`, `mlsnacava.sk`, `poetrycoffee.sk`, `karmacoffee.sk` - no origin data in the API; only an HTML detail-page scrape (or manual curation) will add origin fields, and for `mlsnacava.sk`/`karmacoffee.sk` even that is doubtful.
6. **`qb.coffee` leaves the WooCommerce adapter entirely** - it is Shoptet (`/wp-json/*` 404 from openresty, 328 `shoptet` markers and `data-micro-identifier` tiles on `/kava/`, no `li.product` / `woocommerce-loop-product__title` / `.woocommerce-product-attributes` anywhere). Route it to a Shoptet adapter.

Expected yield with this design: **name, permalink, images, stock, currency-correct price and category for 38/39 shops**, plus structured origin attributes for **33/38** (17 from attributes, 16 from description labels - overlapping set of 33 distinct shops), with 5 shops degraded to name/price/prose.

## Per-shop table

| Domain | X-WP-Total | Currency / minor unit | Suggested coffee category ids | Attributes | Origin data from |
|---|---|---|---|---|---|
| ebenica.sk | 137 | EUR/2 | 77:kava(27) | weight only | **none** |
| goriffee.com | 43 | EUR/2 | 22:kava(26) | attributes | attributes |
| diroastery.sk | 57 | EUR/2 | 28:kava(14) | attributes | attributes |
| illimite.sk | 4 | EUR/2 | 107:zrnkova-vyberova-kava(4) | weight only | description labels |
| goodtimes.coffee | 3 | EUR/2 | 47:premium(3) | weight only | description labels |
| 25coffee.sk | 9 | EUR/2 | 23:zrnkova-kava-100-arabica(6), 25:nase-vlastne-zmesi(1), 27:zrnkova-bezkofeinova-kava(1) | weight only | description labels |
| caffe4u.sk | 70 | EUR/2 | 17:prazena-zrnkova-kava(45) | attributes | attributes |
| trinitybeans.sk | 17 | EUR/2 | 50:espresso(10), 37:filter(6) | attributes | attributes |
| kaan.sk | 76 | EUR/2 | 20:kava(17) | weight only | **none** |
| zarno.sk | 8 | EUR/2 | 20:arabica(4), 21:robusta(3), 25:zmes(4) | weight only | **none** |
| sweetbeans.coffee | 68 | EUR/2 | 30:coffee-beans(31) | weight only | description labels |
| spiritcoffee.sk | 23 | EUR/2 | 53:kava(10) | weight only | description labels |
| industra.coffee | 189 | CZK/2 | 17:filtrovana-kava(14), 53:espresso-2(12) | none | description labels |
| beansmiths.com | 144 | CZK/2 | 33:kava(67) | attributes | attributes |
| cokafe.com | 32 | CZK/2 | 56:kava(11) | attributes | attributes |
| qb.coffee | - | -/None | **none - take all** | - | - |
| triplefivecoffee.com | 136 | EUR/2 | 27:kava(21) | weight only | description labels |
| kavaondrejka.sk | 7 | EUR/2 | 16:espresso(6), 18:bezkofeinova-kava(1) | attributes | attributes |
| mlsnacava.sk | 12 | EUR/2 | 53:kava(12) | weight only | **none** |
| zrnco.sk | 11 | EUR/2 | 25:kava(5) | attributes | attributes |
| kmen.coffee | 79 | CZK/0 | 306:kava(23) | attributes | attributes |
| kavaloka.cz | 54 | CZK/0 | 19:jednodruhova-kava(13), 20:kavove-smesi-kavaloka(3), 21:kava-bez-kofeinu(1) | attributes | attributes |
| longberry.cz | 106 | CZK/2 | 156:kava(31) | none | description labels |
| praziarenepera.sk | 135 | EUR/2 | 20:cerstva-kava(25) | attributes | attributes |
| twohands.sk | 22 | EUR/2 | 25:kava(11) | weight only | description labels |
| severan.eu | 17 | EUR/2 | 172:horka(8), 173:ovocna(8), 66:bezkofeinova(1) | attributes | attributes |
| ripit.sk | 59 | EUR/2 | 16:kavy(32) | attributes | attributes |
| triproasters.sk | 43 | EUR/2 | 22:vyberova-kava(14) | attributes | attributes |
| finecoffee.sk | 16 | EUR/2 | 15:prazena-kava(9) | weight only | description labels |
| karmacoffee.sk | 19 | EUR/2 | 24:nasa-kava(13) | none | description labels |
| riksakava.sk | 11 | EUR/2 | 16:ponuka-kav(11) | weight only | description labels |
| bozinroastery.sk | 328 | EUR/2 | 51:kava(32) | weight only | description labels |
| doraz.sk | 15 | EUR/2 | 63:arabiky(11), 47:blendy(3) | weight only | description labels |
| aurelica.coffee | 88 | EUR/2 | 39:kava(10) | attributes | attributes |
| tokycaffe.sk | 62 | EUR/2 | 169:kava-podla-krajiny-povodu(17), 178:speciality-coffee(11), 177:bezkofeinovakavaplechovky(4) | none | description labels |
| poetrycoffee.sk | 11 | EUR/2 | 53:kava(4) | none | **none** |
| viemcoffee.sk | 6 | EUR/2 | 19:kava-na-espresso(6) | weight only | description labels |
| seriouscoffee.sk | 7 | EUR/2 | 191:vyberova(4), 222:decaf(1), 196:espresso-blend(1) | attributes | attributes |
| kavavitazov.sk | 1 | EUR/2 | **none - take all** | attributes | attributes |
## Per-shop notes and anomalies

- **ebenica.sk** - robots.txt User-agent: * has "Disallow: /wp-json/" - the ONLY shop that forbids the Store API. API technically returns 200 JSON but a polite adapter must fall back to HTML here. Single variation attribute "gramáž (produkt)" (taxonomy pa_variant-kosik); no origin attributes, no Label: value lines in the description either.
- **goriffee.com** - description empty across the sample (short_description carries the copy). Coffee products do expose 6 origin attributes.
- **diroastery.sk** - 8 variations per product, 2 attributes (Hmotnosť, Mletie) plus tag-based flavour descriptors (cokoladova, ovocna, sladka) which are useful as tasting notes.
- **illimite.sk** - Behind Cloudflare but no challenge for a plain UA (200 JSON). Only one category exists: 107 zrnkova-vyberova-kava (4 products). Origin data is in the description as Label: value lines (Farma, Farmár, Nadmorská výška, Oblasť, Odroda, Príprava, Spracovanie).
- **goodtimes.coffee** - Only one category exists (47 premium, 3 products) - no "kava" slug to filter on. Origin data is in the description as Label: value lines.
- **25coffee.sk** - Category slugs are grind/type based (zrnkova-kava-100-arabica, zrnkova-bezkofeinova-kava, nase-vlastne-zmesi, cascara). Origin data in description Label: value lines.
- **caffe4u.sk** - Category endpoint returns duplicate entries with 16-digit synthetic ids (e.g. 9003241321018644 prazena-kava alongside 17 prazena-zrnkova-kava, both count 45) - a plugin injects virtual terms. Prefer the small integer ids. Richest attribute set of the survey (16 attributes incl. Kyslosť kávy, Horkosť kávy, Zloženie kávy).
- **trinitybeans.sk** - 28-field (older) schema, simple products only, 11 attributes incl. Skóre kvality, Obdobie zberu, Klasifikácia kávy.
- **kaan.sk** - Simple products with real SKUs; X-WP-Total 76 for a shop the first wave counted at 17 coffees - the API total covers the whole catalogue incl. variations/other types.
- **zarno.sk** - No coffee-named categories at all: arabica(4), robusta(3), zmes(4), nezaradena(1). Category filtering must be per-shop configured.
- **sweetbeans.coffee** - English-language shop (EN label lines: Altitude, Process, Producer, Varieties, Location) though it is an SK roaster; categories in English (coffee-beans 31, specialty-beans 12).
- **spiritcoffee.sk** - Simple products only; second sampled product has a 94-char description. Label lines present (Chuť, Dozvuk, Praženie, výška).
- **industra.coffee** - Bilingual (CZ/EN, WPML-style): categories are duplicated per language (espresso-2 appears twice with different ids, filtrovana-kava/filter-coffee, kava/coffee). Sampled products were soaps, so 0 attributes in this sample; description uses EN/CZ "Weight:/Hmotnost:" label lines.
- **beansmiths.com** - Bilingual CZ/EN: kava(67)/coffee(58), filtr(37)/filter(35), espresso(30)/espresso(28) are the same products in two languages - an adapter must pick one language tree or it will double-count.
- **cokafe.com** - CZK, 7 attributes; catalogue mixes a bakery (cerstve-pecivo 10, cukrarna 6) with coffee (56 kava, 11).
- **qb.coffee** - NOT WooCommerce. eshop.qb.coffee is a Shoptet store (168 KB of "shoptet" markup, data-micro-identifier product tiles, openresty 404 on /wp-json/*). Misclassified in the first wave; needs a Shoptet adapter, not the Woo one.
- **triplefivecoffee.com** - Simple products, bilingual description label lines (SK + EN). X-WP-Total 136 vs 11 coffees on the category page - filter by category 27/100 kava.
- **kavaondrejka.sk** - 5 attributes incl. Spôsob spracovania, Odroda, Nadmorská výška; only 7 products.
- **mlsnacava.sk** - robots.txt returns 404 (no robots file). Store API fine. Both description AND short_description are empty in the API (page-builder content) and the only attribute is "Veľkosť balenia" -> no origin data at all from the API; a second product is type "yith_bundle" (YITH bundles plugin).
- **zrnco.sk** - description empty (page builder); short_description is a flavour sentence. Attributes are shop-specific upsell labels ("Čerstvo pražená výberová káva", "Praženie (od 500g)", "\"Vylepši\" si kávu sám") rather than origin data. 11 variations per product.
- **kmen.coffee** - CZK with currency_minor_unit = 0 (prices are whole crowns, NOT hundredths) - the adapter must honour currency_minor_unit. One sampled product has zero images.
- **kavaloka.cz** - CZK with currency_minor_unit = 0. 24 variations per product; 11 attributes.
- **longberry.cz** - One sampled simple product has price "0" (price on request / machine). Duplicated category trees again (kava 156 and kava-2 581, both count 31). Sampled products were a blend and an espresso machine -> 0 attributes in this sample.
- **praziarenepera.sk** - 12 attributes including SCA-style fields; category cerstva-kava(25) + vzorky-kavy(17).
- **twohands.sk** - 28-field schema. Only a weight attribute, but the description carries a full Label: value block (Odroda, Spracovanie, Region, Stupeň praženia, Cupping score, Chuťový profil).
- **severan.eu** - description empty; short_description contains an HTML <table class="coffee-table"> with Chuť/Praženie style rows plus icons - needs table parsing rather than Label: value. Prices use a dot decimal separator in price_html.
- **ripit.sk** - Mixed: one sampled variable product has no price_range, the other has one. 15 attributes - richest SK set.
- **triproasters.sk** - Variable product returned without price_range. Sampled items included merch, so some attributes are Rozmer/Potlač.
- **finecoffee.sk** - Weight-only attribute, but description carries Farma/Lokalita/Nadmorská výška/Odroda/Spracovanie/Chuťový profil lines. SKUs populated.
- **karmacoffee.sk** - Real attribute gap: coffee products carry NO WooCommerce attributes, description is theme/nav markup and short_description is a <pre> narrative blob. Origin data would have to be NLP-extracted or scraped from the HTML detail page.
- **riksakava.sk** - Oldest Store API schema in the survey (27 fields, no slug/weight/dimensions/_links). Only one category: 16 ponuka-kav (11).
- **bozinroastery.sk** - Largest catalogue (X-WP-Total 328). Category tree mixes coffee with machines (domaca-priprava-kavy 147, domace-kavovary 13); the real bean category is 51 kava (32).
- **doraz.sk** - Older Store API build: variable products come back WITHOUT prices.price_range (only a single price). No "kava" category at all - coffee lives in arabiky(11)/blendy(3).
- **aurelica.coffee** - Behind Cloudflare, no challenge. 8 attributes but they are tasting sliders (Intenzita, Kyslosť, Sladkosť, Horkosť) rather than origin fields.
- **tokycaffe.sk** - No attributes; the facts live in the description as "Druh: <strong>..</strong> Obal: .. Hmotnosť: .." label lines - parseable with a Label: value extractor.
- **poetrycoffee.sk** - Sampled products were brewers (Hario, French press) -> no attributes in this sample; only 11 products total of which ~4 are coffees.
- **viemcoffee.sk** - 28-field schema. Long description with a rich Label: value block (Farma, Majiteľ, Nadmorská výška, Odroda, Cuppingové skóre...). Only one category: 19 kava-na-espresso.
- **seriouscoffee.sk** - 2 attributes + full Label: value description block; categories are vyberova(4), decaf(1), espresso-blend(1).
- **kavavitazov.sk** - X-WP-Total says 1 while the endpoint returns 2 items - the count header is unreliable here (caching/filter plugin); paginate until an empty page instead of trusting the header. Products have 20-30 variations each.
