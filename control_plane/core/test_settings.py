from .settings import *

# Override database for testing
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    },
    "oracle": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    },
}

SECRET_KEY = "django-insecure-test-secret-key"


class TestRouter:
    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if db == "oracle":
            return app_label in ("extracted_data", "contenttypes", "auth")
        return True


DATABASE_ROUTERS = ["core.test_settings.TestRouter"]

