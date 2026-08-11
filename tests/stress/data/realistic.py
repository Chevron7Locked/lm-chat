# SPDX-License-Identifier: Apache-2.0
"""Faker-backed realistic test data for the stress harness.

Used by 60% of scenarios.  Three public helpers:

- ``realistic_username()`` — like a plausible admin handle.
- ``realistic_chat_title()`` — short, plausible chat title.
- ``realistic_user_message()`` — one-paragraph user prompt.
- ``realistic_password()`` — 16-char password with mixed alphabet.

A pre-seeded ``Faker`` instance is exposed as ``FAKER`` for callers that
need other generators (sentences, paragraphs).  The seed is fixed in
the helper module so reruns produce identical content distributions
when LOCUST_RANDOM_SEED is set (Locust honors a stable seed for
reproducibility).
"""
from __future__ import annotations

import os
import random
import secrets
import string
from typing import Final

from faker import Faker

_SEED = int(os.environ.get("STRESS_FAKER_SEED", "1742"))

# A single Faker instance is fine; Locust uses gevent, so concurrency is
# cooperative (one greenlet at a time).  Seed once at import.
FAKER: Final[Faker] = Faker(["en_US"])
FAKER.seed_instance(_SEED)


_PROMPT_PATTERNS: Final[tuple[str, ...]] = (
    "Explain {topic} like I'm five.",
    "Compare and contrast {topic} and {topic2}.",
    "Write a short story about {topic}.",
    "What are the best practices for {topic}?",
    "Summarise the latest research on {topic}.",
    "Draft an email to a colleague about {topic}.",
    "Give me five tips for {topic}.",
    "Help me debug a problem with {topic}.",
    "Translate the following passage about {topic} into French.",
    "Generate a unit-test outline for a function that {topic}.",
)


def realistic_username() -> str:
    """Return a plausible handle, lower-case, 6–18 chars, underscores ok.

    Constrained to match what the auth router validates (alphanumeric +
    underscore, 3–64).
    """
    base = FAKER.user_name()
    # Faker may include hyphens; replace with underscore.
    base = base.replace("-", "_").replace(".", "_")[:18]
    if len(base) < 6:
        base = base + "_" + str(random.randint(100, 999))
    return base.lower()


def realistic_password() -> str:
    """Return a 16-char password with mixed letters, digits, and symbols.

    Uses ``secrets`` for cryptographic randomness — not Faker — so the
    stress-test credentials are not predictable from the seed alone.
    The credential pool is written to disk only inside ``target/stress/``,
    which is gitignored.
    """
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(16))


def realistic_chat_title() -> str:
    """Return a short, plausible chat title (one sentence, no trailing dot)."""
    raw = FAKER.sentence(nb_words=5, variable_nb_words=True)
    return raw.rstrip(".")


def realistic_user_message() -> str:
    """Return a plausible user prompt — one filled-in template line."""
    pattern = random.choice(_PROMPT_PATTERNS)
    topic = FAKER.bs() if random.random() < 0.5 else FAKER.catch_phrase()
    topic2 = FAKER.bs()
    return pattern.format(topic=topic, topic2=topic2)


def realistic_paragraph() -> str:
    """Return a one-paragraph blob suitable for use as a long-ish prompt."""
    return FAKER.paragraph(nb_sentences=5, variable_nb_sentences=True)
