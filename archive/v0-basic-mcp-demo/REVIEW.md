# Review of v0: basic MCP demo (archived)

What it was: two tiny MCP servers (`mathserver.py` over stdio, `weather.py` over
streamable HTTP) and `client.py`, which loads their tools through
`langchain-mcp-adapters` and hands them to a Groq-backed LangChain agent.

## What worked well
- Shows the core MCP idea: one client discovers tools from several servers on
  different transports (stdio and streamable HTTP) with no per-tool glue code.
- `@mcp.tool()` with type hints is the right entry point for FastMCP.

## Issues found
| # | Finding | Why it matters | Fixed in v1 by |
|---|---------|----------------|----------------|
| 1 | `.env` (holds `GROQ_API_KEY`) was untracked but **not in `.gitignore`** | One `git add .` would commit a secret | `.gitignore` now ignores `.env`; `.env.example` is committed instead |
| 2 | `pyproject.toml` has `dependencies = []`; real deps live in `requirements.txt` | Two sources of truth, unpinned | Single `pyproject.toml` |
| 3 | `os.environ["GROQ_API_KEY"] = os.getenv(...)` raises an unhelpful `TypeError` if unset | Poor failure mode | Typed settings (pydantic-settings) fail fast with a clear message |
| 4 | `client.py` runs `asyncio.run(main())` at import time | Can't import or test it | `if __name__ == "__main__"` guards, testable functions |
| 5 | Tools are fake (`get_weather` returns a constant), no error handling, no auth | Nothing to learn about real failure modes | Real DB-backed tools, typed errors, bearer auth |
| 6 | `mathserver.py` docstring is the placeholder `_summary_`; tool named `multiple` | The docstring is the description the LLM sees; bad docs = bad tool selection | Precise tool descriptions |
| 7 | Hard-coded `python` / relative path for the stdio server | Breaks outside the repo root | Config-driven URLs/commands |
| 8 | No tests, no Docker, no logging | Not runnable or verifiable by others | Whole v1 |

The old `.venv` was empty/broken and was not archived.
