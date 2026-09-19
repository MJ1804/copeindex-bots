#!/bin/sh
set -e
echo "→ Installing deps"
pip install -r requirements.txt --quiet
echo "→ Running DB migrations"
python shared/migrate.py
echo "✓ Ready"