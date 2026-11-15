"""Take numbers: they continue across server restarts with the same session name and never repeat a number that a
file in the session folder already has."""
import json
import os

import numpy as np

from quest_teleop.recording import TakeRecorder, read_index


def _record(rec, steps=3):
    rec.start()
    for k in range(steps):
        rec.add(0.02 * k, np.zeros((2, 3)))
    e = rec.stop()
    rec.wait()
    return e


def test_numbers_continue_across_restarts(tmp_path):
    a = TakeRecorder(str(tmp_path), "s1", tools=("left", "right"))
    assert [_record(a)["take"] for _ in range(2)] == [1, 2]
    b = TakeRecorder(str(tmp_path), "s1", tools=("left", "right"))        # a restarted server, same session
    e = _record(b)
    assert e["take"] == 3 and os.path.basename(e["file"]).startswith("take_003_")
    assert [t["take"] for t in read_index(str(tmp_path))] == [1, 2, 3]


def test_numbers_skip_files_whose_index_entry_is_gone(tmp_path):
    a = TakeRecorder(str(tmp_path), "s1", tools=("left", "right"))
    for _ in range(3):
        _record(a)
    with open(os.path.join(str(tmp_path), "index.json"), "w") as f:      # entries lost, files still there
        json.dump(dict(version=2, takes=[]), f)
    b = TakeRecorder(str(tmp_path), "s1", tools=("left", "right"))
    e = _record(b)
    assert e["take"] == 4 and os.path.basename(e["file"]).startswith("take_004_")
    names = sorted(os.listdir(os.path.join(str(tmp_path), "s1")))
    assert [n[:8] for n in names] == ["take_001", "take_002", "take_003", "take_004"]


def test_two_recorders_in_one_session_do_not_reuse_a_number(tmp_path):
    a = TakeRecorder(str(tmp_path), "s1", tools=("left", "right"))
    b = TakeRecorder(str(tmp_path), "s1", tools=("left", "right"))        # both think the next take is 1
    assert _record(a)["take"] == 1 and _record(b)["take"] == 2


def test_delete_last_frees_its_number(tmp_path):
    a = TakeRecorder(str(tmp_path), "s1", tools=("left", "right"))
    _record(a)
    _record(a)
    assert a.delete_last()["take"] == 2
    assert _record(a)["take"] == 2
