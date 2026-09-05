from orchestrator.hub import EventHub


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_seq_starts_at_one_and_increments():
    hub = EventHub()
    assert hub.publish({"t": "state", "s": "idle"})["seq"] == 1
    assert hub.publish({"t": "unclear", "conf": 0.3})["seq"] == 2


def test_hello_carries_no_seq_and_does_not_consume_one():
    hub = EventHub()
    assert "seq" not in hub.publish({"t": "hello", "v": 1})
    assert hub.publish({"t": "state", "s": "idle"})["seq"] == 1


def test_seq_is_placed_directly_after_t():
    """Key order matters: the fixtures are byte-compared."""
    hub = EventHub()
    assert list(hub.publish({"t": "state", "s": "idle"})) == ["t", "seq", "s"]


def test_a_subscriber_receives_published_messages():
    hub = EventHub()
    q = hub.subscribe()
    drain(q)
    hub.publish({"t": "state", "s": "listening"})
    assert drain(q) == [{"t": "state", "seq": 1, "s": "listening"}]


def test_a_new_subscriber_is_seeded_with_hello_and_the_current_state():
    hub = EventHub()
    hub.publish({"t": "state", "s": "listening"})
    assert drain(hub.subscribe()) == [
        {"t": "hello", "v": 1},
        {"t": "state", "seq": 1, "s": "listening"},
    ]


def test_replay_does_not_mint_a_new_seq():
    hub = EventHub()
    hub.publish({"t": "state", "s": "idle"})
    hub.subscribe()
    assert hub.seq == 1


def test_only_state_is_retained_for_replay():
    """A result is a one-shot answer; replaying it would strand the panel."""
    hub = EventHub()
    hub.publish({"t": "state", "s": "idle"})
    hub.publish({"t": "result", "id": "hello", "text": "Hello", "conf": 0.9})
    assert drain(hub.subscribe())[-1]["t"] == "state"


def test_a_fifth_subscriber_is_refused():
    hub = EventHub(max_subscribers=4)
    assert all(hub.subscribe() is not None for _ in range(4))
    assert hub.subscribe() is None


def test_unsubscribe_frees_a_slot():
    hub = EventHub(max_subscribers=1)
    q = hub.subscribe()
    assert hub.subscribe() is None
    hub.unsubscribe(q)
    assert hub.subscribe() is not None


def test_a_full_queue_drops_the_oldest_and_counts_it():
    hub = EventHub(maxsize=2)
    hub.subscribe()
    for n in range(5):
        hub.publish({"t": "unclear", "conf": n / 10})
    assert hub.dropped == 3


def test_publish_never_raises_with_no_subscribers():
    EventHub().publish({"t": "state", "s": "idle"})


def test_seq_is_contiguous_across_a_run():
    """DoD 6."""
    hub = EventHub()
    q = hub.subscribe()
    drain(q)
    for n in range(50):
        hub.publish({"t": "unclear", "conf": 0.1})
    assert [m["seq"] for m in drain(q)] == list(range(1, 51))
