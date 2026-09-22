#!/bin/zsh

set -e
cd -- "${0:A:h}"

if [[ ! -x .venv/bin/python ]]; then
  echo "Python environment not found. Follow the setup steps in README.md first."
  exit 1
fi

# Try to fetch the last 5 sessions from IDX directly. If IDX refuses, the
# pipeline still runs with whatever files are already in downloads/.
.venv/bin/python idx_download.py --days 5 || echo "IDX direct download unavailable - using existing files in downloads/"
.venv/bin/python idx_summary.py
.venv/bin/python pipeline.py
.venv/bin/python news.py

echo "Done. Open reports/dashboard.html to view the result."
