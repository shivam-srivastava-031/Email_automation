"""`python manage.py runserver` on port 5000 by default (pass a port to override).

Extends the staticfiles runserver so CSS is still served in development.
"mailer" is listed before "django.contrib.staticfiles" in INSTALLED_APPS so
this command wins.
"""
from django.contrib.staticfiles.management.commands.runserver import Command as StaticfilesRunserver


class Command(StaticfilesRunserver):
    default_port = "5000"
