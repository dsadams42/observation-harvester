# Repository Rules

- Keep external services behind typed interfaces in `ports.py`.
- Never put secrets, API keys, tokens, or generated `.env` files in source code.
- Tests must not make unapproved network calls or require `OPENAI_API_KEY`.
- Run pytest, Ruff, and mypy after material changes.
- Preserve exact source quotations when creating or validating evidence.
- Do not let model output bypass deterministic validation.
- Prefer Codex-operated JSON run artifacts plus local validation for subscription-backed usage.
- Keep direct OpenAI API or Agents SDK execution behind explicit optional commands and extras.
- Keep this proof of concept small; avoid scheduling, scraping, databases, and UI work here.
