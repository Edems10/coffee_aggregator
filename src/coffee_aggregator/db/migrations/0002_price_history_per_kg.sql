-- The price per kilogram, kept with the price it was derived from.
--
-- `price_history` already stores `price_eur` and `price_czk` beside the shop's
-- own price, so the series stays comparable without re-running the day's FX
-- fixing against it. The per-kilogram figure needs storing for the same reason
-- and for a sharper one: it is derived from `price` AND `weight_g`, and the
-- weight is the part that goes missing. On 2026-10-02 two frolikovakava
-- coffees lost their stated weight to a page change at an unchanged price, and
-- their kilogram price for every earlier day became uncomputable with it —
-- recorded, it would have survived.
--
-- It is also the figure the catalogue is ranked on, so reading a product's
-- history should not mean dividing it back out row by row.

ALTER TABLE price_history ADD COLUMN IF NOT EXISTS price_per_kg_eur numeric;
ALTER TABLE price_history ADD COLUMN IF NOT EXISTS price_per_kg_czk numeric;

-- Backfill what the stored price and weight can still answer for. Rows whose
-- weight was never recorded stay NULL, which is the truthful answer: no
-- kilogram price was knowable on that day.
UPDATE price_history
SET price_per_kg_eur = round(price_eur * 1000 / weight_g, 2)
WHERE price_per_kg_eur IS NULL AND price_eur IS NOT NULL AND weight_g > 0;

UPDATE price_history
SET price_per_kg_czk = round(price_czk * 1000 / weight_g, 2)
WHERE price_per_kg_czk IS NULL AND price_czk IS NOT NULL AND weight_g > 0;

-- "What did this coffee cost per kilogram over time" is the query this table
-- exists for; the day index alone makes it read the row to find out.
CREATE INDEX IF NOT EXISTS price_history_per_kg_eur_idx
    ON price_history USING btree (site, external_id, seen_on, price_per_kg_eur);
