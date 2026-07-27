#!/bin/sh
# Fix ownership of the persistent data volume at runtime, then drop to appuser.
mkdir -p /app/data
chown -R appuser:appuser /app/data
exec setpriv --reuid=appuser --regid=appuser --clear-groups python main.py
