"""The recordings folder kept in step with the disk, and takes deleted and renamed: through the functions, through
the status page's endpoints and from the recorder (the headset menu's "Delete the last take")."""
import json
import os
import time
import urllib.error
import urllib.request

import numpy as np
import pytest

from quest_teleop.recording import (TakeRecorder, delete_takes, read_converted, read_index, reconcile, rename_take,
                                    sanitize_name, take_files)
from quest_teleop.status import StatusPage


class _Server:
    def monitor(self):
        return dict(time=time.time(), clients=[])


def _record(rec, steps=5):
    rec.start()
    for i in range(steps):
        rec.add(i * 0.02, np.full((len(rec.tools), 3), 0.01 * i, np.float32))
    entry = rec.stop()
    rec.wait()
    return entry


def _folder(tmp_path, n=3):
    rec = TakeRecorder(str(tmp_path), session="s1", tools=("left", "right"))
    return rec, [_record(rec)["file"] for _ in range(n)]


def _post(page, path, body, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{page.port}{path}", data=json.dumps(body).encode(), method="POST",
                                 headers=headers or {"Content-Type": "application/json", "X-Requested-With": "quest-teleop"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _get(page, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{page.port}{path}") as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def served(tmp_path):
    rec, files = _folder(tmp_path)
    page = StatusPage(_Server(), port=0, period=0.1, recorder=rec).start()
    yield tmp_path, rec, files, page
    page.stop()


def test_reconcile_missing_and_extra_file(tmp_path):
    rec, files = _folder(tmp_path)
    os.remove(tmp_path / files[1])                                   # deleted by hand
    os.makedirs(tmp_path / "s2")
    os.replace(tmp_path / files[2], tmp_path / "s2" / "copied.npz")  # moved by hand: gone from s1, new in s2
    (tmp_path / "s2" / "not_a_take.npz").write_bytes(b"junk")
    np.savez(tmp_path / "s2" / "other.npz", x=np.zeros(3))           # an .npz that is not a take
    out = reconcile(str(tmp_path))
    assert sorted(out["removed"]) == sorted(files[1:]) and out["added"] == [os.path.join("s2", "copied.npz")]
    index = read_index(str(tmp_path))
    assert [t["file"] for t in index] == [files[0], os.path.join("s2", "copied.npz")]
    added = index[1]
    assert added["session"] == "s2" and added["steps"] == 5 and added["tools"] == ["left", "right"] and added["recovered"]
    assert reconcile(str(tmp_path)) == dict(removed=[], added=[], converted_removed=[])     # nothing left to do
    assert not os.path.exists(tmp_path / "index.json.tmp")
    # the page starts from the reconciled index and never lists a missing take
    os.remove(tmp_path / files[0])
    page = StatusPage(_Server(), port=0, period=0.1, recordings_dir=str(tmp_path)).start()
    try:
        assert page.reconciled["removed"] == [files[0]]
        assert [t["file"] for t in read_index(str(tmp_path))] == [os.path.join("s2", "copied.npz")]
        code, body = _get(page, "/api/take?file=" + files[0])
        assert code == 404 and body["error"] == "this take no longer exists"
        time.sleep(0.4)
        assert [t["file"] for t in _get(page, "/api/state")[1]["recordings"]["takes"]] == [os.path.join("s2", "copied.npz")]
    finally:
        page.stop()


def test_reconcile_converted_index(tmp_path):
    rec, files = _folder(tmp_path, 1)
    conv = tmp_path / "converted"
    os.makedirs(conv / "kept")
    (conv / "kept" / "shard_0.bin").write_bytes(b"x")
    json.dump(dict(version=1, datasets=[dict(name="kept", out=str(conv / "kept"), takes=files),
                                        dict(name="gone", out=str(conv / "gone"), takes=files)]), open(conv / "index.json", "w"))
    assert reconcile(str(tmp_path))["converted_removed"] == ["gone"]
    assert [d["name"] for d in read_converted(str(tmp_path))] == ["kept"]


def test_delete_through_the_endpoint(served):
    root, rec, files, page = served
    (root / files[0]).with_suffix(".json").write_text("{}")          # a sidecar goes with its take
    conv = root / "converted" / "clips"
    os.makedirs(conv)
    (conv / "shard_0.bin").write_bytes(b"x")
    json.dump(dict(version=1, datasets=[dict(name="clips", out=str(conv), takes=[files[0], files[1]])]),
              open(root / "converted" / "index.json", "w"))
    code, out = _post(page, "/api/delete", dict(files=[files[0]]))
    assert code == 200 and out["deleted"] == [files[0]] and len(out["removed"]) == 2 and not out["refused"]
    assert not os.path.exists(root / files[0]) and not os.path.exists((root / files[0]).with_suffix(".json"))
    assert [t["file"] for t in read_index(str(root))] == files[1:]
    assert conv.is_dir() and read_converted(str(root)), "converted clips stay unless asked for"
    assert [t["file"] for t in _get(page, "/api/state")[1]["recordings"]["takes"]] == files[:0:-1]     # shown at once
    code, out = _post(page, "/api/delete", dict(files=[files[1]], converted=True))
    assert code == 200 and out["converted_removed"] == ["clips"] and not conv.exists() and read_converted(str(root)) == []
    assert _post(page, "/api/delete", dict(files=[files[1]]))[1]["refused"][0]["reason"] == "no such take"
    # only this page's own requests are accepted
    assert _post(page, "/api/delete", dict(files=[files[2]]), {"Content-Type": "application/json"})[0] == 403
    assert _post(page, "/api/delete", dict(files=[files[2]]), {"X-Requested-With": "quest-teleop", "Origin": "http://example.org"})[0] == 403
    assert os.path.isfile(root / files[2])


def test_traversal_and_symlinks_rejected(served, tmp_path_factory):
    root, rec, files, page = served
    outside = tmp_path_factory.mktemp("outside")
    np.savez(outside / "victim.npz", t=np.zeros(2), **{"tool/pos": np.zeros((2, 1, 3))})
    os.symlink(outside / "victim.npz", root / "s1" / "link.npz")     # a link to a file outside
    os.symlink(outside, root / "linked")                             # a link to a folder outside
    rel_out = os.path.relpath(outside / "victim.npz", root)
    bad = [rel_out, "../" + os.path.basename(str(outside)) + "/victim.npz", str(outside / "victim.npz"), "s1/../../x.npz",
           "s1/link.npz", "linked/victim.npz", "index.json", "s1\\..\\..\\x.npz", ""]
    code, out = _post(page, "/api/delete", dict(files=bad))
    assert code == 200 and out["deleted"] == [] and out["removed"] == [] and len(out["refused"]) == len(bad)
    assert os.path.isfile(outside / "victim.npz") and os.path.isfile(root / "index.json") and len(read_index(str(root))) == 3
    for f in bad:
        assert _post(page, "/api/rename", dict(file=f, title="x"))[0] == 400
        assert _get(page, "/api/take?file=" + urllib.request.quote(f))[0] == 404
    for name in ("../up", "a/b", "a\\b", "..", "x..y", "", "   ", "."):
        code, out = _post(page, "/api/rename", dict(file=files[0], rename_file=True, new_name=name))
        assert code == 400 and out["error"], name
    assert sorted(t["file"] for t in read_index(str(root))) == sorted(files) and not os.path.exists(root.parent / "up.npz")
    assert reconcile(str(root))["added"] == [], "linked files are never taken into the index"


def test_take_being_recorded_is_protected(served):
    root, rec, files, page = served
    rec.start()
    rec.add(0.0, np.zeros((2, 3), np.float32))
    busy = rec.busy_files()[0]
    assert _post(page, "/api/delete", dict(files=[busy]))[1]["refused"][0]["reason"] == "the take is being recorded or written"
    assert _post(page, "/api/rename", dict(file=busy, title="x"))[0] == 400
    # written but not complete yet: listed, and still protected
    open(root / (busy + ".part"), "wb").close()
    entry = dict(read_index(str(root))[0], file=busy, take=4)
    json.dump(dict(version=2, takes=read_index(str(root)) + [entry]), open(root / "index.json", "w"))
    assert reconcile(str(root))["removed"] == []
    assert delete_takes(str(root), [busy])["refused"][0]["reason"] == "the take is being recorded or written"
    with pytest.raises(ValueError, match="no longer exists|being recorded"):
        rename_take(str(root), busy, title="x")
    os.remove(root / (busy + ".part"))
    done = rec.stop()
    rec.wait()
    assert done["file"] == busy and done["take"] == 4
    time.sleep(0.3)
    row = next(t for t in _get(page, "/api/state")[1]["recordings"]["takes"] if t["file"] == busy)
    assert not row["busy"]
    assert _post(page, "/api/delete", dict(files=[busy]))[1]["deleted"] == [busy]


def test_delete_last_uses_the_same_path(tmp_path):
    rec, files = _folder(tmp_path)
    (tmp_path / files[2]).with_suffix(".json").write_text("{}")
    gone = rec.delete_last()
    assert gone["file"] == files[2] and not os.path.exists((tmp_path / files[2]).with_suffix(".json"))
    assert [t["file"] for t in read_index(str(tmp_path))] == files[:2] and rec.take == 3
    os.remove(tmp_path / files[1])                                   # removed by hand: the numbering follows
    reconcile(str(tmp_path))
    assert _record(rec)["take"] == 2


def test_rename_title_and_note(served):
    root, rec, files, page = served
    code, out = _post(page, "/api/rename", dict(file=files[0], title="  Pick and   place ", note="first good one"))
    assert code == 200 and out["entry"]["title"] == "Pick and place" and out["entry"]["file"] == files[0]
    entry = read_index(str(root))[0]
    assert entry["title"] == "Pick and place" and entry["note"] == "first good one" and os.path.isfile(root / files[0])
    assert json.load(open((root / files[0]).with_suffix(".json"))) == dict(title="Pick and place", note="first good one")
    row = next(t for t in _get(page, "/api/state")[1]["recordings"]["takes"] if t["file"] == files[0])
    assert row["title"] == "Pick and place" and row["take"] == 1
    assert _get(page, "/api/take?file=" + files[0])[1]["title"] == "Pick and place"
    os.remove(root / "index.json")                                   # the name survives a rebuilt index
    reconcile(str(root))
    assert next(t for t in read_index(str(root)) if t["file"] == files[0])["title"] == "Pick and place"
    _post(page, "/api/rename", dict(file=files[0], title="", note=""))
    assert "title" not in read_index(str(root))[0] and not os.path.exists((root / files[0]).with_suffix(".json"))


def test_rename_file_and_clash(served):
    root, rec, files, page = served
    (root / files[0]).with_suffix(".json").write_text(json.dumps(dict(title="Old")))
    os.makedirs(root / "converted" / "clips")
    (root / "converted" / "clips" / "shard_0.bin").write_bytes(b"x")
    json.dump(dict(version=1, datasets=[dict(name="clips", out=str(root / "converted" / "clips"), takes=files[:2])]),
              open(root / "converted" / "index.json", "w"))
    code, out = _post(page, "/api/rename", dict(file=files[0], title="Good one", rename_file=True, new_name="good one?.npz"))
    new = os.path.join("s1", "good one_.npz")
    assert code == 200 and out["entry"]["file"] == new and out["entry"]["original_file"] == files[0]
    assert os.path.isfile(root / new) and not os.path.exists(root / files[0])
    assert take_files(str(root), new) == [new, os.path.join("s1", "good one_.json")]
    assert read_index(str(root))[0]["file"] == new and read_converted(str(root))[0]["takes"] == [new, files[1]]
    assert _get(page, "/api/take?file=" + urllib.request.quote(new))[0] == 200 and _get(page, "/api/take?file=" + files[0])[0] == 404
    # clashes: another take's name, and an existing sidecar name
    other = os.path.splitext(os.path.basename(files[1]))[0]
    code, out = _post(page, "/api/rename", dict(file=new, rename_file=True, new_name=other))
    assert code == 400 and "already exists" in out["error"]
    (root / "s1" / "taken.json").write_text("{}")
    assert _post(page, "/api/rename", dict(file=new, rename_file=True, new_name="taken"))[0] == 400
    (root / "s1" / "session.json").write_text('{"keep": 1}')        # a file that is not a take's must not become one
    assert _post(page, "/api/rename", dict(file=files[2], rename_file=True, new_name="session"))[0] == 400
    assert json.load(open(root / "s1" / "session.json")) == dict(keep=1)
    assert os.path.isfile(root / new) and os.path.isfile(root / files[1]) and read_index(str(root))[0]["file"] == new
    # a take whose name starts like another's does not take that one's files along
    rename_take(str(root), files[1], new_name="good one_ second")
    assert take_files(str(root), new) == [new, os.path.join("s1", "good one_.json")]
    assert delete_takes(str(root), [new])["removed"] == [new, os.path.join("s1", "good one_.json")]
    assert os.path.isfile(root / "s1" / "good one_ second.npz")


def test_sanitize_name():
    assert sanitize_name("My take (3).npz") == "My take _3_"
    assert sanitize_name("  .hidden  ") == "hidden" and len(sanitize_name("a" * 200)) == 80
    for bad in ("a/b", "..", "a\\b", "", "...", "x/../y"):
        with pytest.raises(ValueError):
            sanitize_name(bad)
