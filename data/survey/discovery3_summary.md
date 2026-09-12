# Discovery pass 3 - remaining blogokave.cz candidates + richness scoring

Run date: 2026-09-12. User-Agent `coffee-aggregator-research/0.1`, >= 1 s between requests to the same host,
Crawl-delay honoured where robots.txt declares one, hard cap of 4 requests per domain. No browser UA spoofing;
403 / bot-challenge responses are recorded as unreachable.

Inputs: `discovery2/new_sites.json` (118 sellers of 150 checked), `discovery2/raw/candidates_unchecked.json` (64),
plus `eshopmakacoffee.cz` and `eshop.trafficcoffee.cz` from the discovery2 notes.
Output row format follows `audit/sites_table.json`, with two added keys: `scope` and `sells_beans_online`.

## 1. Part 1 - the 64 unchecked domains (+2 eshop domains)

| Metric | Count |
|---|---|
| Domains checked (homepage + robots.txt, 2 requests each) | 66 |
| **Sell whole-bean coffee online** | **46** |
| Do not sell online / unreachable | 20 |

Platform distribution across all 66 Part 1 domains:

| Platform | Domains |
|---|---|
| shoptet | 19 |
| custom/unknown | 13 |
| woocommerce | 6 |
| unreachable | 6 |
| shopify | 5 |
| eshop-rychle | 5 |
| wordpress (no shop plugin detected) | 4 |
| webnode | 3 |
| upgates | 2 |
| prestashop | 2 |
| wix | 1 |

robots.txt verdict for `User-agent: *` on product/category paths, Part 1:

| Verdict | Domains |
|---|---|
| partially_restricted | 46 |
| no_robots | 8 |
| allowed | 6 |
| unreachable | 6 |

Crawl-delay declared by 20 of the 184 domains in this pass: bunacafe.cz=10.0, caffe08.cz=5, caffediem.cz=5.0, caffeoro.sk=30, casadelcaffe.sk=5, eshop.volkafe.cz=2.0, ferovakava.cz=2.0, kafeprosebe.cz=2, kafesolo.cz=2, kafetrio.cz=2.0, kafujeme.cz=2.0, kavanaknopp.cz=10.0, pappacoffee.cz=10.0, prazirna.com=5.0, prazirnalunga.cz=10, prazirnavimperk.cz=10.0, roprakafe.cz=2.0, skvela-kava.cz=2, soulmatecoffee.cz=10, valeriacoffee.sk=2

## 2. Part 2 - richness scoring

Scored by fetching the coffee category page and one product page from it.
Scoring rule actually applied (5 dimensions: origin, process, variety, altitude, tasting notes;
"origin" is satisfied by a labelled country, region or farm/producer line):

- **high** - >= 4 of the 5 present as discrete / labelled lines, or all 5 present with >= 3 labelled
- **medium** - >= 3 labelled, or a labelled origin plus labelled tasting notes
- **low** - anything less (typically price + free-text description only)

| Metric | Count |
|---|---|
| Sellers carried into Part 2 | 164 |
| - from discovery2 (the 118) | 118 |
| - new sellers found in Part 1 | 46 |
| **Sellers actually scored** | **150** |
| Sellers not scored (see list below) | 14 |

### Richness distribution (150 scored sellers)

| Richness | Sellers | Share |
|---|---|---|
| high | 50 | 33% |
| medium | 66 | 44% |
| low | 34 | 23% |

### Platform distribution among high + medium sellers (116)

| Platform | high | medium | total |
|---|---|---|---|
| shoptet | 25 | 29 | 54 |
| woocommerce | 12 | 11 | 23 |
| shopify | 6 | 4 | 10 |
| custom | 2 | 6 | 8 |
| upgates | 3 | 5 | 8 |
| eshop-rychle | 2 | 6 | 8 |
| prestashop | 0 | 3 | 3 |
| wordpress | 0 | 1 | 1 |
| fastcentrik | 0 | 1 | 1 |

## 3. First wave addition - crawlable AND high richness (50 domains)

"Crawlable" = robots verdict allowed / partially_restricted / no_robots (no `Disallow: /` for `User-agent: *`).

### shoptet (25)

