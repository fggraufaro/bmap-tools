-- a40 ROLLBACK for the view swap. CREATE OR REPLACE cannot remove columns, so this drops and recreates the view.
-- Only valid while the view has NO dependent database objects (checked at swap time: none).
BEGIN;
SET LOCAL lock_timeout = '5s';
DROP VIEW public.vw_rate_radar_latest;
CREATE VIEW public.vw_rate_radar_latest AS
 WITH attempts AS (
         SELECT DISTINCT ON (raw_rate_radar.bank_name) raw_rate_radar.bank_name,
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
           FROM raw_rate_radar
          ORDER BY raw_rate_radar.bank_name, raw_rate_radar.run_date DESC, raw_rate_radar.crawled_at DESC, raw_rate_radar.inserted_at DESC
        ), piv AS (
         SELECT rate_observations.run_id,
            rate_observations.bank_name,
            max(rate_observations.min_balance) AS min_balance,
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
    p.money_market_verified
   FROM attempts a
     LEFT JOIN piv p ON p.run_id = a.run_id AND p.bank_name = a.bank_name;
ALTER VIEW public.vw_rate_radar_latest OWNER TO postgres;
GRANT MAINTAIN ON public.vw_rate_radar_latest TO anon;
GRANT MAINTAIN ON public.vw_rate_radar_latest TO authenticated;
GRANT DELETE ON public.vw_rate_radar_latest TO service_role;
GRANT INSERT ON public.vw_rate_radar_latest TO service_role;
GRANT MAINTAIN ON public.vw_rate_radar_latest TO service_role;
GRANT REFERENCES ON public.vw_rate_radar_latest TO service_role;
GRANT SELECT ON public.vw_rate_radar_latest TO service_role;
GRANT TRIGGER ON public.vw_rate_radar_latest TO service_role;
GRANT TRUNCATE ON public.vw_rate_radar_latest TO service_role;
GRANT UPDATE ON public.vw_rate_radar_latest TO service_role;
COMMIT;
