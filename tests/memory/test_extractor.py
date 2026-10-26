def test_default_prompt_forbids_outside_facts_and_replying():
    from openjarvis.memory.extractor import _DEFAULT_SYSTEM_PROMPT

    assert "explicitly stated" in _DEFAULT_SYSTEM_PROMPT
    assert "do not answer" in _DEFAULT_SYSTEM_PROMPT
