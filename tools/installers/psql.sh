#!/bin/bash
# PostgreSQL Client (psql, pg_dump, pg_restore, etc.)
set -e

sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends postgresql-client
sudo rm -rf /var/lib/apt/lists/*

psql --version | head -1
