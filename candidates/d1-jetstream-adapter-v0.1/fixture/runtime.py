"""Run-scoped handles set by run_jetstream_tests.py for the in-process integration suite."""
BROKER = None          # fixture.broker.BrokerProcess (owned by the harness)
WORK = None            # Path of the private fixture work directory
GATEWAY_ROOT = None
PROJECT_RETURN_ROOT = None
PYTHON = None          # interpreter used for child adapter processes
CANDIDATE = None
