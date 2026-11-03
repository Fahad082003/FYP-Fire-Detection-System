#!/usr/bin/env python
import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        # Tests never read .env (it can hold real Twilio/SMTP credentials) and never use real keys.
        os.environ["SENTINEL_NO_DOTENV"] = "1"
        for name in [n for n in os.environ if n.upper().startswith("NOTIFICATIONS__")]:
            del os.environ[name]  # a real Twilio/SMTP/push setting in the OS environment must not reach a test either
        os.environ.setdefault("Jwt__SigningKey", "test-only-signing-key-0123456789abcdef")
        os.environ.setdefault("Device__ApiKey", "test-device-key")
        os.environ.setdefault("Device__SensorApiKey", "test-sensor-key")
        os.environ.setdefault("AiService__BaseUrl", "http://127.0.0.1:9")  # nothing listens there
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)
