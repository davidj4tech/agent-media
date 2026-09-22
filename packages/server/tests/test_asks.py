"""Claude Code's AskUserQuestion on a pane: read, shaped, and answered.

The captures in fixtures/asks/ are a real Claude Code (2.1.278) in a
throwaway pane, 22 Sep 2026. `FakeAsk` is that dialog as measured there —
what each key does — drawn the way those captures are, so the key sequences
`asks.drive` sends are checked against the behaviour, not against themselves.
"""

from pathlib import Path

import pytest

from agent_media_server import asks, auth_abs, panes, send, sessions

FIX = Path(__file__).parent / "fixtures" / "asks"
SID = "11111111-2222-3333-4444-555555555555"
RULE = "─" * 60


def cap(name: str) -> str:
    return (FIX / f"{name}.txt").read_text()


# --- reading the screen ------------------------------------------------------------


def test_a_fresh_multi_select_is_read_with_its_boxes_and_descriptions():
    d = asks.parse(cap("multi_fresh"))
    assert d["multiSelect"] is True and d["review"] is False
    assert d["question"] == "Which fruits do you like?"
    assert d["tabs"] == [{"header": "Fruit", "answered": False}] and d["submit_tab"]
    assert d["options"] == [
        {"n": 1, "label": "Apple", "detail": "crisp", "checked": False},
        {"n": 2, "label": "Pear", "detail": "soft", "checked": False},
        {"n": 3, "label": "Plum", "detail": "tart", "checked": False}]
    assert d["free"] == {"n": 4, "text": "", "checked": False}
    assert d["chat_n"] == 5 and d["cursor"] == 1 and d["partial"] is False


def test_ticked_boxes_the_cursor_and_typed_words_are_read():
    d = asks.parse(cap("multi_checked"))
    assert [o["checked"] for o in d["options"]] == [True, False, True]
    assert d["cursor"] == 2 and d["tabs"][0]["answered"] is True
    d = asks.parse(cap("multi_other_typed"))
    assert d["free"] == {"n": 4, "text": "kiwi", "checked": True} and d["cursor"] == 4


def test_a_narrow_pane_wraps_the_box_and_is_still_read():
    d = asks.parse(cap("multi_narrow"))
    assert d["question"] == "Which of these long tasks should I start next today?"
    assert d["options"][0] == {"n": 1, "label": "Speech and multi-line fixes",
                               "detail": "Stop silences only that thread speech including queued replies",
                               "checked": True}
    assert d["options"][1]["label"] == "Every harness sessions"
    assert d["options"][1]["checked"] is False


def test_single_select_and_its_tick():
    d = asks.parse(cap("single"))
    assert d["multiSelect"] is False and d["submit_tab"] is False
    assert [o["label"] for o in d["options"]] == ["Small", "Large"]
    assert d["options"][0]["detail"] == "little" and d["free"]["n"] == 3
    d = asks.parse(cap("two_q1_answered"))
    assert [o["checked"] for o in d["options"]] == [False, True, False]


def test_several_questions_show_their_tabs():
    d = asks.parse(cap("two_q2"))
    assert d["tabs"] == [{"header": "Colour", "answered": True},
                         {"header": "Pets", "answered": False}]
    assert d["question"] == "Which pets?" and d["multiSelect"] is True


def test_the_review_page():
    d = asks.parse(cap("review_single"))
    assert d["review"] is True and d["unanswered"] is False
    assert d["reviewed"] == [{"question": "Which fruits do you like?", "answer": "Apple, Plum, kiwi"}]
    d = asks.parse(cap("review_two_partial"))
    assert d["unanswered"] is True and d["reviewed"][0]["answer"] == "Blue"


def test_every_capture_is_an_approval_and_a_permission_prompt_is_no_question():
    for f in FIX.glob("*.txt"):
        assert panes.classify(f.read_text(), "claude") == "approval", f.name
    plan = """  ──────────────────────────────
   Do you want to proceed?
   ❯ 1. Yes
     2. No
"""
    assert asks.parse(plan) is None


# --- the approval --------------------------------------------------------------------

