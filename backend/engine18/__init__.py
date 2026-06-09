"""Engine 18 — Earnings Drift (PEAD).

Scans fresh earnings reports for large, high-quality beats and surfaces
long-equity drift candidates with validated sizing tiers.

Spec is anchored to the Edge Bake-Off (backend/research/, live run 2026-06-09):
  * OOS 2023+: +0.65%/trade after costs, t=2.7, n=843
  * Large beats (surprise >= +20%) carry the edge: +1.05%/trade
  * Misses LOSE money when shorted -> the engine is long-only by design
  * Top transcript-quality quintile: +1.45%/trade @ 60% hit rate

Division of labor (house rule): the LLM grades transcript quality only.
Candidate filtering, surprise buckets, quintile bucketing, and sizing tiers
are deterministic and reproducible from stored evidence.
"""
