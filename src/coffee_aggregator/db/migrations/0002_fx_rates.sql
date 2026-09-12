-- 0002_fx_rates — prices comparable across the CZK and EUR shops.
--
-- One EUR/CZK fixing per day is stored in `fx_rates` and stamped onto every row
-- the crawl writes, so a query can rank a Czech and a Slovak roastery against
-- each other without knowing which rate was in force when the row was made.

ALTER TABLE coffee ADD COLUMN IF NOT EXISTS price_eur         numeric;
ALTER TABLE coffee ADD COLUMN IF NOT EXISTS price_czk         numeric;
ALTER TABLE coffee ADD COLUMN IF NOT EXISTS price_per_kg_eur  numeric;
ALTER TABLE coffee ADD COLUMN IF NOT EXISTS price_per_kg_czk  numeric;
ALTER TABLE coffee ADD COLUMN IF NOT EXISTS fx_rate_eur_czk   numeric;
ALTER TABLE coffee ADD COLUMN IF NOT EXISTS fx_date           date;

ALTER TABLE price_history ADD COLUMN IF NOT EXISTS price_eur       numeric;
ALTER TABLE price_history ADD COLUMN IF NOT EXISTS price_czk       numeric;
ALTER TABLE price_history ADD COLUMN IF NOT EXISTS fx_rate_eur_czk numeric;

-- One row per fixing day per pair. `date` is the day the fixing belongs to, not
-- the day it was downloaded: on a weekend the Czech National Bank still serves
-- the last working day's file, and that is the date recorded.
CREATE TABLE IF NOT EXISTS fx_rates (
    date        date          NOT NULL,
    base        text          NOT NULL,
    quote       text          NOT NULL,
    rate        numeric(12,6) NOT NULL,
    source      text,
    fetched_at  timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (date, base, quote)
);

CREATE INDEX IF NOT EXISTS coffee_price_per_kg_eur_idx ON coffee (price_per_kg_eur);