| Domain | Country | Products | Category URL |
|---|---|---|---|
| 1754.cz | CZ | ~3 | https://www.1754.cz/kava/ |
| ajvencoffee.cz | CZ | ~21 | https://www.ajvencoffee.cz/e-shop/ |
| analogprazirna.cz | CZ | ~15 | https://www.analogprazirna.cz/zrnkova-kava/ |
| apecafe.cz | CZ | ~1799 | https://www.apecafe.cz/vyberova-kava/ |
| cafemontana.cz | CZ | ~2 | https://www.cafemontana.cz/zrnkova-kava/ |
| chcikofein.cz | CZ | ~3 | https://www.chcikofein.cz/filtr/ |
| coffee-culture.cz | CZ | ~3 | https://www.coffee-culture.cz/kava/ |
| dotcoffee.cz | CZ | ~23 | https://www.dotcoffee.cz/kava/ |
| eshop.trafficcoffee.cz | CZ | ~9 | https://eshop.trafficcoffee.cz/kava/ |
| fairbio.cz | CZ | ~18 | https://www.fairbio.cz/kava/ |
| frolikovakava.cz | CZ | ~12 | https://www.frolikovakava.cz/vyberove-kavy/ |
| kafekrizka.cz | CZ | ~7 | https://www.kafekrizka.cz/vyberova-kava/ |
| kavajordan.cz | CZ | ~34 | https://www.kavajordan.cz/zrnkova-kava/ |
| kavapodebrady.eu | CZ | ~6 | https://www.kavapodebrady.eu/prazena-kava/ |
| kavavra.cz | CZ | ~3 | https://www.kavavra.cz/vyberova-prazena-kava/ |
| kavovybob.cz | CZ | ~11 | https://www.kavovybob.cz/prazena-kava/ |
| kubaprazikavu.eu | CZ | ~8 | https://www.kubaprazikavu.eu/kava/ |
| labcafe.cz | CZ | ~10 | https://www.labcafe.cz/prazena-vyberova-kava/ |
| liskafe.cz | CZ | ~4 | https://www.liskafe.cz/espresso/ |
| mlkcoffee.cz | CZ | ~8 | https://www.mlkcoffee.cz/kafe/ |
| mohaji.cz | CZ | ~1 | https://www.mohaji.cz/zrnkova-kava/ |
| mrcoffee.cz | CZ | ~5 | https://www.mrcoffee.cz/zrnkova-kava/ |
| opravduhustykafe.cz | CZ | ~7 | https://www.opravduhustykafe.cz/kafe/ |
| pepecoffee.cz | CZ | ~1 | https://www.pepecoffee.cz/bezkofeinova-kava/ |
| zrnicko.cz | CZ | ~18 | https://www.zrnicko.cz/prazena-kava/ |

### woocommerce (12)

| Domain | Country | Products | Category URL |
|---|---|---|---|
| becafe.sk | SK | ~5 | https://becafe.sk/kategoria/kava/vyberova-kava/ |
| cafemontecintu.cz | CZ | ~8 | https://www.cafemontecintu.cz/kategorie-produktu/prazena_kava/ |
| coffeebanditos.cz | CZ | ~5 | https://www.coffeebanditos.cz/obchod/ |
| frisnakava.sk | SK | ~4 | https://frisnakava.sk/kategoria-produktu/prazena-kava/vyberova-kava/ |
| grinders.sk | SK | ~1 | https://australiancoffeecompany.sk/obchod/kava/ |
| herecprazikavu.cz | CZ | ~13 | https://herecprazikavu.cz/kategorie-produktu/vyberova-kava/ |
| incuple.sk | SK | ~6 | https://incuple.sk/kategoria-produktu/kava/vyberova-kava-specialty-coffee/ |
| kocurmt.sk | SK | ? | https://www.kocurmt.sk/kava/ |
| lexacoffee.cz | CZ | ~9 | https://www.lexacoffee.cz/zrnkova-kava-kolumbie/ |
| lubezna.sk | SK | ~71 | https://lubezna.sk/kategoria-produktu/kava/ |
| naturpark12.cz | CZ | ~12 | https://www.naturpark12.cz/kava-na-filtr/ |
| networkcafe.sk | SK | ~11 | https://www.networkcafe.sk/produkt-kategorie/kava/ |

### shopify (6)

| Domain | Country | Products | Category URL |
|---|---|---|---|
| bluemondayscoffee.com | SK | ~12 | https://bluemondays.sk/collections/kavy-na-filter |
| bohemiancoffeehouse.cz | CZ | ~18 | https://bohemiancoffeehouse.cz/collections/vyberove-kavy |
| cityroasters.eu | CZ | ~2 | https://cityroasters.eu/collections/kava/ |
| penguincoffee.cz | CZ | ~10 | https://penguincoffee.cz/collections/vyberova-kava/ |
| raposacoffee.com | CZ | ~10 | https://raposacoffee.com/collections/whole-roasted-beans-1/ |
| twobeans.cz | CZ | ~5 | https://www.twobeans.cz/collections/kava/ |