HOOK = {"questions": [
    {"question": "Which colour?", "header": "Colour", "multiSelect": False,
     "options": [{"label": "Red", "description": "warm"}, {"label": "Blue", "description": "cool"},
                 {"label": "Green", "description": "calm"}]},
    {"question": "Which pets?", "header": "Pets", "multiSelect": True,
     "options": [{"label": "Cat", "description": "independent"},
                 {"label": "Dog", "description": "loyal"},
                 {"label": "Fish", "description": "quiet"}]}]}


def _keep(session=SID, tool_use_id="toolu_01", questions=HOOK["questions"]):
    from agent_media_core import pending_asks

    assert pending_asks.record(session, tool_use_id, {"questions": questions})


def test_the_hook_s_copy_gives_every_tab_of_the_question():
    _keep()
    a = asks.approval(cap("two_q2"), SID)
    assert a["kind"] == "question" and a["source"] == "hook" and a["tool_use_id"] == "toolu_01"
    assert [q["question"] for q in a["questions"]] == ["Which colour?", "Which pets?"]
    assert a["questions"][1]["options"][2] == {"n": 3, "label": "Fish", "description": "quiet",
                                               "detail": "quiet", "checked": False}
    assert a["current"] == 1 and a["multiSelect"] is True and a["free_text"] is True
    assert a["question"] == "Which pets?" and a["partial"] is False
    # The key follows the question, not which tab is showing or what is ticked.
    assert asks.approval(cap("two_q1"), SID)["key"] == a["key"]
    assert asks.approval(cap("review_two_partial"), SID)["key"] == a["key"]


def test_a_hook_copy_of_another_question_is_not_used():
    _keep(questions=[{"question": "Something else?", "options": [{"label": "A"}]}])
    a = asks.approval(cap("multi_fresh"), SID)
    assert a["source"] == "screen" and a["tool_use_id"] == ""
    assert a["questions"] == [{"question": "Which fruits do you like?", "header": "Fruit",
                               "multiSelect": True, "free_text": True,
                               "options": [{"n": 1, "label": "Apple", "description": "crisp",
                                            "detail": "crisp", "checked": False},
                                           {"n": 2, "label": "Pear", "description": "soft",
                                            "detail": "soft", "checked": False},
                                           {"n": 3, "label": "Plum", "description": "tart",
                                            "detail": "tart", "checked": False}]}]


def test_the_v0_fields_are_kept_for_a_client_that_answers_by_number():
    a = asks.approval(cap("single"))
    assert a["question"] == "Pick a size?" and a["partial"] is False and len(a["key"]) == 12
    assert [(o["n"], o["label"]) for o in a["options"]] == [
        (1, "Small"), (2, "Large"), (3, "Type something"), (4, "Chat about this")]
    assert a["multiSelect"] is False and a["questions"][0]["header"] == "Size"


def test_several_tabs_without_the_hook_are_partial():
    a = asks.approval(cap("two_q2"))
    # No card for part of a question: the app sends it to the desk.
    assert a["partial"] is True and a["questions"] == [] and a["kind"] == "question"
    assert a["question"] == "Which pets?" and a["options"][0]["label"] == "Cat"


def test_approval_for_reads_a_question_through_the_pane(monkeypatch):
    _keep()
    monkeypatch.setattr(sessions, "_capture_pane", lambda p: cap("two_q1"))
    a = sessions.approval_for("%7", "claude", SID)
    assert a["kind"] == "question" and a["questions"][0]["header"] == "Colour"


# --- the answer, as data -------------------------------------------------------------

def _qs():
    _keep()
    return asks.approval(cap("two_q1"), SID)["questions"]


def test_answers_by_index_and_by_label_come_out_the_same():
    qs = _qs()
    a = asks.normalise(qs, [{"question_index": 0, "selected": [2]},
                            {"question_index": 1, "selected": [1, 3], "other_text": "a newt"}])
    b = asks.normalise(qs, {"Which colour?": "Blue", "Which pets?": ["Cat", "Fish", "a newt"]})
    assert a == b == [{"selected": [2], "other": ""}, {"selected": [1, 3], "other": "a newt"}]
    assert asks.as_text(qs, a) == {"Which colour?": "Blue", "Which pets?": "Cat, Fish, a newt"}
    assert asks.as_labels(qs, a) == {"Which colour?": "Blue", "Which pets?": ["Cat", "Fish", "a newt"]}


