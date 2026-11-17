"""Static checks on the RR01 migration files (stdlib unittest). The SQL itself was dry-run on a scratch PostgreSQL; these tests keep the properties that matter
from being edited away: additive only, row security on, nothing for anon / authenticated, no change to apy NOT NULL, a guard, a rollback."""
import os
import re
import unittest

SQL_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "sql")


def sql(name):
    with open(os.path.join(SQL_DIR, name), encoding="utf-8") as fh:
        return re.sub(r"--[^\n]*", "", fh.read())   # without comments


APPLY, ROLLBACK, VERIFY = sql("RR01_Apply.sql"), sql("RR01_Rollback.sql"), sql("RR01_Verify.sql")


class ApplyTests(unittest.TestCase):
    def test_runs_in_one_transaction(self):
        self.assertTrue(APPLY.strip().lower().startswith("begin;"))
        self.assertTrue(APPLY.strip().lower().endswith("commit;"))

    def test_adds_the_two_columns_additively(self):
        self.assertIn("add column review_flags text[] not null default '{}'", APPLY)
        self.assertIn("add column tier_note text;", APPLY)

    def test_never_alters_or_drops_existing_columns_or_rows(self):
        for bad in ("alter column", "drop column", "drop table", "delete from", "update public.rate_observations", "truncate", "set not null", "drop not null", "drop constraint"):
            self.assertNotIn(bad, APPLY.lower(), bad)

    def test_apy_stays_not_null(self):
        self.assertNotRegex(APPLY.lower(), r"apy[^;]*(drop not null|null;)")

    def test_new_table_has_row_security_and_service_role_only(self):
        self.assertIn("alter table public.rate_not_published enable row level security;", APPLY)
        self.assertIn("revoke all on public.rate_not_published from public, anon, authenticated, service_role;", APPLY)
        self.assertIn("grant select, insert, update, delete on public.rate_not_published to service_role;", APPLY)
        self.assertEqual(len(re.findall(r"grant\b", APPLY.lower())), 1)

    def test_product_check_lists_the_six_values(self):
        m = re.search(r"product_type text not null check \(product_type in \(([^)]*)\)\)", APPLY)
        self.assertIsNotNone(m)
        self.assertEqual(re.findall(r"'([a-z_]+)'", m.group(1)), ["checking", "savings", "high_yield_savings", "money_market", "cd", "all"])

    def test_one_row_per_run_bank_and_product(self):
        self.assertIn("unique (run_id, bank_name, product_type)", APPLY)

    def test_run_id_is_a_foreign_key(self):
        self.assertIn("run_id text not null references public.rate_radar_runs (run_id)", APPLY)

    def test_guard_refuses_a_second_apply_and_a_wrong_shape(self):
        self.assertIn("rr01 is already applied (review_flags or tier_note exists)", APPLY)
        self.assertIn("rr01 is already applied (public.rate_not_published exists)", APPLY)
        self.assertIn("rr01: public.rate_observations does not have the 18 columns", APPLY)

    def test_post_checks(self):
        self.assertIn("20 columns after the change", APPLY)
        self.assertIn("row security is not on", APPLY)
        self.assertIn("anon or authenticated can read", APPLY)


class RollbackTests(unittest.TestCase):
    def test_drops_exactly_the_added_objects(self):
        self.assertIn("drop table if exists public.rate_not_published;", ROLLBACK)
        self.assertIn("drop column if exists review_flags;", ROLLBACK)
        self.assertIn("drop column if exists tier_note;", ROLLBACK)
        self.assertEqual(len(re.findall(r"\bdrop\b", ROLLBACK.lower())), 3)

    def test_refuses_when_there_is_nothing_to_roll_back_and_checks_the_result(self):
        self.assertIn("nothing to roll back", ROLLBACK)
        self.assertIn("does not have 18 columns after the rollback", ROLLBACK)

    def test_one_transaction(self):
        self.assertTrue(ROLLBACK.strip().lower().startswith("begin;"))
        self.assertTrue(ROLLBACK.strip().lower().endswith("commit;"))


class VerifyTests(unittest.TestCase):
    def test_is_read_only(self):
        self.assertIsNone(re.search(r"(?im)^\s*(insert|update|delete|drop|alter|create|grant|revoke|truncate)", VERIFY))
        for stmt in [s.strip() for s in VERIFY.split(";") if s.strip()]:
            self.assertTrue(stmt.lower().startswith("select"), stmt[:40])

    def test_checks_the_security_of_the_new_table(self):
        self.assertIn("relrowsecurity", VERIFY)
        self.assertIn("has_table_privilege('anon'", VERIFY)


if __name__ == "__main__":
    unittest.main()
