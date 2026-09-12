# Ebenica Coffee (ebenica.sk)

- site_id: `woo_ebenica`  |  country: SK  |  base: `https://ebenica.sk`
- captured: 3 requests, UA `coffee-aggregator/0.2 (+onboarding)`, >= 1 s between requests to the host
- category page (human): https://ebenica.sk/kava/

## robots.txt
- HTTP 200, 4915 bytes
- Crawl-delay: none
- wp-json rules: Disallow: /wp-json/
- Sitemap: https://ebenica.sk/sitemaps.xml

## Store API
- **NOT REQUESTED.** robots.txt disallows `/wp-json/` for `User-agent: *`.
- HTML fixtures captured instead: `list_page1.html` (category listing) and `detail_kava-colombia-medellin.html` (one product page).
- HTML was stripped of `<style>`, inline `<svg>`, HTML comments and base64 data URIs; all `<script>` tags kept.

## Facts
- robots.txt has `Disallow: /wp-json/` for `User-agent: *` -> the Store API was NOT requested at all; HTML capture only (the survey had confirmed the API works and returns 137 products / 69 pages, attribute `pa_variant-kosik` = 'gramáž (produkt)').
- Detail page exposes a full WooCommerce attribute table (`woocommerce-product-attributes-item__label`): Lokalita, Nadmorská výška, Druh, Odroda, Spracovanie, SCA skóre, Praženie, Príprava, Chuť a tóny.
- Detail page carries schema.org JSON-LD (BreadcrumbList + Product @graph + FAQPage) and a `data-product_variations` JSON blob with per-variation `display_price` (variable product, 70 g / 250 g / 1 kg ...).
- Category listing is paginated 12 per page: 'Zobrazených 1–12 z 27 výsledkov'.

## Files
- `detail_kava-colombia-medellin.html` (349805 bytes)
- `list_page1.html` (282159 bytes)
- `notes.md` (1616 bytes)
- `robots.txt` (4915 bytes)
