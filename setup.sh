#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
if [[ ! -f .env ]]; then cp .env.example .env; fi
if [[ ! -f data/products.csv ]]; then cp data/products.template.csv data/products.csv; fi
printf '%s\n' 'Setup complete. Edit .env, then: .venv/bin/python main.py check' 'Offline demo: .venv/bin/python main.py demo'
