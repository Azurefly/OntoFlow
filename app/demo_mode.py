"""Explicit local demonstration mode.

Disables OntoFlow's own API/admin-key prompts, at-rest Fernet-key requirement,
and deployment host allowlists / forced HTTPS for OpenMind and model endpoints.
HTTP(S) URL syntax, remote credentials, TLS certificate verification, and native
ontology correctness/approval checks remain in effect. Remote OpenMind, LLM and
PostgreSQL still authenticate normally.
NEVER expose a demo-mode instance to untrusted networks.
"""
from __future__ import annotations
import os


def enabled() -> bool:
    return os.getenv('ONTOFLOW_DEMO_MODE', '').strip().lower() in ('1', 'true', 'yes', 'on')


def connection_policy() -> str:
    """Public diagnostic label; contains no addresses or credentials."""
    return 'demo_unrestricted' if enabled() else 'standard'
