#!/usr/bin/env bash
set -euxo pipefail

# مهم: لا نستخدم apt-get هنا. Render يثبّت محتويات apt.txt تلقائيًا.
python -m pip install --upgrade pip
pip install -r requirements.txt
