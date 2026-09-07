#!/bin/sh
# Render's free tier has no pre-deploy-command step, and free instances are
# always single-replica, so running migrations here (ahead of gunicorn) is
# safe - it's the multi-replica race the Dockerfile's own CMD avoids, not
# this. See render.yaml's dockerCommand.
set -e
python manage.py migrate
exec gunicorn --config gunicorn.conf.py config.wsgi:application
