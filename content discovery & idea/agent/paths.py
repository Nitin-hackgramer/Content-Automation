"""Where things live on disk. Absolute, so nothing depends on the directory the process was started from."""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")  # approved_tweets.md, linkedin_post_template.md, last_run.json
# This project's own .env only. A bare load_dotenv() searches parent folders and never overrides
# variables already set, so it could pick up another project's keys. Every module loads this path.
ENV_FILE = os.path.join(ROOT, ".env")
