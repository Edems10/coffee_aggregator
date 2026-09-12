-- 0001_initial — the base schema: plain PostgreSQL, no extensions required.
--
-- Everything is reached through one DATABASE_URL, so the same DDL and the same
-- code run against every deployment:
--   local docker      DATABASE_URL=postgresql://coffee:coffee@localhost:5432/coffee
--                     (docker compose up -d, then `coffee-aggregator init-db`)
--   AWS RDS           DATABASE_URL=postgresql://USER:PASSWORD@db.eu-central-1.rds.amazonaws.com:5432/coffee?sslmode=require
--   Supabase          DATABASE_URL=postgresql://postgres:PASSWORD@db.PROJECT.supabase.co:5432/postgres
--                     (the *direct* Postgres connection string, not the REST API)
--   any other managed Postgres: the same postgresql:// URL.
--
-- Every statement is IF NOT EXISTS, so applying this file to a database that was
-- created by the pre-migrations `schema.sql` is a no-op and only records the
-- version.

CREATE TABLE IF NOT EXISTS coffee (
    site                    text        NOT NULL,
    external_id             text        NOT NULL,
    url                     text,
    name                    text,
    site_country            text,
    price                   numeric,
    currency                text,
    weight_g                int,
    price_per_kg            numeric,
    available               bool,
    decaf                   bool,
    origin_country          text,
    origin_region           text,
    origin_farm             text,
    origin_producer         text,
    origin_washing_station  text,
    altitude_min_m          int,
    altitude_max_m          int,
    altitude_raw            text,
    variety                 jsonb,
    harvest                 text,
    process_method          text,
    process_methods         jsonb,
    process_raw             text,
    roast_level             text,
    roast_raw               text,
    roast_profile           text,
    roast_date              date,
    best_before             date,
    arabica_pct             int,
    robusta_pct             int,
    is_blend                bool,
    body                    int,
    bitterness              int,
    acidity                 int,
    sweetness               int,
    taste_scale_max         int,
    flavor_notes            jsonb,
    tasting_text            text,
    brewing_methods         jsonb,
    sca_score               numeric,
    rating                  numeric,
    rating_max              int,
    review_count            int,
    reviews                 jsonb,
    sold_count              int,
    variants                jsonb,
    images                  jsonb,
    tags                    jsonb,
    categories              jsonb,
    certifications          jsonb,
    awards                  jsonb,
    specialty_grade         bool,
    original_price          numeric,
    description             text,
    origin_text             text,
    raw_attributes          jsonb,
    scraped_at              timestamptz,
    first_seen_at           timestamptz NOT NULL DEFAULT now(),
    last_seen_at            timestamptz NOT NULL DEFAULT now(),
    delisted_at             timestamptz,
    PRIMARY KEY (site, external_id)
);

CREATE INDEX IF NOT EXISTS coffee_site_idx ON coffee (site);
CREATE INDEX IF NOT EXISTS coffee_origin_country_idx ON coffee (origin_country);
CREATE INDEX IF NOT EXISTS coffee_delisted_at_idx ON coffee (delisted_at);

CREATE TABLE IF NOT EXISTS price_history (
    id           bigserial   PRIMARY KEY,
    site         text        NOT NULL,
    external_id  text        NOT NULL,
    seen_at      timestamptz NOT NULL DEFAULT now(),
    price        numeric,
    currency     text,
    weight_g     int,
    available    bool
);

CREATE INDEX IF NOT EXISTS price_history_product_idx
    ON price_history (site, external_id, seen_at);
