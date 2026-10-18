-- a40: swap public.vw_rate_radar_latest to the RSSD-keyed definition with confidence and conditional columns.
-- Run as ONE transaction after Session 4 sign-off and Francisco's yes (not before 10/15).
-- CREATE OR REPLACE keeps the owner and the grants; existing columns keep name, type and order; 10 columns are appended.
BEGIN;
SET LOCAL lock_timeout = '5s';
CREATE TABLE IF NOT EXISTS backup.bak_a40_vw_rate_radar_latest_def AS
  SELECT now() AS saved_at, pg_get_viewdef('public.vw_rate_radar_latest'::regclass, true) AS definition,
         (SELECT relacl::text FROM pg_class WHERE oid = 'public.vw_rate_radar_latest'::regclass) AS acl;
ALTER TABLE backup.bak_a40_vw_rate_radar_latest_def ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON backup.bak_a40_vw_rate_radar_latest_def FROM anon, authenticated, PUBLIC;
-- snapshot of the existing columns (name, type, position) to prove they survive the replace
CREATE TEMP TABLE _a40_cols_before ON COMMIT DROP AS
  SELECT ordinal_position, column_name, data_type
  FROM information_schema.columns
  WHERE table_schema = 'public' AND table_name = 'vw_rate_radar_latest';
