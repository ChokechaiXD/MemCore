"""MemCore native Hermes memory-provider package.

Activation is exclusive through ``memory.provider: memcore``. The native
adapter owns Hermes lifecycle ingress/prefetch/tools; canonical governance
remains in the MemCore engine and Dashboard remains a separate UI extension.
"""
from .native_provider import MemCoreMemoryProvider


def _host_llm_facade(ctx):
    """Resolve Hermes' host-owned plugin LLM facade across plugin loaders.

    General plugins expose ``ctx.llm`` directly. Exclusive memory providers are
    currently loaded through Hermes' collector facade, which owns a lazy real
    PluginContext under ``_plugin_context()``. Use that host context when present
    rather than constructing provider SDK clients or reading credentials here.
    Older Hermes builds simply return None and MemCore keeps manual review.
    """
    try:
        llm = getattr(ctx, 'llm')
        if callable(getattr(llm, 'complete_structured', None)):
            return llm
    except Exception:
        pass
    try:
        factory = getattr(ctx, '_plugin_context', None)
        if callable(factory):
            llm = factory().llm
            if callable(getattr(llm, 'complete_structured', None)):
                return llm
    except Exception:
        pass
    return None


def register(ctx) -> None:
    ctx.register_memory_provider(MemCoreMemoryProvider(plugin_llm=_host_llm_facade(ctx)))
