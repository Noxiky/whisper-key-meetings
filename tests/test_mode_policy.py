from whisper_key.domain import policy_for


def test_each_mode_has_an_explicit_capture_and_projection_policy():
    meeting = policy_for("meeting")
    learning = policy_for("learning")
    reading = policy_for("reading")
    idea = policy_for("idea")
    dictation = policy_for("dictation")

    assert meeting.capture_microphone and meeting.capture_system_audio
    assert learning.screenshots and learning.default_marker == "not_understood"
    assert reading.capture_microphone and not reading.capture_system_audio
    assert idea.projection == "idea"
    assert not dictation.durable and not dictation.screenshots