### upgates (3)

| Domain | Country | Products | Category URL |
|---|---|---|---|
| kawio.cz | CZ | ? | https://www.kopibean.cz/kava |
| praziarenkupele.sk | SK | ? | https://www.praziarenkupele.sk/cerstvo-prazena-kava-1/ |
| theroses.cz | CZ | ? | https://www.theroses.cz/zrnkova-kava/ |

### custom (2)

| Domain | Country | Products | Category URL |
|---|---|---|---|
| bunacafe.cz | CZ | ~146 | https://www.bunacafe.cz/kava |
| miacoffee.cz | CZ | ~46 | https://www.miacoffee.cz/vyberova-kava/ |

### eshop-rychle (2)

| Domain | Country | Products | Category URL |
|---|---|---|---|
| kafetrio.cz | CZ | ~10 | https://www.kafetrio.cz/Jednodruhove-kavy-c1_0_1.htm |
| valeriacoffee.sk | SK | ~5 | https://www.valeriacoffee.sk/espresso/ |

## 4. Unreachable / blocked / not scored

### Unreachable (DNS failure, timeout, TLS error, 403 bot challenge) - 6

| Domain | Note |
|---|---|
| aromacoffee.cz | homepage unreachable - HTTP 403 bot challenge to the declared research User-Agent (no UA spoofing attempted) |
| fierybean.com | homepage unreachable - HTTP 403 bot challenge to the declared research User-Agent (no UA spoofing attempted) |
| grindful.coffee | homepage returned HTTP 200 but an empty / near-empty body (<200 B) - treated as unreachable |
| meadowcoffee.cz | homepage unreachable - DNS does not resolve (NXDOMAIN) |
| prazirnaplzen.cz | homepage unreachable - DNS does not resolve (NXDOMAIN) |
| zrnkazhrnka.cz | homepage unreachable - HTTP 421 Misdirected Request |

### robots.txt `Disallow: /` for `User-agent: *` - 0

none

### Sellers left unscored - 14

| Domain | Reason |
|---|---|
| beanup.sk | not scored (no product link found) |
| cafeeternity.cz | not scored (category page 404) |
| coffeedream.cz | not scored (category page 404) |
| fabricadecafe.cz | not scored (no product link found) |
| idylika.sk | not scored (category link resolution failed - the picked link went to a chocolate product / massage category on the megusta.sk marketplace host) |
| lpopel.cz | not scored (no product link found) |
| lustigcoffee.cz | not scored (no category URL) |
| madeincoffee.cz | not scored (no category URL) |
| prazirnakrok.cz | not scored (no category URL) |
| prazirnazoban.cz | not scored (no product link found) |
| rebels-coffee.cz | not scored (no product link found) |
| sedmitchka.coffee | not scored (no category URL) |
| strakafe.cz | not scored (category link resolution failed - the picked link went to the Shoptet vendor site (shoptet.cz), not the shop) |
| vilemovakava.cz | not scored (no category URL) |

### Domain aliases (site 301-redirects to a different domain) - 9

- `alternativcoffee.sk` -> `avcoffee.sk`
- `bluemondayscoffee.com` -> `bluemondays.sk`
- `coffeeport.sk` -> `lighthousecoffee.sk`
- `grinders.sk` -> `australiancoffeecompany.sk`
- `habeshcoffee.sk` -> `habesh.sk`
- `kawio.cz` -> `kopibean.cz`
- `lificaffe.sk` -> `zahorackapraziaren.sk`
- `makacoffee.com` -> `eshopmakacoffee.cz`
- `trobica.cz` -> `kavatrobica.cz`

These are the same shop under two names; keep only one when building the crawl list.

## 5. Request accounting

| Metric | Value |
|---|---|
| **Total HTTP requests this pass** | **439** |
| Part 1 (homepage + robots.txt) | 128 |
| Distinct domains contacted | 181 |
| Max requests to a single domain | 4 (cap 4) |
| HTTP 200 | 425 |
| HTTP 403 / 404 / other | 10 |
| Connection errors (DNS/TLS/timeout) | 4 |

Cached homepages and robots.txt for the 118 discovery2 sellers were reused from `discovery2/raw/`,
so no request was spent re-fetching them; their 2 requests here are the category page and the product page.
