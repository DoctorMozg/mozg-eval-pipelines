"""The LLM judge; every PydanticAI import stays inside this package so upgrades touch one place."""

import pydantic_ai

# Its first run prints a banner to the terminal, which would land in the CLI's output.
pydantic_ai.BANNER_ENABLED = False
