"""
product/connectors/google_docs_writer.py

Creates Google Docs in the CSM's Drive.
Used for QBRs, success plans, adoption assessments, and other
structured documents that CS Agent drafts and the CSM approves.

Human-in-the-loop: CS Agent drafts the document structure/content,
CSM reviews and approves, then this module creates it in Drive.
"""

from __future__ import annotations

import logging

from googleapiclient.discovery import build

logger = logging.getLogger("cs-agent.google_docs_writer")


def _load_credentials(csm_id: str):
    from product.auth_utils import load_google_credentials
    return load_google_credentials(csm_id)


def create_document(
    csm_id: str,
    title: str,
    body_text: str,
    folder_id: str | None = None,
) -> dict:
    """Create a Google Doc with the given title and body text.

    Args:
        csm_id: The CSM's user ID (for credential loading)
        title: Document title
        body_text: Plain text or simple formatted content for the document body
        folder_id: Optional Drive folder ID to place the document in

    Returns:
        {"document_id": str, "web_link": str, "title": str}
    """
    creds = _load_credentials(csm_id)
    docs_service = build("docs", "v1", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds)

    # 1. Create the document
    doc = docs_service.documents().create(body={"title": title}).execute()
    doc_id = doc["documentId"]

    # 2. Insert body text
    if body_text:
        requests = [
            {
                "insertText": {
                    "location": {"index": 1},
                    "text": body_text,
                }
            }
        ]
        docs_service.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests}
        ).execute()

    # 3. Move to folder if specified
    if folder_id:
        try:
            drive_service.files().update(
                fileId=doc_id,
                addParents=folder_id,
                fields="id, parents",
            ).execute()
        except Exception as e:
            logger.warning("Could not move doc %s to folder %s: %s", doc_id, folder_id, e)

    # 4. Get the web link
    file_meta = drive_service.files().get(
        fileId=doc_id, fields="webViewLink"
    ).execute()

    result = {
        "document_id": doc_id,
        "web_link": file_meta.get("webViewLink", ""),
        "title": title,
    }
    logger.info("Created Google Doc '%s' for %s: %s", title, csm_id, result["web_link"])
    return result


def create_qbr_document(
    csm_id: str,
    account_name: str,
    content: dict,
    folder_id: str | None = None,
) -> dict:
    """Create a QBR document from structured content.

    Args:
        csm_id: The CSM's user ID
        account_name: Customer account name
        content: Dict with keys like "executive_summary", "health_overview",
                 "key_metrics", "risks", "action_items", "next_steps"
        folder_id: Optional Drive folder ID

    Returns:
        {"document_id": str, "web_link": str, "title": str}
    """
    from datetime import date
    quarter = f"Q{(date.today().month - 1) // 3 + 1} {date.today().year}"
    title = f"QBR — {account_name} — {quarter}"

    sections = []
    sections.append(f"{title}\n{'=' * len(title)}\n")

    if content.get("executive_summary"):
        sections.append(f"Executive Summary\n{'-' * 17}\n{content['executive_summary']}\n")
    if content.get("health_overview"):
        sections.append(f"Health Overview\n{'-' * 15}\n{content['health_overview']}\n")
    if content.get("key_metrics"):
        sections.append(f"Key Metrics\n{'-' * 11}\n{content['key_metrics']}\n")
    if content.get("risks"):
        sections.append(f"Risks & Watch Items\n{'-' * 19}\n{content['risks']}\n")
    if content.get("action_items"):
        sections.append(f"Action Items\n{'-' * 12}\n{content['action_items']}\n")
    if content.get("next_steps"):
        sections.append(f"Next Steps\n{'-' * 10}\n{content['next_steps']}\n")

    body_text = "\n".join(sections)
    return create_document(csm_id, title, body_text, folder_id)


def create_success_plan_document(
    csm_id: str,
    account_name: str,
    content: dict,
    folder_id: str | None = None,
) -> dict:
    """Create a Success Plan document from structured content.

    Args:
        csm_id: The CSM's user ID
        account_name: Customer account name
        content: Dict with keys like "goals", "milestones", "timeline",
                 "stakeholders", "risks", "success_criteria"
        folder_id: Optional Drive folder ID

    Returns:
        {"document_id": str, "web_link": str, "title": str}
    """
    title = f"Success Plan — {account_name}"

    sections = []
    sections.append(f"{title}\n{'=' * len(title)}\n")

    if content.get("goals"):
        sections.append(f"Goals\n{'-' * 5}\n{content['goals']}\n")
    if content.get("milestones"):
        sections.append(f"Milestones\n{'-' * 10}\n{content['milestones']}\n")
    if content.get("timeline"):
        sections.append(f"Timeline\n{'-' * 8}\n{content['timeline']}\n")
    if content.get("stakeholders"):
        sections.append(f"Stakeholders\n{'-' * 12}\n{content['stakeholders']}\n")
    if content.get("risks"):
        sections.append(f"Risks\n{'-' * 5}\n{content['risks']}\n")
    if content.get("success_criteria"):
        sections.append(f"Success Criteria\n{'-' * 16}\n{content['success_criteria']}\n")

    body_text = "\n".join(sections)
    return create_document(csm_id, title, body_text, folder_id)