@pytest.mark.parametrize("answers, why", [
    ([{"question_index": 0, "selected": [2]}], "no answer for 'Which pets?'"),
    ([{"question_index": 0, "selected": [9]}, {"question_index": 1, "selected": [1]}], "no option 9"),
    ([{"question_index": 0, "selected": [1, 2]}, {"question_index": 1, "selected": [1]}], "takes one answer"),
    ([{"question_index": 0, "selected": [1], "other_text": "teal"},
      {"question_index": 1, "selected": [1]}], "takes one answer"),
    ([{"question_index": 5, "selected": [1]}], "no such question"),
    ({"Nope?": "x"}, "no such question"),
    ("apple", "answers needed"),
])
def test_an_answer_that_does_not_fit_is_refused(answers, why):
    with pytest.raises(asks.Refused) as e:
        asks.normalise(_qs(), answers)
    assert why in str(e.value) and e.value.status == 400


# --- the keys ------------------------------------------------------------------------


class FakeAsk:
    """The dialog as measured: what each key does, drawn like the captures."""

    def __init__(self, questions, *, above="● Here is what I found:\n  1. one\n  2. two\n"):
        self.qs = questions
        self.tabbed = len(questions) > 1 or any(q["multiSelect"] for q in questions)
        self.tab, self.review, self.cursor, self.gone = 0, False, 1, False
        self.state = [{"checked": set(), "choice": None, "text": "", "free": False}
                      for _ in questions]
        self.sent = None
        self.keys: list[str] = []
        self.above = above
        self.deaf = False

    # -- drawing --
    def _answer(self, i):
        q, s = self.qs[i], self.state[i]
        labels = [o["label"] for o in q["options"]]
        if q["multiSelect"]:
            got = [labels[n - 1] for n in sorted(s["checked"])] + ([s["text"]] if s["free"] and s["text"] else [])
        elif s["choice"] == "other":
            got = [s["text"]]
        else:
            got = [labels[s["choice"] - 1]] if s["choice"] else []
        return ", ".join(got)

    def render(self) -> str:
        if self.gone:
            return "● Thanks.\n\n❯ \n  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
        tabs = "  ".join(("☒ " if self._answer(i) else "☐ ") + q["header"]
                         for i, q in enumerate(self.qs))
        tabrow = f"←  {tabs}  ✔ Submit  →" if self.tabbed else f" {tabs}"
        out = [self.above, RULE, tabrow, ""]
        if self.review:
            out += ["Review your answers", ""]
            if not all(self._answer(i) for i in range(len(self.qs))):
                out += ["⚠ You have not answered all questions", ""]
            for i, q in enumerate(self.qs):
                if self._answer(i):
                    out += [f" ● {q['question']}", f"   → {self._answer(i)}"]
            out += ["", "Ready to submit your answers?", "", "❯ 1. Submit answers", "  2. Cancel", ""]
            return "\n".join(out)
        q, s = self.qs[self.tab], self.state[self.tab]
        k = len(q["options"])
        out += [q["question"], ""]
        for j, o in enumerate(q["options"], 1):
            mark = "❯ " if self.cursor == j else "  "
            if q["multiSelect"]:
                box = "[✔]" if j in s["checked"] else "[ ]"
                out += [f"{mark}{j}. {box} {o['label']}", f"  {o['description']}"]
            else:
                tick = " ✔" if s["choice"] == j else ""
                out += [f"{mark}{j}. {o['label']}{tick}", f"     {o['description']}"]
        mark = "❯ " if self.cursor == k + 1 else "  "
        if q["multiSelect"]:
            box = "[✔]" if s["free"] else "[ ]"
            out += [f"{mark}{k + 1}. {box} {s['text'] or 'Type something'}",
                    ("❯    Submit" if self.cursor == "submit" else "     Submit")]
        else:
            out += [f"{mark}{k + 1}. {s['text'] or 'Type something.'}"]
        out += [RULE, f"  {k + 2}. Chat about this", "",
                "Enter to select · Tab/Arrow keys to navigate · Esc to cancel"]
        return "\n".join(out)

    # -- keys --
    def _advance(self):
        if not self.tabbed:
            self.gone, self.sent = True, {q["question"]: self._answer(i) for i, q in enumerate(self.qs)}
            return
        self.tab += 1
        self.cursor = 1
        if self.tab == len(self.qs):
            self.review = True

    def type(self, text):
        self.keys.append(f"'{text}'")
        if self.deaf:
            return
        for ch in text:
            self._char(ch)

    def _char(self, ch):
        q, s = self.qs[self.tab], self.state[self.tab]
        k = len(q["options"])
        if self.cursor == k + 1 and not self.review:
            s["text"] += ch
            if q["multiSelect"]:
                s["free"] = True
            return
        self.key(ch, record=False)

    def key(self, name, record=True):
        if record:
            self.keys.append(name)
        if self.deaf or self.gone:
            return
        if self.review:
            if name in ("1", "Enter"):
                self.gone = True
                self.sent = {q["question"]: self._answer(i) for i, q in enumerate(self.qs)}
            elif name == "Left":
                self.review, self.tab = False, len(self.qs) - 1
            return
        q, s = self.qs[self.tab], self.state[self.tab]
        k = len(q["options"])
        on_text = self.cursor == k + 1
        if on_text and len(name) == 1 and name not in "\t":
            return self._char(name)
        if name == "BSpace":
            if on_text:
                s["text"] = s["text"][:-1]
            return
        if name.isdigit():
            n = int(name)
            if q["multiSelect"]:
                if 1 <= n <= k:
                    s["checked"] ^= {n}
                elif n == k + 1:
                    s["free"] = not s["free"]
            else:
                if 1 <= n <= k:
                    s["choice"] = n
                    self._advance()
                elif n == k + 1:
                    self.cursor = k + 1
            return
        rows = list(range(1, k + 2)) + (["submit"] if q["multiSelect"] else [])
        if name == "Down":
            self.cursor = rows[min(rows.index(self.cursor) + 1, len(rows) - 1)]
        elif name == "Up":
            self.cursor = rows[max(rows.index(self.cursor) - 1, 0)]
        elif name == "Left":
            if self.tab:
                self.tab, self.cursor = self.tab - 1, 1
        elif name == "Tab":
            if q["multiSelect"] and on_text:
                self.cursor = "submit"
            elif self.tabbed:
                self._advance()
        elif name == "Enter":
            if self.cursor == "submit":
                self._advance()
            elif q["multiSelect"]:
                if on_text:
                    s["free"] = not s["free"]
                else:
                    s["checked"] ^= {self.cursor}
            elif on_text:
                if s["text"]:
                    s["choice"] = "other"
                    self._advance()
            else:
                s["choice"] = self.cursor
                self._advance()


