#!/usr/bin/env bash
set -euxo pipefail

# مهم: لا نستخدم apt-get داخل build. Render يثبّت apt.txt تلقائياً قبل البناء.
python -m pip install --upgrade pip
pip install -r requirements.txt
