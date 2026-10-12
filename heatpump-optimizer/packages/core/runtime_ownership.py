"""Cancellation-resistant teardown retaining ownership until workers have stopped."""

from __future__ import annotations

import asyncio


async def finish_owned_cleanup(cleanup):
    """Repeated cancellation must not release resources while teardown is still running."""
    task = asyncio.create_task(cleanup())
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def run_owned_thread(function, *args, **kwargs):
    """A cancelled await retains a running thread until its underlying work completes."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            task.exception()
        raise


async def close_service_resources(*, scheduler=None, maintenance=None, wrapper=None):
    from packages.core.log_sink import shutdown_log_sink

    if scheduler is not None:
        await scheduler.close()
    if maintenance is not None:
        await maintenance.close()
    # If the bounded log drain retains an unsettled writer, this raises rather
    # than disposing a resource still owned by that writer.
    await shutdown_log_sink()
    if wrapper is not None:
        await wrapper.stop()
