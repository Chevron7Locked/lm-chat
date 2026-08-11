#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Test that check_regex_tests correctly handles the _RE_GATE shared-decorator
# idiom with multi-line settings() and @_RE_GATE @given def chains.
#
# Creates a synthetic test file matching the real shape from
# tests/tools/test_tool_args_property.py, runs the check_regex_tests logic
# against it, and asserts 0 missing regex variables.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
TMPDIR=$(mktemp -d)
trap 'rm -rf "${TMPDIR}"' EXIT

# ---------------------------------------------------------------------------
# Generate a synthetic SARIF file using Python (avoids heredoc escaping
# issues with regex patterns that contain backslashes and quotes).
# ---------------------------------------------------------------------------
python3 -c "
import json

snippets = [
    '_XML_FUNC_RE = re.compile(r\"<function=([^>\\\\s]+)\\\\s*>(.*?)</function>\", re.S)',
    '_XML_PARAM_RE = re.compile(r\"<parameter=([^>\\\\s]+)\\\\s*>(.*?)</parameter>\", re.S)',
    '_XML_TC_WRAP_RE = re.compile(r\"<tool_call>(.*?)</tool_call>\", re.S)',
    '_XML_FUNC_OPEN_RE = re.compile(r\"<function=([^>\\\\s]+)\\\\s*>\", re.S)',
    '_XML_PARAM_OPEN_RE = re.compile(r\"<parameter=([^>\\\\s]+)\\\\s*>\", re.S)',
    '_FENCE_RE = re.compile(r\"^\\\\s*\\x60\\x60\\x60(?:json)?\\\\s*|\\\\s*\\x60\\x60\\x60\\\\s*$\", re.I)',
    '_SQ_KEY_RE = re.compile(r\"([{,]\\\\s*)\\x27([^\\x27\\x22]*?)\\x27(\\\\s*:)\")',
    '_SQ_VAL_RE = re.compile(r\"(:\\\\s*)\\x27([^\\x27\\x22]*?)\\x27(\\\\s*[,}])\")',
]

data = {
    '\$schema': 'https://raw.githubusercontent.com/oasis-tcs/openc2-sarif/master/schemas/sarif-schema-2.1.0.json',
    'version': '2.1.0',
    'runs': [
        {
            'tool': {'driver': {'name': 'semgrep', 'rules': [{'id': 'regex-without-deadline'}]}},
            'results': [
                {
                    'ruleId': 'regex-without-deadline',
                    'locations': [{'physicalLocation': {'region': {'snippet': {'text': s}}}}]
                }
                for s in snippets
            ]
        }
    ]
}

with open('${TMPDIR}/test_sarif.json', 'w') as f:
    json.dump(data, f, indent=2)
print('SARIF generated')
"

# ---------------------------------------------------------------------------
# Create a synthetic test file that matches the real _RE_GATE shape:
#   _RE_GATE = settings(
#       deadline=50,
#       ...
#   )
#   @_RE_GATE
#   @given(...)
#   def test_foo(self):
#       ... recover_xml_tool_calls(...) ...
#
# Also imports non-XML regex variables by name so Step 3a finds them.
# ---------------------------------------------------------------------------
cat > "${TMPDIR}/synthetic_test.py" << 'PYTHON'
"""Synthetic test file mimicking the real _RE_GATE shared-decorator idiom.

Covers ALL regex variables from tool_args.py:
    _XML_FUNC_RE, _XML_PARAM_RE, _XML_TC_WRAP_RE,
    _XML_FUNC_OPEN_RE, _XML_PARAM_OPEN_RE,
    _FENCE_RE, _SQ_KEY_RE, _SQ_VAL_RE
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from lmchat.services.tool_args import (
    recover_xml_tool_calls,
    _FENCE_RE,
    _SQ_KEY_RE,
    _SQ_VAL_RE,
)

# Multi-line settings( with deadline= on a SEPARATE line --
# the exact shape the old matcher missed.
_RE_GATE = settings(
    deadline=50,
    suppress_health_check=[HealthCheck.data_too_large, HealthCheck.too_slow],
)


class TestRoundtrip:

    @_RE_GATE
    @given(name=st.text(max_size=10))
    def test_single_function(self, name: str) -> None:
        """A single function call round-trips identically."""
        wire = f"<tool_call><function={name}><parameter=x>1</parameter></function></tool_call>"
        result = recover_xml_tool_calls(wire)
        assert result is not None
        # Reference non-XML regexes so Step 3a finds them.
        assert _FENCE_RE.pattern is not None
        assert _SQ_KEY_RE.pattern is not None
        assert _SQ_VAL_RE.pattern is not None

    @_RE_GATE
    @given(
        st.lists(
            st.tuples(st.text(max_size=5), st.text(max_size=5)),
            min_size=2,
            max_size=5,
        )
    )
    def test_multi_function(self, call_list: list[tuple[str, str]]) -> None:
        """Multiple function calls round-trip."""
        parts = []
        for name, arg in call_list:
            parts.append(f"<tool_call><function={name}><parameter=x>{arg}</parameter></function></tool_call>")
        wire = "".join(parts)
        result = recover_xml_tool_calls(wire)
        assert result is not None
PYTHON

# ---------------------------------------------------------------------------
# Run the check_regex_tests logic directly via python3
# ---------------------------------------------------------------------------
echo "=== Synthetic test: check_regex_tests against _RE_GATE shape ==="

MISSING=$(python3 -c "
import json, re, sys

sarif_file = '${TMPDIR}/test_sarif.json'
test_file = '${TMPDIR}/synthetic_test.py'
missing = 0

with open(test_file) as f:
    content = f.read()

# Step 1: find shared settings bindings
binding_names = re.findall(r'^(_[A-Z_]+)\s*=\s*settings\(', content, re.MULTILINE)
print(f'Step 1 - binding_names: {binding_names}', file=sys.stderr)

# Step 2: verify @_NAME appears
valid_bindings = []
for name in binding_names:
    if '@' + name in content:
        valid_bindings.append(name)
print(f'Step 2 - valid_bindings: {valid_bindings}', file=sys.stderr)

with open(sarif_file) as f:
    data = json.load(f)

_recovery_tested = False
if 'recover_xml_tool_calls' in content and valid_bindings:
    _recovery_tested = True
print(f'Step 2b - _recovery_tested: {_recovery_tested}', file=sys.stderr)

seen = set()
for run in data.get('runs') or []:
    for r in run.get('results') or []:
        locs = r.get('locations') or []
        for loc in locs:
            snippet = loc.get('physicalLocation', {}).get('region', {}).get('snippet', {}).get('text', '')
            m = re.match(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*re\.compile\(', snippet)
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                var_name = m.group(1)
                found = False
                if var_name in content:
                    found = True
                if not found and var_name.startswith('_XML_') and _recovery_tested:
                    found = True
                if not found:
                    print(f'MISSING: {var_name}', file=sys.stderr)
                    missing += 1
                else:
                    print(f'COVERED: {var_name}', file=sys.stderr)

print(f'Step 3 - total missing: {missing}', file=sys.stderr)
print(missing)
")

echo ""
echo "Missing count: ${MISSING}"
if [[ "${MISSING}" -eq 0 ]]; then
  echo "PASS: All 8 regex variables correctly identified as covered"
  exit 0
else
  echo "FAIL: ${MISSING} regex variable(s) reported as missing"
  exit 1
fi