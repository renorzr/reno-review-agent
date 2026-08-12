#!/usr/bin/env python3
"""Compatibility entry point for the Codex GitHub review agent.

New code should import :mod:`review_agent` or run ``python -m review_agent``.
"""

import subprocess
import time

from review_agent import *
from review_agent.app import record_issue_review, record_review, render_issue_review_comment, render_review_comment
from review_agent.core import _safe_network_operation
from review_agent.reviews import _invoke_codex_sweep, _run_issue_contract_review, _run_product_review

if __name__ == "__main__":
    raise SystemExit(main())
