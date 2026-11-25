#!/bin/sh
cd "$(dirname "$0")" && exec .venv/bin/python manage.py runserver 0.0.0.0:${PORT:-8030}
