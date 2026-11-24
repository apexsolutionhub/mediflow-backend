"""Apex chat for a clinic server: the cloud side. The chat is cloud-only (docs/OFFLINE_SYNC_CONTRACT.md 6.3)."""
from tenants.feedback_views import post_message, thread_payload, unread_payload


def handle_feedback_unread(node, body, db="default"):
    return 200, unread_payload(node.clinic_tin)


def handle_feedback_thread(node, body, db="default"):
    return 200, thread_payload(node.clinic_tin)


def handle_feedback_send(node, body, db="default"):
    return post_message(
        node.clinic_tin, str(body.get("username") or "")[:150], body.get("body") or "", body.get("image_url") or ""
    )