class FakeScreen(asks.Screen):
    def __init__(self, fake):
        self.fake = fake

    def read(self):
        return asks.parse(self.fake.render())

    def key(self, name):
        self.fake.key(name)

    def type(self, text):
        self.fake.type(text)

    def sleep(self, s):
        pass


@pytest.fixture(autouse=True)
def _quick(monkeypatch):
    monkeypatch.setattr(asks, "STEP_TIMEOUT_S", 0.05)
    monkeypatch.setattr(asks, "STEP_POLL_S", 0.0)


def _drive(fake, answers, questions=None):
    qs = questions or asks.approval(fake.render(), SID)["questions"]
    want = asks.normalise(qs, answers)
    asks.drive(FakeScreen(fake), qs, want,
               tabbed=not (len(qs) == 1 and not qs[0]["multiSelect"]))
    return fake.sent


def test_the_fake_is_drawn_like_the_captures():
    fake = FakeAsk(HOOK["questions"][1:])
    mine, real = asks.parse(fake.render()), asks.parse(cap("multi_fresh"))
    assert set(mine) == set(real) and mine["multiSelect"] and mine["free"]["n"] == 4


def test_one_single_select_is_one_digit():
    fake = FakeAsk([{"question": "Pick a size?", "header": "Size", "multiSelect": False,
                     "options": [{"label": "Small", "description": "little"},
                                 {"label": "Large", "description": "big"}]}])
    assert _drive(fake, [{"question_index": 0, "selected": [2]}]) == {"Pick a size?": "Large"}
    assert fake.keys == ["2"]


