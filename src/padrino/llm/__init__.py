"""Impure LLM adapter layer.

This package wraps provider-specific completion calls behind the
:class:`~padrino.llm.adapter.LlmAdapter` Protocol so the runner can dispatch
seat observations without knowing the underlying network or model.
"""
