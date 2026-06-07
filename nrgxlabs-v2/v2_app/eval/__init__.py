"""Layer 4 - Eval and continuous learning.

Modules:

- ``counterfactual_journal``  v1-vs-v2 disagreement dataset (Redis stream consumer)
- ``conformal_coverage``      live tracking of predicted-vs-realized coverage
- ``adversarial_suite``       synthetic shock generators + CI gate for v2 deploys
- ``nightly_loop``            replay yesterday, update kNN distance / ranker / prompts
"""
