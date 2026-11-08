"""Tests must never inherit an operator's production DB/model credentials.

The optional PostgreSQL suite uses a separate, explicitly provided TEST admin
DSN and creates a disposable database. Source fixtures are read-only inputs.
"""
import os
import tempfile

_SANDBOX=tempfile.TemporaryDirectory(prefix='ontoflow-pytest-')
for key in ('DATABASE_URL','API_TOKEN','MODEL_ADMIN_TOKEN','MODEL_SETTINGS_KEY',
            'MODEL_ALLOWED_HOSTS','OPENMIND_BASE_URL','OPENMIND_ACCESS_TOKEN','OPENMIND_ADMIN_TOKEN',
            'OPENMIND_USERNAME','OPENMIND_PASSWORD','OPENMIND_AUTH_MODE',
            'OPENMIND_ALLOWED_HOSTS','OPENMIND_ALLOW_INSECURE_HTTP',
            'LLM_BASE_URL','LLM_API_KEY','LLM_MODEL','ASYNC_PARSE','STRUCTURED_EXTRACTION'):
    os.environ.pop(key,None)
os.environ['DATA_DIR']=_SANDBOX.name
os.environ['APP_ENV']='development'
os.environ['ONTOFLOW_DEMO_MODE']='false'
