"""Layer 2 - v2 engine cores.

Each v2 engine is a sibling of its v1 counterpart, calling the foundation
brain instead of bespoke statistics:

- ``e1_v2``   single-name earnings IC (cross-ticker analogues + conformal)
- ``e15_v2``  earnings IC scenario (drops same-ticker constraint)
- ``e2_v2``   SPX weekly IC (path generator instead of bootstrap MC)
- ``e14_v2``  SPX IC scenario (contrastive embedder instead of weighted-L2 kNN)
- ``mi_v2``   market intelligence dashboard (learned regime + UMAP projection)
"""
