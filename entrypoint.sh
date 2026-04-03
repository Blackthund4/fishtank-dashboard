#!/bin/sh
chown -R dashboard:dashboard /app/data
exec gosu dashboard python server.py
