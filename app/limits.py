"""Concurrency guards for CPU-heavy work (Pillow rendering, background removal).

Every ``asyncio.to_thread`` job competes for the same GIL and the same few hundred MB of RAM. Letting 40 of them run
at once (the default thread-pool size) makes *every* request slower and can OOM a small instance. A short queue in
front of them keeps latency predictable: N jobs run, the rest wait their turn.
"""
import asyncio
from contextlib import asynccontextmanager

from . import config

_cpu = asyncio.Semaphore(config.CPU_CONCURRENCY)


@asynccontextmanager
async def cpu_slot():
    async with _cpu:
        yield


async def run_cpu(fn, *args):
    async with _cpu:
        return await asyncio.to_thread(fn, *args)
