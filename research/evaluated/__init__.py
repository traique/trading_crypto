"""The evaluation record: frozen copies of what the independent evaluator ran and decided (do not edit).

- ``trend_majors.py``: byte-identical to the candidate source the evaluator hashed
  before the TEST run. It exists so the live strategy
  (``cryptotrader.strategies.BtcSmaEnsemble``) can be compared fill for fill with
  what was evaluated: ``python -m research run btc_sma_ens_eval`` runs it,
  ``python -m research run btc_sma_ens`` the live one, ``python -m research.parity``
  both (``research/RESULTS.md`` has the result on the real data).
- ``protocol.md`` (+ ``protocol.sha256``, ``timeline.log``): the selection rule,
  frozen before the single TEST run.
- ``winner.json``: the decision, the exact rules and the metrics per split.

``EVALUATED_SHA256`` pins them; ``tests/test_strategy_sma_ensemble.py`` checks it.
"""

EVALUATED_SHA256 = {
    "trend_majors.py": "e10e54919dcccbb217d6b4cc41ab0d126d9d899bc196bf27642e25fa6ddc0a68",
    "protocol.md": "250886b1fa2554471aa912bc4c0226eeefedb8ae92290e7b09eb532bc6985078",
}
