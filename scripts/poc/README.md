# PoC acceptance scripts (archived)

The Step 5–7 acceptance drivers and launchers from the robo-agents proof of
concept, kept as a historical record ([MVP spec](../../docs/mvp-spec.md)
Appendix B). They are not maintained, and their tests in [`tests/`](tests/) are
outside the default `pytest` run and CI. To run those tests anyway:

```sh
uv run --locked pytest scripts/poc/tests
```

How the scripts were used is in [`docs/historical/poc/step5-acceptance.md`](../../docs/historical/poc/step5-acceptance.md)
and [`docs/historical/poc/step6-acceptance.md`](../../docs/historical/poc/step6-acceptance.md).
