#!/bin/bash
# Start cron daemon in background
cron

# Start gunicorn
exec gunicorn server:app \
    --bind 0.0.0.0:5050 \
    --workers 2 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
