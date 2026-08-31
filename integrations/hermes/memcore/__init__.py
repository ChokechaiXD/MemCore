"""MemCore native Hermes memory-provider package.

Activation is exclusive through ``memory.provider: memcore``. The native
adapter owns Hermes lifecycle ingress/prefetch/tools; canonical governance
remains in the MemCore engine and Dashboard remains a separate UI extension.
"""
from .native_provider import MemCoreMemoryProvider


def register(ctx) -> None:
    ctx.register_memory_provider(MemCoreMemoryProvider())