CREATE OR REPLACE VIEW public.vw_rate_radar_latest AS
 WITH attempts AS (
         SELECT DISTINCT ON (raw_rate_radar.identity_key) raw_rate_radar.bank_name,
            raw_rate_radar.run_id,
            raw_rate_radar.run_date,
            raw_rate_radar.crawled_at,
            raw_rate_radar.bank_url,
            raw_rate_radar.rssdid,
            raw_rate_radar.branch_address,
            raw_rate_radar.status,
            raw_rate_radar.note,
            raw_rate_radar.vulnerability_flag,
            raw_rate_radar.min_balance AS attempt_min_balance
           FROM ( SELECT rr.*,
                    COALESCE(NULLIF(rr.rssdid, ''::text),
                             CASE WHEN min(NULLIF(rr.rssdid, ''::text)) OVER (PARTITION BY lower(rr.bank_name))
                                     = max(NULLIF(rr.rssdid, ''::text)) OVER (PARTITION BY lower(rr.bank_name))
                                  THEN max(NULLIF(rr.rssdid, ''::text)) OVER (PARTITION BY lower(rr.bank_name))
                             END,
                             'name:'::text || lower(rr.bank_name)) AS identity_key
                   FROM raw_rate_radar rr ) raw_rate_radar
          ORDER BY raw_rate_radar.identity_key, raw_rate_radar.run_date DESC, raw_rate_radar.crawled_at DESC, raw_rate_radar.inserted_at DESC
        ), piv AS (
         SELECT rate_observations.run_id,
            rate_observations.bank_name,
            max(rate_observations.min_balance) AS min_balance,
            max(rate_observations.rssdid) AS obs_rssdid,
            max(
                CASE
                    WHEN rate_observations.product_type = 'checking'::text THEN rate_observations.apy
                    ELSE NULL::numeric
                END) AS checking_apy,
            max(
                CASE
                    WHEN rate_observations.product_type = 'checking'::text THEN rate_observations.extraction_method
                    ELSE NULL::text
                END) AS checking_provenance,
            max(
                CASE
                    WHEN rate_observations.product_type = 'checking'::text THEN rate_observations.source_url
                    ELSE NULL::text
                END) AS source_url_checking,
            bool_or(
                CASE
                    WHEN rate_observations.product_type = 'checking'::text THEN rate_observations.manually_verified
                    ELSE NULL::boolean
                END) AS checking_verified,
            max(
                CASE
                    WHEN rate_observations.product_type = 'savings'::text THEN rate_observations.apy
                    ELSE NULL::numeric
                END) AS savings_apy,
            max(
                CASE
                    WHEN rate_observations.product_type = 'savings'::text THEN rate_observations.extraction_method
                    ELSE NULL::text
                END) AS savings_provenance,
            max(
                CASE
                    WHEN rate_observations.product_type = 'savings'::text THEN rate_observations.source_url
                    ELSE NULL::text
                END) AS source_url_savings,
            bool_or(
                CASE
                    WHEN rate_observations.product_type = 'savings'::text THEN rate_observations.manually_verified
                    ELSE NULL::boolean
                END) AS savings_verified,
            max(
                CASE
                    WHEN rate_observations.product_type = 'high_yield_savings'::text THEN rate_observations.apy
                    ELSE NULL::numeric
                END) AS high_yield_savings_apy,
            max(
                CASE
                    WHEN rate_observations.product_type = 'high_yield_savings'::text THEN rate_observations.extraction_method
                    ELSE NULL::text
                END) AS high_yield_savings_provenance,
            max(
                CASE
                    WHEN rate_observations.product_type = 'high_yield_savings'::text THEN rate_observations.source_url
                    ELSE NULL::text
                END) AS source_url_high_yield_savings,
            bool_or(
                CASE
                    WHEN rate_observations.product_type = 'high_yield_savings'::text THEN rate_observations.manually_verified
                    ELSE NULL::boolean
                END) AS high_yield_savings_verified,
            max(
                CASE
                    WHEN rate_observations.product_type = 'cd'::text THEN rate_observations.apy
                    ELSE NULL::numeric
                END) AS cd_apy,
            max(
                CASE
                    WHEN rate_observations.product_type = 'cd'::text THEN rate_observations.term_months
                    ELSE NULL::integer
                END) AS cd_term,
            max(
                CASE
                    WHEN rate_observations.product_type = 'cd'::text THEN rate_observations.extraction_method
                    ELSE NULL::text
                END) AS cd_provenance,
            max(
                CASE
                    WHEN rate_observations.product_type = 'cd'::text THEN rate_observations.source_url
                    ELSE NULL::text
                END) AS source_url_cd,
            bool_or(
                CASE
                    WHEN rate_observations.product_type = 'cd'::text THEN rate_observations.manually_verified
                    ELSE NULL::boolean
                END) AS cd_verified,
            max(
                CASE
                    WHEN rate_observations.product_type = 'money_market'::text THEN rate_observations.apy
                    ELSE NULL::numeric
                END) AS money_market_apy,
            max(
                CASE
                    WHEN rate_observations.product_type = 'money_market'::text THEN rate_observations.extraction_method
                    ELSE NULL::text
                END) AS money_market_provenance,
            bool_or(
                CASE
                    WHEN rate_observations.product_type = 'money_market'::text THEN rate_observations.manually_verified
                    ELSE NULL::boolean
                END) AS money_market_verified,
            CASE min(
                CASE
                    WHEN rate_observations.product_type = 'checking'::text THEN
                        CASE rate_observations.confidence
                            WHEN 'low'::text THEN 0
                            WHEN 'medium'::text THEN 1
                            WHEN 'high'::text THEN 2
                            ELSE 0
                        END
                    ELSE NULL::integer
                END)
                WHEN 0 THEN 'low'::text
                WHEN 1 THEN 'medium'::text
                WHEN 2 THEN 'high'::text
                ELSE NULL::text
            END AS checking_confidence,
            bool_or(
                CASE
                    WHEN rate_observations.product_type = 'checking'::text THEN rate_observations.is_conditional
                    ELSE NULL::boolean
                END) AS checking_conditional,
            CASE min(
                CASE
                    WHEN rate_observations.product_type = 'savings'::text THEN
                        CASE rate_observations.confidence
                            WHEN 'low'::text THEN 0
                            WHEN 'medium'::text THEN 1
                            WHEN 'high'::text THEN 2
                            ELSE 0
                        END
                    ELSE NULL::integer
                END)
                WHEN 0 THEN 'low'::text
                WHEN 1 THEN 'medium'::text
                WHEN 2 THEN 'high'::text
                ELSE NULL::text
            END AS savings_confidence,
            bool_or(
                CASE
                    WHEN rate_observations.product_type = 'savings'::text THEN rate_observations.is_conditional
                    ELSE NULL::boolean
                END) AS savings_conditional,
            CASE min(
                CASE
                    WHEN rate_observations.product_type = 'high_yield_savings'::text THEN
                        CASE rate_observations.confidence
                            WHEN 'low'::text THEN 0
                            WHEN 'medium'::text THEN 1
                            WHEN 'high'::text THEN 2
                            ELSE 0
                        END
                    ELSE NULL::integer
                END)
                WHEN 0 THEN 'low'::text
                WHEN 1 THEN 'medium'::text
                WHEN 2 THEN 'high'::text
                ELSE NULL::text
            END AS high_yield_savings_confidence,
            bool_or(
                CASE
                    WHEN rate_observations.product_type = 'high_yield_savings'::text THEN rate_observations.is_conditional
                    ELSE NULL::boolean
                END) AS high_yield_savings_conditional,
            CASE min(
                CASE
                    WHEN rate_observations.product_type = 'cd'::text THEN
                        CASE rate_observations.confidence
                            WHEN 'low'::text THEN 0
                            WHEN 'medium'::text THEN 1
                            WHEN 'high'::text THEN 2
                            ELSE 0
                        END
                    ELSE NULL::integer
                END)
                WHEN 0 THEN 'low'::text
                WHEN 1 THEN 'medium'::text
                WHEN 2 THEN 'high'::text
                ELSE NULL::text
            END AS cd_confidence,
            bool_or(
                CASE
                    WHEN rate_observations.product_type = 'cd'::text THEN rate_observations.is_conditional
                    ELSE NULL::boolean
                END) AS cd_conditional,
            CASE min(
                CASE
                    WHEN rate_observations.product_type = 'money_market'::text THEN
                        CASE rate_observations.confidence
                            WHEN 'low'::text THEN 0
                            WHEN 'medium'::text THEN 1
                            WHEN 'high'::text THEN 2
                            ELSE 0
                        END
                    ELSE NULL::integer
                END)
                WHEN 0 THEN 'low'::text
                WHEN 1 THEN 'medium'::text
                WHEN 2 THEN 'high'::text
                ELSE NULL::text
            END AS money_market_confidence,
            bool_or(
                CASE
                    WHEN rate_observations.product_type = 'money_market'::text THEN rate_observations.is_conditional
                    ELSE NULL::boolean
                END) AS money_market_conditional,
            max(rate_observations.observed_at) AS run_at
           FROM rate_observations
          GROUP BY rate_observations.run_id, rate_observations.bank_name
        )
 SELECT a.bank_name,
    a.bank_url,
    a.rssdid,
    a.branch_address,
    p.checking_apy,
    p.checking_provenance,
    p.savings_apy,
    p.savings_provenance,
    p.cd_apy,
    p.cd_term,
    p.cd_provenance,
    p.money_market_apy,
    p.money_market_provenance,
    COALESCE(p.min_balance, a.attempt_min_balance) AS min_balance,
    a.status,
    a.note,
    a.vulnerability_flag,
    p.source_url_checking,
    p.source_url_savings,
    p.source_url_cd,
        CASE
            WHEN (p.savings_apy IS NULL OR (p.savings_provenance = ANY (ARRAY['site'::text, 'ai_vision'::text]))) AND (p.checking_apy IS NULL OR (p.checking_provenance = ANY (ARRAY['site'::text, 'ai_vision'::text]))) AND (p.cd_apy IS NULL OR (p.cd_provenance = ANY (ARRAY['site'::text, 'ai_vision'::text]))) AND COALESCE(p.savings_apy, p.checking_apy, p.cd_apy) IS NOT NULL THEN 'verified'::text
            WHEN (p.savings_provenance = ANY (ARRAY['site'::text, 'ai_vision'::text])) OR (p.checking_provenance = ANY (ARRAY['site'::text, 'ai_vision'::text])) OR (p.cd_provenance = ANY (ARRAY['site'::text, 'ai_vision'::text])) THEN 'mixed'::text
            WHEN COALESCE(p.savings_apy, p.checking_apy, p.cd_apy, p.money_market_apy) IS NOT NULL THEN 'search-only'::text
            ELSE 'no-data'::text
        END AS data_quality,
    a.run_id,
    a.run_date,
    COALESCE(p.run_at, a.crawled_at) AS run_at,
    p.high_yield_savings_apy,
    p.high_yield_savings_provenance,
    p.source_url_high_yield_savings,
    p.checking_verified,
    p.savings_verified,
    p.high_yield_savings_verified,
    p.cd_verified,
    p.money_market_verified,
    p.checking_confidence,
    p.checking_conditional,
    p.savings_confidence,
    p.savings_conditional,
    p.high_yield_savings_confidence,
    p.high_yield_savings_conditional,
    p.cd_confidence,
    p.cd_conditional,
    p.money_market_confidence,
    p.money_market_conditional
   FROM attempts a
     LEFT JOIN piv p ON p.run_id = a.run_id AND p.bank_name = a.bank_name
       AND (p.obs_rssdid IS NULL OR a.rssdid IS NULL OR p.obs_rssdid = a.rssdid);