def test_one_single_select_in_your_own_words():
    fake = FakeAsk([{"question": "Pick a size?", "header": "Size", "multiSelect": False,
                     "options": [{"label": "Small", "description": "little"},
                                 {"label": "Large", "description": "big"}]}])
    assert _drive(fake, [{"question_index": 0, "selected": [], "other_text": "medium"}]) == \
        {"Pick a size?": "medium"}
    assert fake.keys == ["3", "'medium'", "Enter"]


def test_a_multi_select_ticks_its_boxes_then_tabs_to_review_and_sends():
    fake = FakeAsk(HOOK["questions"][1:])
    assert _drive(fake, [{"question_index": 0, "selected": [1, 3]}]) == {"Which pets?": "Cat, Fish"}
    assert fake.keys == ["1", "3", "Tab", "1"]


def test_a_multi_select_with_words_goes_down_to_the_free_row():
    fake = FakeAsk(HOOK["questions"][1:])
    assert _drive(fake, [{"question_index": 0, "selected": [2], "other_text": "a newt"}]) == \
        {"Which pets?": "Dog, a newt"}
    assert fake.keys == ["2", "Down", "Down", "Down", "'a newt'", "Tab", "Enter", "1"]


def test_two_questions_one_tab_at_a_time():
    _keep()
    fake = FakeAsk(HOOK["questions"])
    got = _drive(fake, [{"question_index": 0, "selected": [], "other_text": "teal"},
                        {"question_index": 1, "selected": [1, 3]}])
    assert got == {"Which colour?": "teal", "Which pets?": "Cat, Fish"}


def test_what_the_desk_already_ticked_is_put_right():
    _keep()
    fake = FakeAsk(HOOK["questions"])
    fake.state[1]["checked"] = {2}
    fake.state[1]["free"], fake.state[1]["text"] = True, "old words"
    fake.tab, fake.review = 1, False          # someone at the desk moved on a tab
    got = _drive(fake, {"Which colour?": "Red", "Which pets?": ["Fish", "a newt"]})
    assert got == {"Which colour?": "Red", "Which pets?": "Fish, a newt"}
    assert fake.keys[0] == "Left"


def test_from_the_review_page_it_goes_back_to_the_first_tab():
    _keep()
    fake = FakeAsk(HOOK["questions"])
    fake.state[0]["choice"] = 1
    fake.tab, fake.review = 2, True
    got = _drive(fake, [{"question_index": 0, "selected": [3]},
                        {"question_index": 1, "selected": [2]}])
    assert got == {"Which colour?": "Green", "Which pets?": "Dog"}
    assert fake.keys[:2] == ["Left", "Left"]


def test_a_screen_that_ignores_the_keys_is_stuck_not_answered():
    fake = FakeAsk(HOOK["questions"][1:])
    fake.deaf = True
    with pytest.raises(asks.Stuck):
        _drive(fake, [{"question_index": 0, "selected": [1]}])
    assert fake.sent is None


# --- through the route's code, keys into the pane ---------------------------------


@pytest.fixture
def pane(monkeypatch):
    monkeypatch.setattr(auth_abs, "abs_identity", lambda b: ({"username": "d", "type": "root"}, 200))
    monkeypatch.setattr(auth_abs, "may_reply", lambda u: (True, ""))
    monkeypatch.setattr(sessions, "live_sessions", lambda: {SID: "%7"})
    monkeypatch.setattr(panes, "alive", lambda p: True)
    monkeypatch.setattr(sessions, "_agent_of_pane", lambda p: "claude")
    monkeypatch.setattr(send.time, "sleep", lambda s: None)
    monkeypatch.setattr(asks.time, "sleep", lambda s: None)
    box = {}

    def install(fake):
        box["fake"] = fake
        monkeypatch.setattr(sessions, "_capture_pane", lambda p: fake.render())

        def tmux(argv, timeout=10):
            assert argv[:3] == ["send-keys", "-t", "%7"]
            if argv[3] == "-l":
                fake.type(argv[4])
            else:
                fake.key(argv[3])
            return ""
        monkeypatch.setattr(panes, "_tmux", tmux)
        return fake
    return install


