-- film_attention: daily Wikipedia pageviews per film.
--
-- Source: Wikimedia Pageviews REST API, keyed on films.enwiki_title (NOT
-- films.title -- the API only knows article names, which carry disambiguation
-- suffixes).
--
-- Window: 2015-07-01, the API's first day, to today. About 4,070 days per
-- article across 1,238 films, so roughly 5.04M rows.
--
-- Every film in this table was released before the window opens. See the
-- interest_* column comments in 001_films.sql for what that means for
-- interpretation; in short, this is catalogue interest, not opening weekend.

CREATE TABLE IF NOT EXISTS film_attention
(
    film_id String,
    date    Date,
    views   UInt32
)
ENGINE = MergeTree
ORDER BY (film_id, date);