-- exact in-transaction checks (the whole transaction fails on any mismatch):
--  1) row count = number of distinct bank identities in raw_rate_radar (same rule as the view);
--  2) the original columns keep their names, types and positions, and exactly 10 columns were appended.
DO $$
DECLARE
  expected integer;
  actual integer;
  n_before integer;
  n_kept integer;
  n_after integer;
BEGIN
  SELECT count(*) INTO expected FROM (
    SELECT DISTINCT COALESCE(NULLIF(rr.rssdid, ''),
                             CASE WHEN min(NULLIF(rr.rssdid, '')) OVER w = max(NULLIF(rr.rssdid, '')) OVER w
                                  THEN max(NULLIF(rr.rssdid, '')) OVER w END,
                             'name:' || lower(rr.bank_name)) AS identity_key
    FROM raw_rate_radar rr
    WINDOW w AS (PARTITION BY lower(rr.bank_name))
  ) x;
  SELECT count(*) INTO actual FROM public.vw_rate_radar_latest;
  IF actual <> expected THEN
    RAISE EXCEPTION 'row count % does not equal the expected distinct bank identities %', actual, expected;
  END IF;

  SELECT count(*) INTO n_before FROM _a40_cols_before;
  SELECT count(*) INTO n_kept
  FROM _a40_cols_before b
  JOIN information_schema.columns a
    ON a.table_schema = 'public' AND a.table_name = 'vw_rate_radar_latest'
   AND a.ordinal_position = b.ordinal_position AND a.column_name = b.column_name AND a.data_type = b.data_type;
  IF n_kept <> n_before THEN
    RAISE EXCEPTION 'original columns changed: % of % kept name, type and position', n_kept, n_before;
  END IF;
  SELECT count(*) INTO n_after FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'vw_rate_radar_latest';
  IF n_after <> n_before + 10 THEN
    RAISE EXCEPTION 'expected % columns after the swap, found %', n_before + 10, n_after;
  END IF;
  -- privileges must be untouched: anon and authenticated may hold nothing that reads or writes the view
  IF has_table_privilege('anon', 'public.vw_rate_radar_latest', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE')
     OR has_table_privilege('authenticated', 'public.vw_rate_radar_latest', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE') THEN
    RAISE EXCEPTION 'anon or authenticated can read or write public.vw_rate_radar_latest after the swap';
  END IF;
END $$;
COMMIT;
-- afterwards: re-check grants and RLS (select * from the public-key path must still be refused), re-run ingestion.run_security_check.
