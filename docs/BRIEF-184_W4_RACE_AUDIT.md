BRIEF-184 W4 race witness audit — 2026-11-14

Files were inspected and edited only. No git commands, tests, mutants, database
operations, or child processes were run during this audit. The co-arch must run
the baseline and mutations below; this document does not claim passing results.

The reported 16 cases match the seven existing `test_tester_w4_*` recovery/retry
groups exactly (1 + 3 + 3 + 2 + 1 + 5 + 1). These use `fresh_client` and
`session_factory`, starting at `tests/test_billing.py:1779`. Their database is
SQLite: `tests/conftest.py:23`, `tests/conftest.py:67`,
`tests/conftest.py:146`, `tests/conftest.py:156`. Their requests are sequential
awaits. Setting `B184_TEST_DATABASE_URL` does not change those fixtures.

Before this edit the PostgreSQL cases were named
`test_tester_postgres_w4_races` and
`test_tester_postgres_w4_org_lock_and_pending_checkout`. Consequently,
`-k tester_w4` excluded them. The reported command/output was not supplied, so
selection is an inference from the exact count and names, not an observed run.
If all original W4 cases had actually been selected with a usable URL, there
would have been 21 cases, including five PostgreSQL cases, rather than 16.

The original PostgreSQL fixture did create real sessions and the four races did
use `asyncio.gather`; it was not a mocked database or one shared session. However,
the races only asserted distinct Session objects, had no critical-read barrier,
and left incidental customer UPDATEs in play. The separate lock witness accepted
a 250 ms timeout as evidence, which could also mean a slow request. Neither
observation proves the intended interleaving or actual server-side blocking.

The production guards are already present:

- One tester month per account is backed by `uq_tester_redemption_user` in
  `app/models/tester_redemption.py:32` and the migration at
  `alembic/versions/20261113_000008_tester_redemptions.py:30`. There is a separate
  org uniqueness constraint on the following line in both files. The reservation
  is committed before the external write at `app/services/billing_service.py:423`.
  Retained applied reservations are refused at `app/services/billing_service.py:145`.
- The User lock (`app/services/billing_service.py:140`) serializes the same user
  across different orgs. The org/subscription locks
  (`app/services/billing_service.py:76`, `app/services/billing_service.py:80`)
  serialize the same org and coordinate with paid checkout. These locks overlap
  for same-user/same-org requests; removing one does not remove all protection.
  Uniqueness still protects committed reservation cardinality without a lock,
  although an insert race can become a generic 502 through the SQLAlchemyError
  handler at `app/services/billing_service.py:429`. The locks are not globally
  redundant just because a quota assertion survives one lock mutation.
- Across two distinct accounts/orgs those row locks are disjoint. The local cap
  check at `app/services/billing_service.py:198` is a read, not a shared atomic
  counter. The intended shared cap guard is Stripe applying the promotion code
  in `app/services/stripe_gateway.py:90`. The SDK double explicitly models its
  atomic cap at `tests/test_billing.py:1386`. Reservation-specific idempotency
  (`app/services/billing_service.py:372`, `app/services/stripe_gateway.py:98`)
  deduplicates one reservation's retries; two accounts have different keys, so
  idempotency does not implement the shared cap.

No production guard was added. Stripe's atomic cap is an external contract
modeled by the SDK double (`tests/test_billing.py:1275`); these tests do not verify
live Stripe enforcement or replace the existing V0 Stripe feasibility gate.

The revised witnesses are all selected by `-k tester_w4`:

- `tests/test_billing.py:2124`: disposable PostgreSQL fixture retains the explicit
  skip message when the URL is unset, creates the reservation table using its
  migration, and pins a physical connection through each request's internal
  commits. The other tables still come from ORM metadata.
- `tests/test_billing.py:2178`: a separate observer connection requires
  `pg_blocking_pids` to identify the holder. Timeouts only fail the witness.
- `tests/test_billing.py:2208`: the first request holds its transaction after an
  empty reservation read; a second request on a different backend must block.
  Same-user/two-org and two-admin/one-org cases isolate each lock. Customers are
  preseeded to remove incidental UPDATE serialization.
- `tests/test_billing.py:2291`: competing INSERTs into the migrated reservation
  table isolate each uniqueness constraint from service locks. The second must
  wait for the first commit, then raise SQLSTATE 23505 for the exact constraint;
  only one row may persist. This catches removal of uniqueness even when the
  service locks would otherwise hide that mutation.
- `tests/test_billing.py:2335`: two distinct accounts reach a barrier after both
  have passed the last-slot read. Their backend PIDs and transaction IDs must
  differ, and both pending reservations must already be committed. Both then
  attempt the Stripe write with different keys. Require one external success,
  one `tester_code_exhausted` refusal, one applied reservation and one subscription.
  The stub now reflects the actual discount payload instead of inventing a
  discount when the writer omitted it.
- `tests/test_billing.py:2404`: the explicit org blocker also requires positive
  PostgreSQL blocking evidence and retains the pending-versus-paid-checkout check.
  Race tasks are cancelled and awaited on failure before schema cleanup.

Co-arch rerun, from `C:/localcode/uam-b184`, with the supplied PostgreSQL URL in
`B184_TEST_DATABASE_URL`: run `pytest -q tests/test_billing.py -k tester_w4`.
The expected selection is 23 cases: 16 recovery cases plus seven PostgreSQL
cases. This count is from source inspection, not a collection or execution run.
With the URL unset, the seven PostgreSQL cases must skip with the explicit
message; those skips are not acceptance.

Apply and restore each mutation separately, requiring an assertion failure:

1. Original mutant (a), `no-user-lock`: remove User `.with_for_update()` in
   `_tester_lock`; run `-k tester_w4_postgres_races`. The same-user/two-org
   contender reaches the empty-read gate instead of blocking.
2. Original mutant (b), `no-org-lock`: remove both `.with_for_update()` calls in
   `_lock`; run `-k "tester_w4_postgres_races or tester_w4_postgres_org_lock_and_pending_checkout"`.
   The two-admin/one-org contender or explicit org blocker fails the lock witness.
3. `no-user-unique`: remove user uniqueness from model and migration; run
   `-k "tester_w4_postgres_reservation_unique or tester_w6_setting_and_migration"`.
4. `no-org-unique`: separately remove org uniqueness from model and migration;
   run the same selector. These two mutations target the durable cardinality
   guards, not just locking behavior.
5. `omit-discount`: remove the promotion discount argument from the real gateway
   writer; run `-k tester_w4_postgres_last_slot`. Both contenders have already
   passed eligibility, so the test exposes bypass of the write-time cap guard.

Exact mutation recipes and commands are in `tests/fixtures/b184-mutants.json:169`.
