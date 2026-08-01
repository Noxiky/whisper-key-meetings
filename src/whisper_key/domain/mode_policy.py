from __future__ import annotations

from dataclasses import dataclass

from .session import SessionMode


@dataclass(frozen=True)
class ModePolicy:
    mode: SessionMode
    capture_microphone: bool
    capture_system_audio: bool
    durable: bool
    screenshots: bool
    default_marker: str
    projection: str


MODE_POLICIES = {
    SessionMode.DICTATION: ModePolicy(SessionMode.DICTATION, True, False, False, False, "important", "dictation"),
    SessionMode.MEETING: ModePolicy(SessionMode.MEETING, True, True, True, True, "action", "meeting"),
    SessionMode.LEARNING: ModePolicy(
        SessionMode.LEARNING,
        True,
        True,
        True,
        True,
        "not_understood",
        "learning",
    ),
    SessionMode.READING: ModePolicy(SessionMode.READING, True, False, True, True, "quote", "reading"),
    SessionMode.IDEA: ModePolicy(SessionMode.IDEA, True, False, True, True, "important", "idea"),
}


def policy_for(mode: SessionMode | str) -> ModePolicy:
    return MODE_POLICIES[SessionMode(mode)]