def test_session_answer_gives_a_pane_its_structured_answers(pane):
    _keep()
    fake = pane(FakeAsk(HOOK["questions"]))
    key = sessions.approval_for("%7", "claude", SID)["key"]
    ok, d = send.answer(SID, 0, key, "tok", answers=[
        {"question_index": 0, "selected": [2]},
        {"question_index": 1, "selected": [1], "other_text": "a newt"}])
    assert ok, d
    assert d["answers"] == {"Which colour?": "Blue", "Which pets?": "Cat, a newt"}
    assert d["waiting"] is False and d["approval"] is None
    assert fake.sent == d["answers"]


def test_a_stale_key_is_409_with_the_question_now_up(pane):
    _keep()
    fake = pane(FakeAsk(HOOK["questions"]))
    ok, d = send.answer(SID, 0, "not-the-key0", "tok",
                        answers=[{"question_index": 0, "selected": [1]}])
    assert not ok and d["status"] == 409 and d["approval"]["kind"] == "question"
    assert fake.keys == []


def test_an_answer_that_does_not_fit_presses_nothing(pane):
    _keep()
    fake = pane(FakeAsk(HOOK["questions"]))
    key = sessions.approval_for("%7", "claude", SID)["key"]
    ok, d = send.answer(SID, 0, key, "tok", answers=[{"question_index": 0, "selected": [1]}])
    assert not ok and d["status"] == 400 and "Which pets?" in d["error"]
    assert fake.keys == []


def test_a_number_on_a_lone_multi_select_ticks_that_one(pane):
    fake = pane(FakeAsk(HOOK["questions"][1:]))
    key = sessions.approval_for("%7", "claude", SID)["key"]
    ok, d = send.answer(SID, 3, key, "tok")
    assert ok and d["answers"] == {"Which pets?": "Fish"}


def test_a_number_cannot_answer_two_questions(pane):
    _keep()
    fake = pane(FakeAsk(HOOK["questions"]))
    ok, d = send.answer(SID, 1, "", "tok")
    assert not ok and d["status"] == 400 and fake.keys == []


def test_without_the_hook_several_tabs_go_to_the_desk(pane):
    fake = pane(FakeAsk(HOOK["questions"]))
    ok, d = send.answer(SID, 0, "", "tok", answers=[{"question_index": 0, "selected": [1]}])
    assert not ok and d["status"] == 409 and "at the desk" in d["error"]
    assert fake.keys == []


def test_a_permission_prompt_refuses_answers(pane, monkeypatch):
    monkeypatch.setattr(sessions, "_capture_pane", lambda p: " Do you want to proceed?\n ❯ 1. Yes\n   2. No\n")
    ok, d = send.answer(SID, 0, "", "tok", answers=[{"question_index": 0, "selected": [1]}])
    assert not ok and d["status"] == 400 and "by number" in d["error"]


def test_a_stuck_pane_is_504(pane):
    fake = pane(FakeAsk(HOOK["questions"][1:]))
    fake.deaf = True
    ok, d = send.answer(SID, 0, "", "tok", answers=[{"question_index": 0, "selected": [1]}])
    assert not ok and d["status"] == 504 and d["approval"]["kind"] == "question"


# --- the hook keeps the question ------------------------------------------------------


def test_the_pretooluse_hook_keeps_the_question(monkeypatch):
    from agent_media_core import pending_asks
    from agent_media_core.intake import hook_claude_code as H

    monkeypatch.setattr(H, "_emit_ask", lambda *a, **k: 0)
    H._handle_pretooluse({"tool_name": "AskUserQuestion", "session_id": SID,
                          "tool_use_id": "toolu_9", "tool_input": HOOK})
    got = pending_asks.read(SID)
    assert got["tool_use_id"] == "toolu_9" and got["questions"][1]["header"] == "Pets"
    monkeypatch.setattr(H, "_record_listener_text", lambda s, t: 0)
    H._handle_posttooluse({"tool_name": "AskUserQuestion", "session_id": SID,
                           "tool_use_id": "toolu_9",
                           "tool_response": {"answers": {"Which colour?": "Red"}}})
    assert pending_asks.read(SID) is None
