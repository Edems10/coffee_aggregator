-- The transactional outbox: the intent to publish, stored with the data.
--
-- The crawl writes `coffee`, `price_history`, `coffee_variant` and a row here
-- in one transaction, so the catalogue and the message about it commit together
-- or not at all. Publishing from inside the crawl instead would mean either a
-- coffee stored and never announced (the broker was down) or announced and
-- never stored (the transaction rolled back afterwards); there is no ordering
-- of two systems that avoids both. A separate publisher then drains this table
-- at-least-once, and the `Nats-Msg-Id` it sets plus the stream's duplicate
-- window make the common case effectively-once.

CREATE TABLE IF NOT EXISTS outbox (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject text NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz
);

-- The publisher asks exactly one question — "the oldest rows nobody has sent
-- yet" — and the answer is almost always a handful of rows in a table that is
-- otherwise entirely published history. A full index over `id` would be as
-- large as the table and would make the planner read through a day of sent rows
-- to find the unsent ones; a partial index holds only the queue itself, and a
-- row leaves it the moment it is marked published.
CREATE INDEX IF NOT EXISTS outbox_unpublished_idx ON outbox (id) WHERE published_at IS NULL;

-- Pruning is "everything sent more than a week ago", which reads the published
-- rows the index above deliberately does not hold.
CREATE INDEX IF NOT EXISTS outbox_published_at_idx ON outbox (published_at) WHERE published_at IS NOT NULL;
