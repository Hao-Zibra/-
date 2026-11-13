import json

from farewatch import store


def test_history_gets_one_line_per_record(tmp_path):
    data_dir = tmp_path / "data"  # created on first use

    store.append_history(data_dir, {"watch": "china-lax", "cheapest": []})
    store.append_history(data_dir, {"watch": "中文也行", "hits": 1})

    lines = (data_dir / store.HISTORY_FILE).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [
        {"watch": "china-lax", "cheapest": []},
        {"watch": "中文也行", "hits": 1},
    ]
    assert "中文也行" in lines[1]  # stored readable, not as \u escapes


def test_state_round_trips(tmp_path):
    state = {"china-lax": {"issue": 3, "last": {"price": 3650.0, "currency": "USD"}}}

    store.save_state(tmp_path, state)

    assert store.load_state(tmp_path) == state


def test_no_state_file_means_no_state(tmp_path):
    assert store.load_state(tmp_path) == {}


def test_an_unreadable_state_file_counts_as_no_state(tmp_path, caplog):
    (tmp_path / store.STATE_FILE).write_text("{ truncated", encoding="utf-8")

    assert store.load_state(tmp_path) == {}
    assert "may be alerted again" in caplog.text


def test_a_state_file_in_the_wrong_shape_counts_as_no_state(tmp_path):
    (tmp_path / store.STATE_FILE).write_text('{"china-lax": 3}', encoding="utf-8")

    assert store.load_state(tmp_path) == {}
