"""PostgreSQL advisory-lock integration tests for comfort-model retraining."""

from __future__ import annotations

import pytest

from packages.ml.comfort_model import PostgresTrainingLock


@pytest.mark.asyncio(loop_scope="session")
async def test_training_lock_contends_reacquires_and_releases_after_invalidation(setup_database):
    first_lock = PostgresTrainingLock()
    second_lock = PostgresTrainingLock()
    first_lease = await first_lock.acquire()
    assert first_lease is not None
    first_committed = False

    try:
        assert await second_lock.acquire() is None
        assert second_lock.reason == "training_in_progress"

        await first_lease.commit()
        first_committed = True
        await first_lease.close()

        second_lease = await second_lock.acquire()
        assert second_lease is not None
        try:
            await second_lease.invalidate()
        finally:
            await second_lease.close()

        recovered_lease = await PostgresTrainingLock().acquire()
        assert recovered_lease is not None
        try:
            await recovered_lease.commit()
        finally:
            await recovered_lease.close()
    finally:
        # A failed assertion before commit must not leave the E2E test session locked.
        if not first_committed:
            try:
                await first_lease.rollback()
            except Exception:
                pass
        await first_lease.close()
