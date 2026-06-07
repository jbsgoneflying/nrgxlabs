"""Layer 1 - Foundation Brain.

Modules to be added in Phase 1:

- ``regime_encoder``      time-series transformer producing 64-d regime embedding
- ``contrastive_matcher`` cross-ticker / cross-time analogue retrieval
- ``cross_asset_gnn``     dynamic graph network replacing ``pc1_proxy_stress``
- ``path_generator``      conditional diffusion over intraday paths
- ``conformal``           split-conformal calibrator wrapping engine outputs

Each will expose a ``train`` CLI and a deterministic inference function so v2
engine cores can call them without holding an LLM in-process.
"""
