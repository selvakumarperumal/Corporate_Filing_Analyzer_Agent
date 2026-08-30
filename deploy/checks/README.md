# Deployment checks

Three scripts that answer "is this deployment actually working", in the order
you would ask it. They talk to a running deployment — they are not a unit test
suite, and there isn't one.

| Script | Answers | Needs |
| --- | --- | --- |
| `smoke.py` | does the whole analyst path work at all? | one endpoint |
| `split.py` | do two instances agree about tokens, filings and the ledger? | two *different* endpoints |
| `coordination.py` | do the locks, leases and the sweep behave? | Postgres and Redis directly |

Run them with the backend's own environment, which already has `aiohttp` and
`python-socketio`:

```bash
backend/Analyzer/.venv/bin/python deploy/checks/smoke.py --base http://cfa.local
```

Each exits 0 when everything passed and 1 when something did not, so they drop
into CI unchanged. Every check prints what it looked at, because the useful
output of a deployment check is usually the detail beside the failure rather
than the failure.

They create accounts and dossiers named after themselves (`smoke-…`,
`split-…`, `coord-…`) and clean up the dossiers. Point them at a development
deployment, not one with real work in it.

Full instructions, and what to do when one fails, are in
[`docs/DEPLOYMENT.md`](../../docs/DEPLOYMENT.md#the-checks).
