# SPDX-License-Identifier: Apache-2.0
"""Failure-injection primitives for the P14 stress harness.

Two backends:

- ``toxiproxy_client`` — wraps the toxiproxy HTTP API (Shopify's TCP-layer
  fault injector).  Primary path when ``toxiproxy-cli`` is on PATH.
- ``httpx_fault_transport`` — httpx ``MockTransport`` fallback per PLAN
  §12.2.  Activated by ``STRESS_CHAOS_BACKEND=httpx-mock`` or chosen
  automatically when toxiproxy is unavailable.

``failure_modes`` enumerates the shared shapes (502, slow, drop-all,
mid-stream-disconnect, partial-response) used by both backends so the
scenarios are agnostic to which is active.
"""
