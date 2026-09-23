# -*- coding: utf-8 -*-
"""
jev_classifier.py - Jev AI (System One) Integration for Kuru Record
- Evaluates live streamer quality & recording priority in < 300ms
- Uses TypeSafe AI System One structured non-generative decision engine
- Safe fallback: If API fails, returns default allow to prevent missing recordings
"""

import os
import json
import urllib.request
import urllib.error

JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"

def _load_api_key():
    key = os.getenv("TYPESAFE_API_KEY")
    if key:
        return key.strip()
    
    # Check local .env file
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_file):
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("TYPESAFE_API_KEY="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    return None

def evaluate_live_streamer(username: str, title: str = "", viewer_count: int = 0) -> dict:
    """
    Evaluates whether a live streamer should be prioritized for recording.
    Returns: { "should_record": bool, "priority": "high"|"normal"|"low", "confidence": float }
    """
    api_key = _load_api_key()
    if not api_key:
        return {"should_record": True, "priority": "normal", "confidence": 1.0, "reason": "no_api_key"}

    state_text = f"TikTok live streamer @{username}. Title: '{title}'. Viewers: {viewer_count}."
    payload = {
        "model": JEV_MODEL,
        "state": state_text,
        "questions": {
            "should_record": {
                "type": "noul",
                "instructions": "Should this live broadcast be recorded based on content value and viewer activity?"
            },
            "priority": {
                "type": "choice",
                "instructions": "What priority tier should be assigned to this stream?",
                "criteria": {
                    "high": "High viewer activity, special event, or VIP gaming/creative stream",
                    "normal": "Standard regular live stream",
                    "low": "Idle stream, black screen, or repetitive static content"
                }
            }
        }
    }

    try:
        req = urllib.request.Request(
            JEV_ENDPOINT,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "KuruRecord/1.0"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            if response.status == 200:
                res_data = json.loads(response.read().decode("utf-8"))
                answers = res_data.get("answers", {})
                rec_noul = answers.get("should_record", {}).get("noul", 0.7)
                prio_choice = answers.get("priority", {}).get("choice", "normal")
                prio_conf = answers.get("priority", {}).get("confidence", 0.9)
                return {
                    "should_record": rec_noul >= 0.4,
                    "priority": prio_choice,
                    "confidence": prio_conf,
                    "model": res_data.get("model", JEV_MODEL)
                }
    except Exception as e:
        # ponytail: Fail-open fallback ensures live streams are never missed due to AI network drops
        return {"should_record": True, "priority": "normal", "confidence": 1.0, "error": str(e)}

    return {"should_record": True, "priority": "normal", "confidence": 1.0}

if __name__ == "__main__":
    print("[*] Testing Jev AI System One Streamer Evaluation...")
    result = evaluate_live_streamer("islizanx", title="Valorant Ranked Radiant Grind", viewer_count=1800)
    print("Result:", json.dumps(result, indent=2))
