# SPDX-License-Identifier: Apache-2.0
"""OWASP LLM Top 10 — custom pytest fixtures.

These tests assert the lm-chat-side invariants for the OWASP LLM Top 10
risks that aren't auto-covered by garak probes.

Each test file targets one LLM-Top-10 ID:

- ``test_llm02_output_sanitization.py`` — Insecure Output Handling.
- ``test_llm06_memory_isolation.py``    — Sensitive info disclosure /
                                          cross-user memory leak.
- ``test_llm07_mcp_preset_injection.py`` — Insecure plugin design (MCP
                                          integrations preset name).
- ``test_llm08_slashcmd_mode_lock.py``  — Excessive agency (slash-command
                                          preset isolation).

The fixtures roll up via pytest's native JUnit XML output; the L7 Makefile
recipe captures that XML into ``target/gates/L7-llm-top10.xml`` and the
combiner merges it with the garak sub-XMLs.
"""
