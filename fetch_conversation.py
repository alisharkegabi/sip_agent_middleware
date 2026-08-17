"""
Manual inspection tool for ElevenLabs post-call data.

Prints the complete conversation record ElevenLabs holds for a call --
evaluation_criteria_results, data_collection_results (e.g. PaymentDate),
transcript_summary, metadata, transcript. This is the same record the
service now fetches automatically after every call (conversation_analysis.py);
this script exists so you can look at one on demand without re-running a call.

    # a specific conversation
    python fetch_conversation.py conv_01k...

    # the most recent conversation for AGENT_ID from .env
    python fetch_conversation.py --latest

    # just the analysis block the .NET client receives, nothing else
    python fetch_conversation.py --latest --summary

Output contains real customer speech unless --summary is used. Redirect to a
file rather than pasting it around:

    python fetch_conversation.py --latest > logs/conversations/last.json
"""
from __future__ import annotations

import argparse
import json
import sys

from elevenlabs.client import ElevenLabs

from config import settings
from conversation_analysis import _model_to_dict, extract_analysis


def _latest_conversation_id(client: ElevenLabs, agent_id: str) -> str | None:
    page = _model_to_dict(client.conversational_ai.conversations.list(agent_id=agent_id, page_size=1))
    conversations = page.get("conversations") or []
    if not conversations:
        return None
    return conversations[0].get("conversation_id")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("conversation_id", nargs="?", help="conversation id to fetch")
    parser.add_argument("--latest", action="store_true", help="use the most recent conversation for AGENT_ID")
    parser.add_argument("--summary", action="store_true", help="print only the extracted analysis block")
    args = parser.parse_args()

    if not settings.elevenlabs_api_key:
        print("ELEVENLABS_API_KEY is not set", file=sys.stderr)
        return 2

    client = ElevenLabs(api_key=settings.elevenlabs_api_key)

    conversation_id = args.conversation_id
    if args.latest or not conversation_id:
        if not settings.agent_id:
            print("AGENT_ID is not set; pass a conversation id explicitly", file=sys.stderr)
            return 2
        conversation_id = _latest_conversation_id(client, settings.agent_id)
        if not conversation_id:
            print(f"no conversations found for agent {settings.agent_id}", file=sys.stderr)
            return 1
        print(f"# latest conversation for {settings.agent_id}: {conversation_id}", file=sys.stderr)

    raw = _model_to_dict(client.conversational_ai.conversations.get(conversation_id))
    status = raw.get("status")
    if status not in ("done", "failed"):
        print(
            f"# status={status} -- ElevenLabs has not finished analysing this "
            "conversation yet; the analysis block will be empty. Retry in a few seconds.",
            file=sys.stderr,
        )

    payload = extract_analysis(raw) if args.summary else raw
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
