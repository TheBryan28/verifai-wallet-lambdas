"""Progress updates that the app shows while an analysis runs.

Each Lambda records the stage it is working on in the analysis item. Updates are
best effort: a failed update is logged and never fails the analysis.
"""
import logging
import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# Stage codes, in order. The app maps each one to a message and a step of its timeline.
EXTRACTING_TEXT = "EXTRACTING_TEXT"
PROTECTING_PERSONAL_DATA = "PROTECTING_PERSONAL_DATA"
IDENTIFYING_CLAUSES = "IDENTIFYING_CLAUSES"
SEARCHING_LAW = "SEARCHING_LAW"
ANALYZING_WITH_AI = "ANALYZING_WITH_AI"
PREPARING_REPORT = "PREPARING_REPORT"

ANALYSES_TABLE = os.environ.get("ANALYSES_TABLE")

logger = logging.getLogger(__name__)
_dynamodb = boto3.client("dynamodb") if ANALYSES_TABLE else None


def set_stage(analysis_id, stage, **counters):
    """Records the current stage and optional counters (pages, clause_count, legal_sources).

    The update only applies while the analysis is PROCESSING, so a late update can never
    overwrite COMPLETED or FAILED.
    """
    if _dynamodb is None:
        return
    assignments = ["#stage = :stage", "stage_updated_at = :now"]
    values = {
        ":stage": {"S": stage},
        ":now": {"S": datetime.now(timezone.utc).isoformat()},
        ":processing": {"S": "PROCESSING"},
    }
    for name, value in counters.items():
        assignments.append(f"{name} = :{name}")
        values[f":{name}"] = {"N": str(int(value))}
    try:
        _dynamodb.update_item(
            TableName=ANALYSES_TABLE,
            Key={"id": {"S": analysis_id}},
            UpdateExpression="SET " + ", ".join(assignments),
            ConditionExpression="#status = :processing",
            ExpressionAttributeNames={"#stage": "stage", "#status": "status"},
            ExpressionAttributeValues=values,
        )
    except (ClientError, BotoCoreError) as e:
        logger.warning("Progress update to %s skipped: %s", stage, e)
