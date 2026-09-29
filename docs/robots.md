# robots.txt across the shops

What every shop we scrape declares in its `robots.txt`, on one page.

**This page is documentation, not an input to the code.** `robots.txt` is enforced
live at runtime by the fetcher (`src/coffee_aggregator/http/fetcher.py`): it fetches
each host's `robots.txt` once, caches it per origin, honours any `Crawl-delay` that
applies to our agent (including for the first content request), and **fails closed** —
if `robots.txt` cannot be read (network error, 401, 403, redirect loop), the whole host
is treated as disallowed. Nothing below is read by the crawler; changing this file
changes nothing.

Rules that name AI crawlers (`GPTBot`, `ClaudeBot`, `CCBot`, `anthropic-ai`,
`Google-Extended`, `Bytespider`, …) **do not apply to us**: this crawler identifies as
`coffee-aggregator` (`DEFAULT_USER_AGENT_NAME` in `src/coffee_aggregator/config.py`),
so it matches the `User-agent: *` group. The same goes for `Content-Signal:`
lines such as `ai-train=no` — they are a licensing signal about training, not a crawl
rule, and we neither train nor fine-tune on this data. Several shops (coffeeveronia,
ebenica) block AI-training agents by name while leaving `*` free; we are not one of them.

The **verdict** column is about the two things the crawler actually walks: **category
listing pages** and **product detail pages**.

- *allowed* — nothing in the `*` group blocks either.
- *partially restricted* — the listing and detail URLs themselves are allowed, but some
  variants of them are not (faceted/sorted listing query strings, product sub-tabs).
- *no robots file* — no `robots.txt` is served for that host.

The **Disallow** column keeps only the lines that matter for that walk. Almost every shop
also blocks the usual admin/cart/checkout/login/registration/account/order/search/export/API
paths; **those are omitted throughout** and are never on our crawl path anyway.

Captured **2026-09-12**. Sources: the nineteen per-shop `robots.txt` copies that lived in
`tests/fixtures/*/` (read verbatim), and the site survey
`data/survey/sites_survey_2026-09-12.{csv,json}` in the 2026-09-12 handoff archive for
the rest. No shop below declares a `Crawl-delay` at all, so the fetcher's configured
global delay is what paces every host.

> **Do not delete `tests/fixtures/woo_*/robots.txt`.** Three of the original nineteen
> fixture copies are live test inputs, not documentation: `woo_ebenica/robots.txt`,
> `woo_kavaloka/robots.txt` and `woo_ripit/robots.txt` are parsed by
> `tests/test_woocommerce.py` (`test_robots_txt_closes_the_store_api_on_the_html_mode_shop`
> and `test_the_open_shops_allow_the_store_api`) to prove that ebenica.sk forbids
> `/wp-json` — which is why that shop runs in HTML mode — while kavaloka and ripit leave
> the Store API open. The other sixteen copies were read by nothing and were replaced by
> this page.

## Shorthand

Three recurring rule sets are referenced by name instead of being repeated 40 times.

**Shoptet standard (CZ)** — the stock Shoptet `robots.txt`. Category and product URLs
are allowed; what is blocked is every *parameterised* form of a listing plus the product
page's own sub-tabs:

```
Disallow: /*?priceMax      Disallow: /*?priceMin       Disallow: /*?parameterId
Disallow: /*?order         Disallow: /*?availabilityId Disallow: /*?manufacturerId
Disallow: /*?stock         Disallow: /*/?letter=*      Disallow: /*/?backTo=
Disallow: /*?pv*=*,        Disallow: /*&pv*=*,         Disallow: /*?pv*=*&pv*=
Disallow: /*&pv*=*&pv*=    Disallow: /*?dd=*,          Disallow: /*&dd=*,
Disallow: /*?dd=*&pv*=     Disallow: /*?pv*=*&dd=      Disallow: /*:*,*/
Disallow: /*:diskuse       Disallow: /*:dotaz          Disallow: /*:hlidat-cenu
Disallow: /*:klient-hodnoceni  Disallow: /*:moznosti-dopravy  Disallow: /*:wysiwyg
```

**Shoptet standard (SK)** — identical, with the Slovak sub-tab names:
`/*:diskusia`, `/*:otazka`, `/*:strazit-cenu`, `/*:klient-hodnotenie`,
`/*:moznosti-dorucenia`, `/*:wysiwyg`.

**Shoptet standard (EN)** — identical, with the English sub-tab names:
`/*:discussion`, `/*:ask-salesman`, `/*:price-guard`, `/*:client-ratings`,
`/*:delivery-offer`, `/*:wysiwyg`.

Practical consequence for all three: **crawl the clean category path and paginate it;
never follow a filter, sort or `letter=` link, and never follow a `:`-suffixed product
tab.**

## The shops

