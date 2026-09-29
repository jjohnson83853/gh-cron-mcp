# Development tests

Install runtime and test dependencies, then run the complete offline suite from
the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The default pytest configuration measures the complete `app` package and
fails below 90% line coverage. Tests use temporary storage, local fixture
repositories, and mocks rather than live GitHub, Docker, or scheduler services.
