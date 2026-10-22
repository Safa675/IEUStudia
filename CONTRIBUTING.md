# Contributing to IEUStudia

Thanks for wanting to help. A few things keep the project healthy.

## Before you start

- For anything bigger than a small fix, open an issue first so we can agree on the approach before you spend time
  on it.
- Never include real personal data in code, tests, issues or screenshots: no names, student numbers, passwords,
  cookies, lecture recordings or transcripts. The tests and the browser preview use made-up data.

## Setting up

You need Windows and [uv](https://docs.astral.sh/uv/).

```powershell
uv sync --group dev        # Python 3.12 environment in .venv
uv run studia --debug      # the app, with developer tools
uv run pytest -q           # all tests must pass
```

- The tests point `STUDIA_HOME` at a temporary folder (see `tests/conftest.py`), so they never touch your real data,
  IEULearn or the internet.
- The interface is plain HTML, CSS and JavaScript in `src/studia/web`. Open `index.html` in a browser to work on it
  with made-up data (`#dark`, `#signin`, `#setup`).
- Files use LF line endings (`.gitattributes` takes care of it). Match the style around your change: type hints in
  Python, small functions, comments only where they help.
- AI features must stay optional, run only when the user asks, and show what they will cost before they run.

## Pull requests

- One topic per pull request, with a short description of what changes for the user.
- Add or update tests for behaviour you change.
- Make sure `uv run pytest -q` passes.

## Security

Please report security problems privately (see [SECURITY.md](SECURITY.md)), not in public issues.
