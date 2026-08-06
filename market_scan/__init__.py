"""Movers — market-wide volume/price spike scanning with per-stock AI notes.

Layered so each concern is testable on its own:

    domain      pure data + the filter rule
    universe    exchange symbol lists, read from committed CSV exports
    feed        Yahoo daily bars
    session     which bar is the last *completed* session
    detector    the spike test (pure)
    scanner     runs a universe through feed + detector, isolating failures
    store       runs persisted on the volume
    enrich      best-effort sector/market cap for hits
    analyst     the per-stock note from DeepSeek V4 Flash
    service     orchestration, single-flight, live progress

Nothing reaches across a boundary except through the constructor arguments
above, so the whole pipeline can be driven by stubs in tests.
"""