| Shop (site id) | Domain | Verdict for `User-agent: *` | Crawl-delay | Relevant `Disallow` lines |
| --- | --- | --- | --- | --- |
| alacoffee | www.alacoffee.cz | partially restricted | — | Shoptet standard (CZ) |
| alegrecafe | www.alegrecafe.cz | partially restricted | — | Shoptet standard (CZ) |
| birdsong | www.birdsong.cz | partially restricted | — | Shoptet standard (CZ) |
| botacoffee | www.botacoffee.cz | partially restricted | — | Shoptet standard (CZ) |
| cafegape | www.cafegape.eu | partially restricted | — | Shoptet standard (CZ) |
| chroast | www.chroast.cz | partially restricted | — | Shoptet standard (CZ) |
| coffeesheep | www.coffeesheep.sk | partially restricted | — | Shoptet standard (SK) |
| coffeespot | www.coffeespot.cz | partially restricted | — | Shoptet standard (CZ). `Content-Signal: search=yes, ai-input=yes, ai-train=yes` — the one shop that positively permits training |
| coffeeveronia | www.coffeeveronia.sk | allowed | — | none — the `*` group is just `Allow: /`. Cloudflare-managed `Disallow: /` blocks for named AI bots (GPTBot, ClaudeBot, CCBot, Google-Extended, Bytespider, Amazonbot, Applebot-Extended, meta-externalagent) do not match us |
| conceptcoffee | www.conceptcoffee.sk | partially restricted | — | Shoptet standard (SK) |
| crosscafeprazirna | www.crosscafeprazirna.cz | partially restricted | — | Shoptet standard (CZ) |
| daliacoffee | www.daliacoffee.cz | partially restricted | — | Shoptet standard (CZ). Survey graded this one `allowed`; nothing on the category/product path is in fact blocked |
| dibosco | www.dibosco.sk | partially restricted | — | Shoptet standard (SK) |
| dosmundos | www.dos-mundos.cz | partially restricted | — | Shoptet standard (CZ) |
| ebenica | ebenica.sk | partially restricted | — | WooCommerce. Blanket facet blocks `*/mletie_kavy-*`, `*/druh_kavy-*`, `*/priprava_kavy-*`, `*/chut_kavy-*`, `*/obsah_kofeinu-*`, `*/ocenena_kava-*`, plus `/*/feed/*`, with an explicit `Allow:` list of ~8 indexed filter URLs (and their `/page/` forms). The plain `/kava/` listing and product pages are allowed. Caveat: `urllib.robotparser`, which the fetcher uses, silently discards this file's whole `User-agent: *` group because blank lines and comments separate the `User-agent` line from its first rule — so at runtime none of these rules are actually applied to us |
| henri | www.henri.cz | partially restricted | — | Shoptet standard (CZ) |
| jungleroastery | www.jungleroastery.sk | partially restricted | — | Shoptet standard (SK) |
| kafista | www.kafista.cz | partially restricted | — | Shoptet standard (CZ) |
| kavaloka | www.kavaloka.cz | allowed | — | none. A Yoast-emitted second `User-agent: *` group with a bare `Disallow:` (empty) follows the WooCommerce uploads/`wp-admin` group |
| kavicka | www.kavicka.sk | partially restricted | — | Shoptet standard (SK) |
| kavomil | www.kavomil.sk | partially restricted | — | Shoptet standard (SK) |
| kavypitel | www.kavypitel.cz | partially restricted | — | Shoptet standard (CZ) |
| kikafe | shop.kikafe.cz | partially restricted | — | Shoptet standard (CZ). The apex `kikafe.cz/robots.txt` is an HTML 404; the shop and its robots live on `shop.kikafe.cz` |
| kofi | www.kofi.sk | partially restricted | — | Shoptet standard (SK) |
| mamacoffee | www.mamacoffee.cz | partially restricted | — | Shoptet standard (CZ) |
| melodyroastery | www.melodyroastery.sk | partially restricted | — | Shoptet standard (SK) |
| motmot | www.motmot.cz | partially restricted | — | Shoptet standard (CZ) |
| ohmybean | www.ohmybean.coffee | partially restricted | — | Shoptet standard (CZ) |
| penerini | eshop.penerini.cz | partially restricted | — | Shoptet standard (CZ). `penerini.cz/robots.txt` serves the HTML homepage (no robots file on the apex); the shop is on `eshop.penerini.cz` |
| prazirnabrno | www.prazirnabrno.cz | partially restricted | — | Shoptet standard (CZ) |
| prazirnaignac | www.prazirnaignac.cz | partially restricted | — | Shoptet standard (CZ) |
| putovniprazirna | shop.putovniprazirna.cz | partially restricted | — | Shoptet standard (CZ). The apex `putovniprazirna.cz/robots.txt` is an HTML 404; the shop is on `shop.putovniprazirna.cz` |
| readyafter | www.readyafter.sk | partially restricted | — | Shoptet standard (SK) |
| rebelbean | www.rebelbean.cz | partially restricted | — | Shoptet standard (CZ) |
| redfawn | www.redfawn.sk | partially restricted | — | Shoptet standard (SK) |
| ripit | ripit.sk | allowed | — | WooCommerce, admin/search noise only (`/wp-content/uploads/…`, `/*?add-to-cart=`, `/?s=`, `/search/`). Nothing on the category or product path |
| theminers | www.theminers.eu | partially restricted | — | Shoptet standard (EN), plus `/client/` |
| urbancoffee | www.urbancoffee.sk | partially restricted | — | Shoptet standard (SK) |
| usteckakava | www.usteckakava.cz | partially restricted | — | Shoptet standard (CZ) |
| valasska | www.valasska-prazirna.cz | partially restricted | — | Shoptet standard (CZ) |
| coffeein *(bespoke)* | www.coffeein.sk | allowed | — | none — the entire file is `User-agent: *` / `Allow: /`. No `Sitemap` line either, though `/sitemap.xml` exists |
| nordbeans *(bespoke)* | www.nordbeans.cz | allowed | — | `/uzivatel/`, `/graphql`, `/admin/graphql`. Nothing on the category or product path |
| fathers *(bespoke)* | fathers.cz | allowed | — | Tracking-parameter forms only: `/?utm`, `/&utm`, `/?gclid=*`, `/&gclid=*`, `/?modal=*`, `/&modal=*`. (The account/cart/subscription paths, CZ and `/en/`, are the omitted noise.) This shop explicitly `Allow: /`s GPTBot, ClaudeBot, PerplexityBot, Google-Extended, Applebot and Bytespider |

43 shops: 40 with a config under `src/coffee_aggregator/sites/configs/`, plus the three
bespoke adapters. Every one of them is covered — none had to be listed as "not captured".
