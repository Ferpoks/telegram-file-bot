#!/usr/bin/env bash
set -euxo pipefail

if [ -f apt.txt ]; then
  apt-get update
  xargs -a apt.txt apt-get install -y --no-install-recommends
  apt-get clean
fi

python -m pip install --upgrade pip
pip install -r requirements.txt
