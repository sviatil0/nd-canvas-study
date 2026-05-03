"""Cloud Function: disable billing when budget hits 100%.

Triggered by Pub/Sub messages from the billing budget. If
`costAmount >= budgetAmount`, removes billing account from project,
which immediately stops billable services.

Env:
    GCP_PROJECT      project id whose billing should be killed
"""
from __future__ import annotations

import base64
import json
import os

from googleapiclient import discovery


def disable_billing(event, context):  # noqa: ARG001
    payload = json.loads(base64.b64decode(event["data"]).decode("utf-8"))
    cost = float(payload.get("costAmount", 0))
    budget = float(payload.get("budgetAmount", 0))
    print(f"Budget alert: cost=${cost:.2f} budget=${budget:.2f}")
    if cost < budget:
        print("Under budget — no action.")
        return
    project_id = os.environ["GCP_PROJECT"]
    project_name = f"projects/{project_id}"
    print(f"OVER BUDGET — disabling billing on {project_name}")
    billing = discovery.build("cloudbilling", "v1", cache_discovery=False)
    body = {"billingAccountName": ""}
    res = billing.projects().updateBillingInfo(name=project_name, body=body).execute()
    print(f"Disabled: {res}")
