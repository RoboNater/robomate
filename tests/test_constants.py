from agent_hub_common import MAX_MESSAGE_PART_BYTES, MAX_TYPED_RESULT_BYTES, MetaKeys


def test_payload_size_limits_are_the_spec_values() -> None:
    assert MAX_MESSAGE_PART_BYTES == 16 * 1024
    assert MAX_TYPED_RESULT_BYTES == 32 * 1024


def test_meta_keys_are_prefixed_and_unique() -> None:
    values = [key.value for key in MetaKeys]
    assert len(values) == len(set(values))
    for key in MetaKeys:
        assert key.startswith("hub.")
        assert isinstance(key, str)
