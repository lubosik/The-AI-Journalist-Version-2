"""
agents/feedback_agent.py

Runs before the main intelligence agent on every incoming message.
Decides if a message is feedback/instruction, stores it, and replies.
If not feedback, returns immediately so the main agent can handle it.
"""

import logging

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


async def process_message_for_feedback(message: str) -> dict:
    """
    Check if a message is feedback about the newsletter.

    Returns:
      {"is_feedback": True,  "reply": str}  — if feedback was identified and stored
      {"is_feedback": False}                 — if not feedback; main agent should handle
    """
    from memory.feedback import is_feedback, store_feedback

    try:
        result = await is_feedback(message)

        if not result.get("is_feedback"):
            return {"is_feedback": False}

        category = result.get("category", "other")
        instruction = result.get("instruction", message)

        feedback_id = await store_feedback(
            raw_message=message,
            category=category,
            instruction=instruction,
        )

        if feedback_id:
            logger.info(f"Feedback stored: [{category}] {instruction[:80]} (id={feedback_id})")
            reply = (
                f"Got it. I've logged that instruction under '{category}'.\n\n"
                f"Instruction: {instruction}\n\n"
                f"It will apply from the next newsletter onwards. "
                f"You now have active writing instructions on file. Use /feedback to review them all."
            )
        else:
            reply = "I understood that as a writing instruction but had trouble saving it. Try again."

        return {"is_feedback": True, "reply": reply}

    except Exception as e:
        logger.error(f"process_message_for_feedback error: {e}")
        return {"is_feedback": False}
