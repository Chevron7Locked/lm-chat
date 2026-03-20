"""
Subprocess coverage startup hook.
Python automatically imports sitecustomize.py at process startup for any
process that has the tests/ directory on its PYTHONPATH. The app_server and
app_server_auth fixtures set COVERAGE_PROCESS_START + include tests/ in
PYTHONPATH, so server.py subprocess coverage is captured automatically.
"""
try:
    import coverage
    coverage.process_startup()
except ImportError:
    pass
