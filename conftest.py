import sys
import os

# To avoid namespace collision with the local 'dagster' directory,
# we move the current directory to the end of sys.path so that
# installed packages in site-packages (like the real 'dagster' library)
# are resolved first.
for path in list(sys.path):
    if path in ('', '.', os.getcwd()):
        sys.path.remove(path)
        sys.path.append(path)

import pytest

def pytest_configure(config):
    """
    pytest hook to configure Django settings and dynamically toggle
    unmanaged models (managed = False) to managed = True so that
    pytest-django's test DB creation creates tables for them.
    """
    try:
        import django
        django.setup()

        from django.apps import apps
        for model in apps.get_models():
            if not model._meta.managed:
                model._meta.managed = True
    except (ImportError, Exception):
        pass
